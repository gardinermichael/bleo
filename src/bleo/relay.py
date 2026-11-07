"""Relay router: picks relays, rolls on failures, hedges slow starts, falls back to direct.

A stdlib port of pi-freeflow v1.44.1 relay.ts built on bleo._http.Attempt. The router
returns an opened Attempt (response headers received); the caller handles its status.
"""

from dataclasses import dataclass
import json
import logging
import queue
import threading
import time
from urllib.parse import urlsplit

from ._http import Attempt
from .exceptions import RelayExhaustedError
from .relay_state import HealthBook, ordered_candidates, validate_relay_url

log = logging.getLogger('bleo.relay')

ROLL_STATUSES = frozenset({408, 429, 502, 503, 504} | set(range(520, 531)))
PEEK_LIMIT = 8192

# Verdicts for one response.
USABLE, ROLL, ROLL_UNMARKED, BARE_500 = 'usable', 'roll', 'roll-unmarked', 'bare-500'


@dataclass(frozen=True)
class Route:
    kind: str  # "relay" | "direct"
    url: str
    label: str = None


class RelayAttempt(Attempt):
    """An Attempt whose first bytes can be inspected and still be read by the caller."""

    _source = None
    _buffered = None
    _failure = None
    _whole = None

    def _chunks(self):
        if self._source is None:
            self._source = super().iter_chunks()
        return self._source

    def peek(self, limit=PEEK_LIMIT):
        """Read up to limit bytes and keep them for iter_chunks, iter_lines and read_all."""
        self._buffered = self._buffered or []
        size = sum(map(len, self._buffered))
        try:
            for chunk in self._chunks():
                self._buffered.append(chunk)
                size += len(chunk)
                if size >= limit:
                    break
        except Exception as error:  # the caller meets it again after the buffered bytes
            self._failure = error
        return b''.join(self._buffered)[:limit]

    def iter_chunks(self):
        while self._buffered:
            yield self._buffered.pop(0)
        if self._failure is not None:
            raise self._failure
        yield from self._chunks()

    def read_all(self):
        if self._whole is None:
            self._whole = b''.join(self.iter_chunks())
        return self._whole

    def iter_lines(self):
        pending = b''
        for chunk in self.iter_chunks():
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                yield line.rstrip(b'\r').decode('utf-8', 'replace')
        if pending:
            yield pending.decode('utf-8', 'replace')


def classify(attempt):
    """Decide what the router does with a response, from its status, headers and (for 401
    and 402 only) the first bytes of its body."""
    status, headers = attempt.status, attempt.headers
    if status in ROLL_STATUSES:
        return ROLL
    if status == 413:
        return ROLL_UNMARKED
    if status == 404:
        content_type = (headers.get('content-type') or '').lower()
        return ROLL if headers.get('x-vercel-error') or 'json' not in content_type else USABLE
    if status == 402:
        if headers.get('x-vercel-error'):
            return ROLL
        return ROLL if 'deployment_disabled' in _peek_text(attempt).lower() else USABLE
    if status == 500:
        return BARE_500
    if status == 403:
        return ROLL
    if status == 401:
        try:
            return ROLL if json.loads(_peek_text(attempt)) == {'error': 'unauthorized'} else USABLE
        except ValueError:
            return USABLE
    if 300 <= status < 400:
        return ROLL
    return USABLE


def _peek_text(attempt):
    return attempt.peek().decode('utf-8', 'replace')


class _Slot:
    def __init__(self, entry, attempt):
        self.entry = entry
        self.attempt = attempt


class _Result:
    def __init__(self, slot, verdict=None, error=None):
        self.slot = slot
        self.verdict = verdict
        self.error = error


class _Race:
    """Results of the attempts of one turn. After finish(), late results are closed."""

    def __init__(self):
        self.results = queue.Queue()
        self.slots = []
        self._lock = threading.Lock()
        self._finished = False

    def deliver(self, result):
        with self._lock:
            if not self._finished:
                self.results.put(result)
                return
        result.slot.attempt.close()

    def finish(self, keep=None):
        """Stop the race: cancel every attempt but keep, and close what is already queued."""
        with self._lock:
            self._finished = True
        for slot in self.slots:
            if slot.attempt is not keep:
                slot.attempt.cancel()
        while True:
            try:
                result = self.results.get_nowait()
            except queue.Empty:
                return
            if result.slot.attempt is not keep:
                result.slot.attempt.close()


class _Turn:
    def __init__(self):
        self.last_status = None
        self.saw_bare_500 = False


class RelayRouter:
    def __init__(self, pool, *, health=None, proxy=None, hedge_grace=8.0, header_timeout=None,
                 stall_timeout=None, allow_insecure_for_tests=False, direct=True):
        self.pool = pool
        self.direct = direct
        self.health = health or HealthBook()
        self.proxy = proxy
        self.hedge_grace = hedge_grace
        self.header_timeout = header_timeout
        self.stall_timeout = stall_timeout
        self._insecure = allow_insecure_for_tests

    # -- public ------------------------------------------------------------------------

    def open(self, upstream_url, *, headers=None, body=None, method='POST', spread_key=None):
        """Return (attempt, route) for the first usable response, or the direct response."""
        parts = urlsplit(upstream_url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError('Use an HTTP or HTTPS upstream URL')
        call = (upstream_url, dict(headers or {}), body, method)
        candidates = ordered_candidates(self.pool, self.health, spread_key=spread_key)
        turn = _Turn()
        index = 0
        while index < len(candidates):
            if not self._startable(candidates[index]):
                index += 1
                continue
            winner, used = self._race(candidates, index, call, turn)
            index += used
            if winner is not None:
                return self._won(winner)
        if not self.direct:
            raise RelayExhaustedError('Relays exhausted and direct fallback is off', last_status=turn.last_status)
        return self._direct(call, turn)

    def report_failure(self, route, status=None):
        if route is not None and route.kind == 'relay':
            self.health.mark_failure(route.url, status)

    def report_success(self, route, elapsed_headers):
        if route is not None and route.kind == 'relay':
            self.health.mark_success(route.url, elapsed_headers)

    # -- candidates --------------------------------------------------------------------

    def _valid(self, entry):
        try:
            validate_relay_url(entry.url, allow_insecure=self._insecure)
            return True
        except ValueError as error:
            log.warning('relay %s skipped: %s', entry.label, error)
            return False

    def _startable(self, entry):
        """Valid and out of cooldown, checked now: ordering may have been computed earlier."""
        return self.health.is_healthy(entry.url) and self._valid(entry)

    def _make_attempt(self, url, method, headers, body):
        extra = {}
        if self.header_timeout is not None:
            extra['header_timeout'] = self.header_timeout
        if self.stall_timeout is not None:
            extra['stall_timeout'] = self.stall_timeout
        return RelayAttempt(url, method=method, headers=headers, body=body, proxy=self.proxy, **extra)

    @staticmethod
    def _relay_headers(entry, call):
        upstream_url, headers, _, _ = call
        parts = urlsplit(upstream_url)
        host = parts.netloc.rpartition('@')[2]
        path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
        sent = {name: value for name, value in headers.items()
                if name.lower() not in ('host', 'accept-encoding') and not name.lower().startswith('x-relay-')}
        sent['x-relay-target'] = f'{parts.scheme}://{host}'
        sent['x-relay-path'] = path
        if entry.auth:
            sent['x-relay-auth'] = entry.auth
        sent['Accept-Encoding'] = 'identity'
        return sent

    # -- one turn: a candidate, plus a hedge partner when it is slow --------------------

    def _start(self, race, entry, call):
        _, _, body, method = call
        slot = _Slot(entry, None)
        try:
            slot.attempt = self._make_attempt(entry.url, method, self._relay_headers(entry, call), body)
        except Exception as error:
            slot.attempt = _Dead()
            race.slots.append(slot)
            race.results.put(_Result(slot, error=error))
            return
        race.slots.append(slot)
        threading.Thread(target=self._run, args=(race, slot), daemon=True, name='bleo-relay').start()

    @staticmethod
    def _run(race, slot):
        try:
            slot.attempt.open()
            result = _Result(slot, verdict=classify(slot.attempt))
        except Exception as error:
            result = _Result(slot, error=error)
        race.deliver(result)

    def _race(self, candidates, index, call, turn):
        """Run candidates[index], hedging to the next one if it is slow. Returns
        (winning _Result or None, number of candidates used)."""
        race = _Race()
        first = candidates[index]
        can_hedge = (index < len(candidates) - 1 and self.health.is_healthy(first.url)
                     and self.health.is_healthy(candidates[index + 1].url))
        deadline = time.monotonic() + self.hedge_grace
        pending, used = 1, 1
        try:
            self._start(race, first, call)
            while pending:
                timeout = max(0.0, deadline - time.monotonic()) if can_hedge else None
                try:
                    result = race.results.get(timeout=timeout)
                except queue.Empty:
                    can_hedge = False
                    partner = candidates[index + 1]
                    if self.health.is_healthy(partner.url) and self._valid(partner):
                        log.info('relay %s slow to answer, hedging to %s', first.label, partner.label)
                        self._start(race, partner, call)
                        pending, used = pending + 1, 2
                    continue
                pending -= 1
                if self._judge(result, turn):
                    race.finish(keep=result.slot.attempt)
                    return result, used
        except BaseException:
            race.finish()
            raise
        race.finish()
        return None, used

    def _judge(self, result, turn):
        """Apply marks for one result. True when it wins the turn."""
        entry, attempt = result.slot.entry, result.slot.attempt
        if result.error is not None:
            self.health.mark_failure(entry.url, None, _describe(result.error))
            log.warning('relay %s failed: %s', entry.label, _describe(result.error))
            return False
        verdict, status = result.verdict, attempt.status
        if verdict == USABLE:
            return True
        if verdict == BARE_500:
            if turn.saw_bare_500:
                result.verdict = 'unmarked'
                return True
            turn.saw_bare_500 = True
        elif verdict == ROLL:
            self.health.mark_failure(entry.url, status)
        turn.last_status = status
        log.warning('relay %s answered %s, rolling', entry.label, status)
        attempt.close()
        return False

    def _won(self, result):
        entry, attempt = result.slot.entry, result.slot.attempt
        route = Route('relay', entry.url, entry.label)
        if result.verdict == USABLE:
            self.health.mark_success(entry.url, attempt.elapsed_headers)
            if self.pool.mode != 'spread' and self.pool.sticky_url != entry.url:
                try:
                    self.pool.set_sticky(entry.url)
                except (OSError, ValueError) as error:
                    log.debug('could not save sticky relay: %s', error)
        return attempt, route

    # -- direct ------------------------------------------------------------------------

    def _direct(self, call, turn):
        upstream_url, headers, body, method = call
        sent = {name: value for name, value in headers.items() if not name.lower().startswith('x-relay-')}
        attempt = self._make_attempt(upstream_url, method, sent, body)
        try:
            attempt.open()
        except Exception as error:
            attempt.close()
            raise RelayExhaustedError(f'Relays exhausted and direct request failed: {_describe(error)}',
                                      last_status=turn.last_status) from error
        return attempt, Route('direct', upstream_url, None)


class _Dead:
    """Stands in for an attempt that could not be built."""

    def cancel(self):
        pass

    close = cancel


def _describe(error):
    return str(error) or type(error).__name__
