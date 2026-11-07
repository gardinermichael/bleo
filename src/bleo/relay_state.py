"""Relay pool, health book and candidate ordering.

A stdlib port of pi-freeflow v1.44.1 relay-state.ts: the persisted pool of relay
deployments, the in-memory health book (cooldowns, escalation, EWMA of time to headers)
and the order in which relays are tried for one request.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import ipaddress
import json
import os
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit

MODES = ('auto', 'spread', 'off')
LOCK_STALE_SECONDS = 30.0

COOLDOWN_DEFAULT = 30.0
COOLDOWN_429 = 90.0
COOLDOWN_504 = 60.0
COOLDOWN_5XX = 45.0
COOLDOWN_402_DISABLED = 6 * 60 * 60.0
MAX_ESCALATION = 4

EWMA_ALPHA = 0.3
EWMA_FLOOR = 3
EWMA_MARGIN = 1.5
EWMA_MARGIN_RATIO = 0.25


def default_path():
    home = os.environ.get('BLEO_HOME') or os.path.join(os.path.expanduser('~'), '.bleo')
    return os.path.join(home, 'relays.json')


def normalize_mode(mode):
    mode = str(mode).strip().lower()
    if mode in ('sticky', 'on'):
        return 'auto'
    if mode not in MODES:
        raise ValueError(f'Unknown relay mode: {mode!r} (use auto, spread or off)')
    return mode


# --- URL validation --------------------------------------------------------------------

_NUMERIC_HOST = re.compile(r'^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+))*$')
_PRIVATE_SUFFIXES = ('.localhost', '.local', '.internal')


def _private_host(host):
    host = host.lower().rstrip('.')
    if not host or host == 'localhost' or host.endswith(_PRIVATE_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Octal, hex and integer spellings are resolved by getaddrinfo, so refuse them
        # rather than guess; a real DNS name never looks like this.
        return bool(_NUMERIC_HOST.match(host))
    mapped = getattr(address, 'ipv4_mapped', None)
    return not (mapped or address).is_global


def validate_relay_url(raw, *, allow_insecure=False):
    """Return the cleaned relay URL or raise ValueError. Never resolves DNS.

    allow_insecure is for tests only: it permits http, loopback hosts and any port.
    """
    url = (raw or '').strip()
    if not url:
        raise ValueError('Relay URL cannot be empty')
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise ValueError('Invalid relay URL') from error
    allowed = ('https', 'http') if allow_insecure else ('https',)
    if parts.scheme not in allowed:
        raise ValueError('Only https relay URLs are allowed')
    if not parts.hostname:
        raise ValueError('Relay URL has no host')
    if '@' in parts.netloc:
        raise ValueError('Credentials are not allowed in relay URLs')
    if allow_insecure:
        return url
    if _private_host(parts.hostname):
        raise ValueError('Private, loopback and link-local relay hosts are not allowed')
    if port not in (None, 443):
        raise ValueError('Non-default relay ports are not allowed')
    return url


def _is_vercel(url):
    try:
        return (urlsplit(url).hostname or '').lower().endswith('.vercel.app')
    except ValueError:
        return False


def _default_label(url):
    host = (urlsplit(url).hostname or url).lower()
    return host.split('.')[0] or host


# --- Pool ------------------------------------------------------------------------------

@dataclass(frozen=True)
class RelayEntry:
    url: str
    label: str
    auth: str = field(default=None, repr=False)
    added_at: str = ''


def _narrow_entry(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('url'), str) or not raw['url'].strip():
        return None
    url = raw['url'].strip()
    label = raw.get('label') if isinstance(raw.get('label'), str) and raw['label'].strip() else _default_label(url)
    auth = raw.get('auth') if isinstance(raw.get('auth'), str) and raw['auth'] else None
    added = raw.get('added_at') if isinstance(raw.get('added_at'), str) else ''
    return RelayEntry(url, label, auth, added)


class RelayPool:
    """The saved list of relays. Reloads when the file changes; saves atomically at 0600.

    Constructing a pool directly (with entries=) skips validation; only add() validates.
    """

    def __init__(self, path=None, *, entries=(), mode='auto', sticky_url=None):
        self.path = os.fspath(path) if path else default_path()
        self._lock = threading.RLock()
        self._entries = tuple(entries)
        self._mode = normalize_mode(mode)
        self._sticky = sticky_url
        self._mtime = self._disk_mtime()

    @classmethod
    def load(cls, path=None):
        pool = cls(path)
        with pool._lock:
            pool._reload()
        return pool

    # -- disk ---------------------------------------------------------------------------

    def _disk_mtime(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return -1

    def _reload(self):
        self._mtime = self._disk_mtime()
        if self._mtime == -1:
            return
        try:
            with open(self.path, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return  # unreadable or half-edited: keep what we hold
        if not isinstance(data, dict):
            return
        raw = data.get('relays')
        entries = [entry for entry in map(_narrow_entry, raw if isinstance(raw, list) else []) if entry]
        try:
            mode = normalize_mode(data.get('mode', 'auto'))
        except ValueError:
            mode = 'auto'
        url = data.get('url')
        self._entries = tuple(entries)
        self._mode = mode
        self._sticky = url.strip() if isinstance(url, str) and url.strip() else None

    def _refresh(self):
        with self._lock:
            if self._disk_mtime() != self._mtime:
                self._reload()

    @contextmanager
    def _file_lock(self):
        lock = self.path + '.lock'
        held = False
        os.makedirs(os.path.dirname(self.path) or '.', mode=0o700, exist_ok=True)
        for _ in range(2):
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                held = True
                break
            except FileExistsError:
                try:
                    stale = time.time() - os.stat(lock).st_mtime > LOCK_STALE_SECONDS
                    if stale:
                        os.unlink(lock)
                        continue
                except OSError:
                    continue
                break  # a live writer holds it: proceed unlocked, the replace is atomic
            except OSError:
                break
        try:
            yield
        finally:
            if held:
                try:
                    os.unlink(lock)
                except OSError:
                    pass

    def _write(self):
        directory = os.path.dirname(self.path) or '.'
        payload = {
            'version': 1,
            'mode': self._mode,
            'url': self._sticky or '',
            'relays': [{'url': e.url, 'label': e.label, 'auth': e.auth, 'added_at': e.added_at}
                       for e in self._entries],
        }
        fd, temp = tempfile.mkstemp(dir=directory, prefix='.relays-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        self._mtime = self._disk_mtime()

    def save(self):
        with self._lock, self._file_lock():
            self._write()

    def _mutate(self, change):
        """Apply change() to the freshest saved state, then save."""
        with self._lock, self._file_lock():
            if self._disk_mtime() != -1:
                self._reload()
            result = change()
            self._write()
            return result

    # -- reads --------------------------------------------------------------------------

    @property
    def entries(self):
        self._refresh()
        return self._entries

    @property
    def mode(self):
        self._refresh()
        return self._mode

    @property
    def sticky_url(self):
        self._refresh()
        urls = [entry.url for entry in self._entries]
        if self._sticky in urls:
            return self._sticky
        return urls[0] if urls else None

    # -- writes -------------------------------------------------------------------------

    def add(self, url, *, auth, label=None):
        clean = validate_relay_url(url)
        label = (label or '').strip() or None

        def change():
            current = list(self._entries)
            existing = next((e for e in current if e.url == clean), None)
            taken = {e.label.lower() for e in current if e is not existing}
            if label and label.lower() in taken:
                raise ValueError(f'Relay label already in use: {label}')
            chosen = label or (existing.label if existing else None)
            if chosen is None:
                base = chosen = _default_label(clean)
                number = 1
                while chosen.lower() in taken:
                    number += 1
                    chosen = f'{base}-{number}'
            stamp = existing.added_at if existing else time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            entry = RelayEntry(clean, chosen, auth or None, stamp)
            self._entries = tuple(entry if e is existing else e for e in current) if existing \
                else tuple(current) + (entry,)
            return entry

        return self._mutate(change)

    def remove(self, label_or_url):
        """Remove by exact URL or case-insensitive label. Returns the entry or None."""
        key = (label_or_url or '').strip()

        def change():
            found = next((e for e in self._entries if e.url == key or e.label.lower() == key.lower()), None)
            if found is None:
                return None
            self._entries = tuple(e for e in self._entries if e is not found)
            if self._sticky == found.url:
                self._sticky = self._entries[0].url if self._entries else None
            return found

        return self._mutate(change)

    def set_sticky(self, url):
        def change():
            if url not in [e.url for e in self._entries]:
                raise ValueError(f'Not a relay in the pool: {url}')
            self._sticky = url

        self._mutate(change)

    def set_mode(self, mode):
        mode = normalize_mode(mode)

        def change():
            self._mode = mode

        self._mutate(change)


# --- Health ----------------------------------------------------------------------------

@dataclass
class RelayHealth:
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    cooldown_until: float = 0.0
    last_status: int = None
    last_error: str = None
    last_latency: float = None
    success_count: int = 0
    failure_count: int = 0
    ewma: float = None
    ewma_samples: int = 0
    recovering: bool = False


def cooldown_for(status):
    if status == 429:
        return COOLDOWN_429
    if status == 504:
        return COOLDOWN_504
    if status == 402:
        return COOLDOWN_402_DISABLED
    if status and status >= 500:
        return COOLDOWN_5XX
    return COOLDOWN_DEFAULT


class HealthBook:
    """In-memory health per relay URL. Every mutation holds one lock; clock is injectable."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._records = {}
        self._cursor = 0

    def mark_failure(self, url, status=None, error=None):
        with self._lock:
            previous = self._records.get(url, RelayHealth())
            now = self.clock()
            consecutive = previous.consecutive_failures + 1
            self._records[url] = replace(
                previous,
                consecutive_failures=consecutive,
                last_failure_time=now,
                cooldown_until=now + cooldown_for(status) * min(MAX_ESCALATION, consecutive),
                last_status=status,
                last_error=error,
                failure_count=previous.failure_count + 1,
                recovering=True,
            )

    def mark_success(self, url, latency=None):
        """Reset failures. With a latency (seconds to headers) keep the record and update the
        EWMA; without one the record is dropped."""
        with self._lock:
            if latency is None:
                self._records.pop(url, None)
                return
            previous = self._records.get(url)
            ewma = latency if previous is None or previous.ewma is None \
                else EWMA_ALPHA * latency + (1 - EWMA_ALPHA) * previous.ewma
            base = previous or RelayHealth()
            self._records[url] = replace(
                base,
                consecutive_failures=0,
                last_failure_time=0.0,
                cooldown_until=0.0,
                last_latency=latency,
                success_count=base.success_count + 1,
                ewma=ewma,
                ewma_samples=base.ewma_samples + 1,
                recovering=False,
            )

    def is_healthy(self, url, now=None):
        with self._lock:
            record = self._records.get(url)
            if record is None:
                return True
            return (self.clock() if now is None else now) >= record.cooldown_until

    def get(self, url):
        with self._lock:
            record = self._records.get(url)
            return replace(record) if record else None

    def reset(self):
        with self._lock:
            self._records.clear()
            self._cursor = 0

    def next_cursor(self):
        with self._lock:
            value = self._cursor
            self._cursor += 1
            return value


# --- Ordering --------------------------------------------------------------------------

def fnv1a(text):
    """32-bit FNV-1a over UTF-16 code units, as the upstream stableHash does."""
    value = 2166136261
    data = text.encode('utf-16-le')
    for index in range(0, len(data), 2):
        value ^= data[index] | (data[index + 1] << 8)
        value = (value * 16777619) & 0xFFFFFFFF
    return value


def _challenger(band, health):
    """The sticky head keeps its lead unless a measured, non-recovering challenger beats its
    EWMA by more than max(1.5 s, 25% of it). Measured relays sort ahead of unmeasured."""
    if len(band) <= 1:
        return band
    head, rest = band[0], band[1:]
    records = {entry.url: health.get(entry.url) for entry in band}
    measured = [e for e in rest if records[e.url] and records[e.url].ewma_samples >= EWMA_FLOOR]
    unknown = [e for e in rest if e not in measured]
    head_record = records[head.url]
    if head_record is not None and head_record.ewma is not None:
        best = None
        for entry in measured:
            record = records[entry.url]
            if record.recovering or record.ewma is None:
                continue
            if best is None or record.ewma < records[best.url].ewma:
                best = entry
        if best is not None:
            margin = max(EWMA_MARGIN, EWMA_MARGIN_RATIO * head_record.ewma)
            if head_record.ewma - records[best.url].ewma > margin:
                return [best] + [e for e in band if e is not best]
    return [head] + measured + unknown


def ordered_candidates(pool, health, *, spread_key=None, now=None):
    """Relays to try for one request, as RelayEntry objects. Empty when mode is off."""
    entries = list(pool.entries)
    if pool.mode == 'off' or not entries:
        return []
    now = health.clock() if now is None else now
    sticky = pool.sticky_url
    start = next((i for i, e in enumerate(entries) if e.url == sticky), 0)
    rotated = entries[start:] + entries[:start]
    healthy = [e for e in rotated if health.is_healthy(e.url, now)]
    cooling = [e for e in rotated if not health.is_healthy(e.url, now)]
    ordered = (_challenger([e for e in healthy if not _is_vercel(e.url)], health)
               + [e for e in healthy if _is_vercel(e.url)]
               + [e for e in cooling if not _is_vercel(e.url)]
               + [e for e in cooling if _is_vercel(e.url)])
    if pool.mode != 'spread':
        return ordered
    up = [e for e in ordered if health.is_healthy(e.url, now)]
    down = [e for e in ordered if not health.is_healthy(e.url, now)]
    if len(up) <= 1:
        return up + down
    shift = (fnv1a(spread_key) if spread_key else health.next_cursor()) % len(up)
    return up[shift:] + up[:shift] + down
