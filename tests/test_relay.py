from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import bleo
from bleo._http import Attempt
from bleo.exceptions import BleoError, GatewayExhaustedError, RelayExhaustedError
from bleo.relay import RelayAttempt, RelayRouter, Route
from bleo.relay_state import HealthBook, RelayEntry, RelayPool, fnv1a

GRACE = 0.15
PATH = '/v1/chat?x=1&y=2'


class Hit:
    def __init__(self, method, path, headers, body):
        self.method, self.path, self.body = method, path, body
        self.headers = {name.lower(): value for name, value in headers.items()}
        self.at = time.monotonic()


class Server:
    """Local HTTP server (a fake relay or upstream) whose handler is function(handler, server)."""

    def __init__(self, behavior):
        self.hits = []
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def handle_any(self):
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                owner.hits.append(Hit(self.command, self.path, self.headers, body))
                behavior(self, owner)

            do_GET = do_POST = handle_any

        class Quiet(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self.httpd = Quiet(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_port}'
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def stop(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


def reply(status=200, body=b'ok', headers=None, delay=0, content_type='application/json'):
    def behavior(handler, server):
        if delay:
            server.release.wait(delay)
        try:
            handler.send_response(status)
            for name, value in {'Content-Type': content_type, **(headers or {})}.items():
                handler.send_header(name, value)
            handler.send_header('Content-Length', str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            handler.wfile.flush()
        except OSError:
            pass
    return behavior


def drop(handler, server):
    handler.close_connection = True


def hang(handler, server):
    server.release.wait(5)


class RouterCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, 'relays.json')
        self.health = HealthBook()
        self.relays = []
        self.upstream = self.serve(reply(body=b'direct'))
        self.start = time.monotonic()

    def serve(self, behavior):
        server = Server(behavior)
        self.addCleanup(server.stop)
        return server

    def build(self, *behaviors, mode='auto', grace=GRACE, router_class=RelayRouter, **kwargs):
        self.relays = [self.serve(behavior) for behavior in behaviors]
        entries = [RelayEntry(server.url, f'r{i}', f'secret-r{i}', '') for i, server in enumerate(self.relays)]
        self.pool = RelayPool(self.path, entries=entries, mode=mode)
        self.router = router_class(self.pool, health=self.health, hedge_grace=grace,
                                   allow_insecure_for_tests=True, **kwargs)
        return self.router

    def go(self, headers=None, body=b'{"q":1}', read=True, **kwargs):
        self.start = time.monotonic()
        attempt, route = self.router.open('%s%s' % (self.upstream.url, PATH),
                                          headers={'Authorization': 'Bearer caller-token', **(headers or {})},
                                          body=body, **kwargs)
        self.addCleanup(attempt.close)
        return attempt, route, (attempt.read_all() if read else None)

    def cooldown(self, index):
        record = self.health.get(self.relays[index].url)
        return None if record is None else record.cooldown_until - self.health.clock()

    def assertMarked(self, index, seconds):
        remaining = self.cooldown(index)
        self.assertIsNotNone(remaining, f'r{index} was not marked')
        self.assertAlmostEqual(remaining, seconds, delta=3)

    def assertNotMarked(self, index):
        record = self.health.get(self.relays[index].url)
        self.assertTrue(record is None or record.failure_count == 0, f'r{index} was marked')

    def hits(self, index):
        return len(self.relays[index].hits)


# --- roll rules -------------------------------------------------------------------------

class RollRuleTests(RouterCase):
    def rolls(self, behavior, *, marked=None):
        """r0 answers `behavior`; the router must roll to r1 and mark r0 for `marked` seconds."""
        self.build(behavior, reply(body=b'second'))
        attempt, route, body = self.go()
        self.assertEqual((route.kind, route.label, attempt.status, body), ('relay', 'r1', 200, b'second'))
        self.assertEqual(self.hits(0), 1)
        self.assertEqual(self.hits(1), 1)
        if marked is None:
            self.assertNotMarked(0)
        else:
            self.assertMarked(0, marked)
        self.assertEqual(self.upstream.hits, [])

    def test_roll_408(self):
        self.rolls(reply(408), marked=30)

    def test_roll_429(self):
        self.rolls(reply(429), marked=90)

    def test_roll_502(self):
        self.rolls(reply(502), marked=45)

    def test_roll_503(self):
        self.rolls(reply(503), marked=45)

    def test_roll_504(self):
        self.rolls(reply(504), marked=60)

    def test_roll_cloudflare_520_to_530(self):
        for status in range(520, 531):
            with self.subTest(status=status):
                self.health.reset()
                self.rolls(reply(status), marked=45)

    def test_roll_413_without_a_mark(self):
        self.rolls(reply(413), marked=None)

    def test_roll_edge_404_with_x_vercel_error(self):
        self.rolls(reply(404, headers={'x-vercel-error': 'DEPLOYMENT_NOT_FOUND'}), marked=30)

    def test_roll_edge_404_with_a_non_json_body(self):
        self.rolls(reply(404, b'<html>not found</html>', content_type='text/html'), marked=30)

    def test_json_404_is_the_upstreams_answer_and_is_returned(self):
        self.build(reply(404, b'{"error":"no such model"}'), reply(body=b'second'))
        attempt, route, body = self.go()
        self.assertEqual((route.label, attempt.status, body), ('r0', 404, b'{"error":"no such model"}'))
        self.assertEqual(self.hits(1), 0)
        self.assertNotMarked(0)

    def test_roll_402_with_x_vercel_error_marks_six_hours(self):
        self.rolls(reply(402, b'{}', headers={'x-vercel-error': 'DEPLOYMENT_DISABLED'}), marked=6 * 3600)

    def test_roll_402_with_a_deployment_disabled_body_marks_six_hours(self):
        self.rolls(reply(402, b'{"error":{"code":"DEPLOYMENT_DISABLED"}}'), marked=6 * 3600)

    def test_generic_402_is_returned_to_the_caller_intact(self):
        payload = b'{"error":"payment required: quota"}'
        self.build(reply(402, payload), reply(body=b'second'))
        attempt, route, body = self.go()
        self.assertEqual((route.label, attempt.status, body), ('r0', 402, payload))
        self.assertEqual(self.hits(1), 0)
        self.assertNotMarked(0)

    def test_returned_402_body_survives_the_peek_for_streaming_readers(self):
        payload = b'{"error":"quota"}\n{"more":1}'
        self.build(reply(402, payload))
        attempt, _, _ = self.go(read=False)
        self.assertEqual(list(attempt.iter_lines()), ['{"error":"quota"}', '{"more":1}'])

    def test_bare_500_rolls_once_without_a_mark(self):
        self.rolls(reply(500, b'boom'), marked=None)

    def test_second_500_is_returned_unmarked(self):
        self.build(reply(500, b'first'), reply(500, b'second'), reply(body=b'third'))
        attempt, route, body = self.go()
        self.assertEqual((route.label, attempt.status, body), ('r1', 500, b'second'))
        self.assertEqual(self.hits(2), 0)
        self.assertNotMarked(0)
        self.assertNotMarked(1)
        self.assertIsNone(self.health.get(self.relays[1].url))  # not even a success

    def test_bare_500_allowance_is_per_turn(self):
        self.build(reply(500), reply(body=b'ok'))
        self.go()
        self.build(reply(500), reply(body=b'ok'))
        attempt, route, _ = self.go()
        self.assertEqual((route.label, attempt.status), ('r1', 200))

    def test_roll_403(self):
        self.rolls(reply(403, b'{"error":"blocked"}'), marked=30)

    def test_roll_401_unauthorized_from_the_relay_itself(self):
        self.rolls(reply(401, b'{"error":"unauthorized"}'), marked=30)

    def test_other_401_bodies_are_upstream_verdicts_and_are_returned(self):
        for payload in (b'{"error":"bad key"}', b'{"error":"unauthorized","detail":"x"}', b'not json', b'[]', b''):
            with self.subTest(payload=payload):
                self.health.reset()
                self.build(reply(401, payload), reply(body=b'second'))
                attempt, route, body = self.go()
                self.assertEqual((route.label, attempt.status, body), ('r0', 401, payload))
                self.assertNotMarked(0)
                self.assertEqual(self.hits(1), 0)

    def test_roll_3xx_and_never_follow_the_redirect(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.health.reset()
                target = self.serve(reply(body=b'redirected'))
                self.build(reply(status, b'', headers={'Location': target.url + '/elsewhere'}),
                           reply(body=b'second'))
                attempt, route, body = self.go()
                self.assertEqual((route.label, body), ('r1', b'second'))
                self.assertMarked(0, 30)
                self.assertEqual(target.hits, [])

    def test_roll_on_a_dropped_connection(self):
        self.rolls(drop, marked=30)

    def test_roll_on_connection_refused(self):
        dead = Server(reply())
        dead.stop()
        self.build(reply(), reply(body=b'second'))
        entries = [RelayEntry(dead.url, 'dead', 's', '')] + list(self.pool.entries)
        self.router.pool = RelayPool(self.path, entries=entries)
        attempt, route, body = self.go()
        self.assertEqual((route.label, body), ('r0', b'ok'))
        record = self.health.get(dead.url)
        self.assertEqual((record.failure_count, record.last_status), (1, None))
        self.assertAlmostEqual(record.cooldown_until - self.health.clock(), 30, delta=3)

    def test_roll_on_header_timeout(self):
        self.build(hang, reply(body=b'second'), grace=5, header_timeout=0.3)
        attempt, route, body = self.go()
        self.assertEqual((route.label, body), ('r1', b'second'))
        self.assertMarked(0, 30)
        self.assertGreaterEqual(time.monotonic() - self.start, 0.25)
        self.assertLess(time.monotonic() - self.start, 2)

    def test_usable_statuses_are_returned_and_counted_as_success(self):
        for status in (200, 201, 204, 400, 418, 422, 499):
            with self.subTest(status=status):
                self.health.reset()
                self.build(reply(status, b'' if status == 204 else b'{"a":1}'), reply(body=b'second'))
                attempt, route, _ = self.go()
                self.assertEqual((route.label, attempt.status), ('r0', status))
                record = self.health.get(self.relays[0].url)
                self.assertEqual((record.success_count, record.ewma_samples, record.consecutive_failures), (1, 1, 0))
                self.assertAlmostEqual(record.ewma, attempt.elapsed_headers)

    def test_rolled_response_is_closed(self):
        self.build(reply(503), reply(body=b'second'), router_class=Recording)
        self.go()
        self.assertTrue(self.router.attempts[0].closed_or_cancelled())


class Recording(RelayRouter):
    """Keeps every attempt it builds so tests can see what became of them."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attempts = []

    def _make_attempt(self, url, method, headers, body):
        attempt = super()._make_attempt(url, method, headers, body)
        attempt.closed_or_cancelled = lambda a=attempt: a._closed or a._cancelled
        self.attempts.append(attempt)
        return attempt


# --- request shape ----------------------------------------------------------------------

class RequestShapeTests(RouterCase):
    def test_relayed_request_carries_the_relay_headers(self):
        self.build(reply())
        self.go(headers={'Accept-Encoding': 'gzip', 'X-Relay-Auth': 'forged', 'x-relay-target': 'http://evil',
                         'Host': 'wrong.example.com', 'X-Custom': 'kept'})
        hit = self.relays[0].hits[0]
        host = self.upstream.url.removeprefix('http://')
        self.assertEqual((hit.method, hit.path, hit.body), ('POST', '/', b'{"q":1}'))
        self.assertEqual(hit.headers['x-relay-target'], 'http://' + host)
        self.assertEqual(hit.headers['x-relay-path'], PATH)
        self.assertEqual(hit.headers['x-relay-auth'], 'secret-r0')
        self.assertEqual(hit.headers['accept-encoding'], 'identity')
        self.assertEqual(hit.headers['authorization'], 'Bearer caller-token')
        self.assertEqual(hit.headers['x-custom'], 'kept')
        self.assertNotEqual(hit.headers['host'], 'wrong.example.com')

    def test_method_is_kept(self):
        self.build(reply())
        self.router.open(self.upstream.url + '/models', method='GET')[0].close()
        self.assertEqual(self.relays[0].hits[0].method, 'GET')

    def test_entry_without_auth_sends_no_x_relay_auth(self):
        self.build(reply())
        self.router.pool = RelayPool(self.path, entries=[RelayEntry(self.relays[0].url, 'r0', None, '')])
        self.go()
        self.assertNotIn('x-relay-auth', self.relays[0].hits[0].headers)

    def test_each_relay_gets_its_own_secret(self):
        self.build(reply(503), reply())
        self.go()
        self.assertEqual([self.relays[i].hits[0].headers['x-relay-auth'] for i in (0, 1)],
                         ['secret-r0', 'secret-r1'])

    def test_non_443_or_http_relay_is_refused_without_the_test_flag(self):
        self.build(reply(body=b'relay'))
        strict = RelayRouter(self.pool, health=self.health)
        attempt, route = strict.open(self.upstream.url + PATH, headers={}, body=b'{}')
        self.addCleanup(attempt.close)
        self.assertEqual((route.kind, attempt.read_all()), ('direct', b'direct'))
        self.assertEqual(self.relays[0].hits, [])


# --- direct fallback --------------------------------------------------------------------

class DirectTests(RouterCase):
    def assertCleanDirect(self):
        self.assertEqual(len(self.upstream.hits), 1)
        hit = self.upstream.hits[0]
        self.assertFalse([name for name in hit.headers if name.startswith('x-relay')], hit.headers)
        self.assertNotIn('secret-r', str(hit.headers) + hit.body.decode() + hit.path)
        self.assertEqual((hit.path, hit.body, hit.headers['authorization']), (PATH, b'{"q":1}', 'Bearer caller-token'))

    def test_exhausted_pool_falls_back_to_one_clean_direct_request(self):
        self.build(reply(503), reply(429), drop)
        attempt, route, body = self.go(headers={'x-relay-target': 'http://evil', 'X-Relay-Path': '/x'})
        self.assertEqual((route, body), (Route('direct', self.upstream.url + PATH, None), b'direct'))
        self.assertCleanDirect()

    def test_mode_off_goes_direct_without_touching_a_relay(self):
        self.build(reply(), mode='off')
        attempt, route, body = self.go()
        self.assertEqual((route.kind, body, self.hits(0)), ('direct', b'direct', 0))
        self.assertCleanDirect()

    def test_empty_pool_goes_direct(self):
        self.build()
        attempt, route, body = self.go()
        self.assertEqual((route.kind, route.label, body), ('direct', None, b'direct'))
        self.assertCleanDirect()

    def test_direct_result_is_returned_whatever_its_status(self):
        for status in (200, 403, 429, 500):
            with self.subTest(status=status):
                self.upstream.hits.clear()
                self.upstream = self.serve(reply(status, b'verdict'))
                self.build(reply(503))
                attempt, route, body = self.go()
                self.assertEqual((route.kind, attempt.status, body), ('direct', status, b'verdict'))

    def test_direct_failure_raises_relay_exhausted_with_the_last_relay_status(self):
        self.upstream = self.serve(drop)
        self.build(reply(429), reply(503))
        with self.assertRaises(RelayExhaustedError) as caught:
            self.go()
        self.assertEqual(caught.exception.last_status, 503)
        self.assertIsInstance(caught.exception.__cause__, Exception)

    def test_direct_failure_without_any_relay_response_has_no_status(self):
        self.upstream = self.serve(drop)
        self.build()
        with self.assertRaises(RelayExhaustedError) as caught:
            self.go()
        self.assertIsNone(caught.exception.last_status)

    def test_error_text_never_contains_a_relay_secret(self):
        self.upstream = self.serve(drop)
        self.build(reply(503))
        with self.assertRaises(RelayExhaustedError) as caught:
            self.go()
        self.assertNotIn('secret', str(caught.exception))

    def test_alias_and_exports(self):
        self.assertIs(GatewayExhaustedError, RelayExhaustedError)
        self.assertIs(bleo.GatewayExhaustedError, bleo.RelayExhaustedError)
        self.assertTrue(issubclass(RelayExhaustedError, BleoError))
        self.assertIn('RelayExhaustedError', bleo.__all__)
        self.assertIn('GatewayExhaustedError', bleo.__all__)

    def test_gateway_module_is_gone(self):
        with self.assertRaises(ImportError):
            __import__('bleo.gateway')

    def test_bad_upstream_url_is_refused_before_anything_is_sent(self):
        self.build(reply())
        with self.assertRaises(ValueError):
            self.router.open('ftp://example.com/x')
        self.assertEqual(self.hits(0), 0)


# --- hedging ----------------------------------------------------------------------------

class HedgeTests(RouterCase):
    def test_hedge_fires_at_the_grace_and_the_fast_neighbour_wins(self):
        self.build(reply(delay=1.0, body=b'slow'), reply(body=b'fast'), grace=0.25)
        attempt, route, body = self.go()
        elapsed = time.monotonic() - self.start
        self.assertEqual((route.label, body), ('r1', b'fast'))
        started = self.relays[1].hits[0].at - self.start
        self.assertGreaterEqual(started, 0.22)
        self.assertLess(started, 0.7)
        self.assertLess(elapsed, 0.8)
        self.assertEqual(self.upstream.hits, [])

    def test_no_hedge_when_the_first_answers_within_the_grace(self):
        self.build(reply(delay=0.05), reply(), grace=0.5)
        self.go()
        self.assertEqual((self.hits(0), self.hits(1)), (1, 0))

    def test_no_hedge_when_the_neighbour_is_cooling(self):
        self.build(reply(delay=0.5, body=b'slow'), reply(body=b'fast'))
        self.health.mark_failure(self.relays[1].url, 503)
        attempt, route, body = self.go()
        self.assertEqual((route.label, body), ('r0', b'slow'))
        self.assertEqual(self.hits(1), 0)

    def test_no_hedge_when_the_candidate_is_the_last(self):
        self.build(reply(delay=0.5, body=b'slow'))
        attempt, route, body = self.go()
        self.assertEqual((route.label, body), ('r0', b'slow'))
        self.assertEqual((self.hits(0), len(self.upstream.hits)), (1, 0))

    def test_no_hedge_to_a_neighbour_when_second_to_last_has_none_after_it(self):
        self.build(reply(503), reply(delay=0.5, body=b'slow'))
        attempt, route, body = self.go()
        self.assertEqual((route.label, body, self.hits(1)), ('r1', b'slow', 1))

    def test_fast_429_from_the_hedge_does_not_beat_a_slow_200(self):
        self.upstream = self.serve(drop)  # direct is broken: only the slow relay can answer
        self.build(reply(delay=0.6, body=b'slow'), reply(429))
        attempt, route, body = self.go()
        self.assertEqual((route.label, attempt.status, body), ('r0', 200, b'slow'))
        self.assertMarked(1, 90)
        self.assertNotMarked(0)
        self.assertEqual(self.upstream.hits, [])

    def test_fast_503_from_the_hedge_does_not_beat_a_slow_200(self):
        self.build(reply(delay=0.6, body=b'slow'), reply(503))
        attempt, route, body = self.go()
        self.assertEqual((route.label, attempt.status, body), ('r0', 200, b'slow'))
        self.assertMarked(1, 45)
        self.assertNotMarked(0)
        self.assertEqual(self.upstream.hits, [])

    def test_fast_429_and_fast_503_with_direct_broken_still_end_in_the_slow_relay(self):
        self.upstream = self.serve(drop)
        self.build(reply(delay=0.6, body=b'slow'), reply(429), reply(503))
        attempt, route, body = self.go()
        self.assertEqual((route.label, body), ('r0', b'slow'))
        self.assertEqual(self.upstream.hits, [])
        self.assertEqual(self.hits(2), 0)

    def test_primary_that_fails_after_the_hedge_started_loses_to_the_hedge(self):
        self.build(reply(429, delay=0.35), reply(delay=0.5, body=b'hedge'))
        attempt, route, body = self.go()
        self.assertEqual((route.label, body), ('r1', b'hedge'))
        self.assertMarked(0, 90)
        self.assertNotMarked(1)

    def test_both_failing_continues_down_the_list(self):
        self.build(reply(503, delay=0.3), reply(429), reply(body=b'third'), reply(body=b'fourth'))
        attempt, route, body = self.go()
        self.assertEqual((route.label, body), ('r2', b'third'))
        self.assertEqual([self.hits(i) for i in range(4)], [1, 1, 1, 0])
        self.assertMarked(0, 45)
        self.assertMarked(1, 90)

    def test_both_failing_with_nothing_left_goes_direct(self):
        self.build(reply(503, delay=0.3), reply(429))
        attempt, route, body = self.go()
        self.assertEqual((route.kind, body), ('direct', b'direct'))

    def test_hedge_after_both_failed_does_not_reuse_the_hedge_partner(self):
        self.build(reply(503, delay=0.3), reply(503), reply(503), reply(body=b'fourth'))
        attempt, route, _ = self.go()
        self.assertEqual(route.label, 'r3')
        self.assertEqual([self.hits(i) for i in range(4)], [1, 1, 1, 1])

    def test_at_most_two_attempts_are_in_flight(self):
        self.build(reply(503, delay=0.4), reply(503, delay=0.4), reply(body=b'third'), grace=0.1)
        attempt, route, body = self.go()
        self.assertEqual(route.label, 'r2')
        # r1 started near 0.1 and failed near 0.5; r2 must not have started at 0.2.
        self.assertGreaterEqual(self.relays[2].hits[0].at - self.start, 0.45)

    def test_loser_is_cancelled_and_not_marked(self):
        self.build(hang, reply(body=b'fast'), router_class=Recording)
        attempt, route, body = self.go()
        self.assertEqual((route.label, body), ('r1', b'fast'))
        loser, winner = self.router.attempts
        self.assertTrue(loser.cancelled)
        self.assertFalse(winner.cancelled)
        self.assertIsNone(self.health.get(self.relays[0].url))
        self.assertEqual(self.health.get(self.relays[1].url).success_count, 1)

    def test_winner_stays_readable_after_the_loser_is_cancelled(self):
        payload = b'x' * 200000
        self.build(hang, reply(body=payload))
        attempt, route, body = self.go()
        self.assertEqual(body, payload)

    def test_primary_winning_cancels_the_hedge_partner(self):
        self.build(reply(delay=0.35, body=b'primary'), hang, router_class=Recording)
        attempt, route, body = self.go()
        self.assertEqual((route.label, body), ('r0', b'primary'))
        self.assertEqual(self.hits(1), 1)
        self.assertTrue(self.router.attempts[1].cancelled)
        self.assertIsNone(self.health.get(self.relays[1].url))

    def test_late_response_from_the_loser_is_closed(self):
        gate = threading.Event()
        built = {}

        class Fake:
            def __init__(self, url, status, wait=None):
                self.url, self.status, self.wait = url, status, wait
                self.headers, self.elapsed_headers = Message(), 0.01
                self.cancelled = self.closed = False
                self.done = threading.Event()

            def open(self):
                if self.wait:
                    self.wait.wait(5)  # deaf to cancel: the response lands after the race
                self.done.set()

            def cancel(self):
                self.cancelled = True

            def close(self):
                self.closed = True

            def peek(self):
                return b''

        class Router(RelayRouter):
            def _make_attempt(self, url, method, headers, body):
                built[url] = Fake(url, 200, gate if url == self.pool.entries[0].url else None)
                return built[url]

        self.build(hang, reply(), router_class=Router)
        attempt, route = self.router.open(self.upstream.url + PATH, headers={}, body=b'{}')
        self.assertEqual(route.label, 'r1')
        loser = built[self.relays[0].url]
        self.assertTrue(loser.cancelled)
        self.assertFalse(loser.closed)
        gate.set()
        self.assertTrue(loser.done.wait(2))
        deadline = time.monotonic() + 2
        while not loser.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(loser.closed)
        self.assertIsNone(self.health.get(self.relays[0].url))
        self.assertEqual(self.pool.sticky_url, self.relays[1].url)

    def test_hedge_winner_becomes_sticky(self):
        self.build(hang, reply(), grace=GRACE)
        self.go()
        self.assertEqual(self.pool.sticky_url, self.relays[1].url)

    def test_hedge_threads_do_not_outlive_the_turn(self):
        self.build(hang, reply(body=b'ok'))
        self.go()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not [t for t in threading.enumerate() if t.name == 'bleo-relay']:
                break
            time.sleep(0.02)
        self.assertEqual([t for t in threading.enumerate() if t.name == 'bleo-relay'], [])


# --- sticky, spread, reports ------------------------------------------------------------

class StickyTests(RouterCase):
    def saved_sticky(self):
        return RelayPool.load(self.path).sticky_url

    def test_winner_after_a_roll_becomes_sticky_and_is_saved(self):
        self.build(reply(503), reply(), reply())
        self.go()
        self.assertEqual(self.pool.sticky_url, self.relays[1].url)
        self.assertEqual(self.saved_sticky(), self.relays[1].url)
        self.go()
        self.assertEqual([self.hits(0), self.hits(1)], [1, 2])  # next turn starts at r1

    def test_unchanged_winner_does_not_rewrite_the_file(self):
        self.build(reply(), reply())
        with patch.object(self.pool, 'set_sticky') as set_sticky:
            self.go()
        set_sticky.assert_not_called()
        self.assertFalse(os.path.exists(self.path))

    def test_spread_mode_never_rewrites_sticky(self):
        self.build(reply(503), reply(), reply(), mode='spread')
        with patch.object(self.pool, 'set_sticky') as set_sticky:
            attempt, route, _ = self.go(spread_key='k')
        self.assertEqual(route.label, 'r1')
        set_sticky.assert_not_called()
        self.assertFalse(os.path.exists(self.path))

    def test_failure_to_save_sticky_does_not_fail_the_request(self):
        self.build(reply(503), reply())
        with patch.object(self.pool, 'set_sticky', side_effect=OSError('read-only')):
            attempt, route, body = self.go()
        self.assertEqual(route.label, 'r1')

    def test_direct_fallback_leaves_sticky_alone(self):
        self.build(reply(503), reply(503))
        with patch.object(self.pool, 'set_sticky') as set_sticky:
            self.go()
        set_sticky.assert_not_called()

    def test_ewma_promotion_moves_sticky_when_the_challenger_serves(self):
        self.build(reply(), reply())
        for _ in range(3):
            self.health.mark_success(self.relays[0].url, 10)
            self.health.mark_success(self.relays[1].url, 1)
        attempt, route, _ = self.go()
        self.assertEqual(route.label, 'r1')
        self.assertEqual(self.pool.sticky_url, self.relays[1].url)


class SpreadTests(RouterCase):
    def test_spread_key_picks_the_relay(self):
        self.build(reply(), reply(), reply(), mode='spread')
        key = next(k for k in map(str, range(100)) if fnv1a(k) % 3 == 2)
        attempt, route, _ = self.go(spread_key=key)
        self.assertEqual(route.label, 'r2')

    def test_spread_without_a_key_round_robins(self):
        self.build(reply(), reply(), mode='spread')
        labels = [self.go()[1].label for _ in range(4)]
        self.assertEqual(labels, ['r0', 'r1', 'r0', 'r1'])


class ReportTests(RouterCase):
    def test_report_failure_marks_a_relay_route(self):
        self.build(reply())
        _, route, _ = self.go()
        self.router.report_failure(route, 429)
        self.assertMarked(0, 90)
        self.router.report_failure(route)
        self.assertMarked(0, 60)  # 30 s doubled by the second consecutive failure

    def test_report_success_records_latency_and_clears_the_mark(self):
        self.build(reply())
        _, route, _ = self.go()
        self.router.report_failure(route, 503)
        self.router.report_success(route, 0.5)
        record = self.health.get(self.relays[0].url)
        self.assertEqual((record.consecutive_failures, record.cooldown_until, record.last_latency), (0, 0.0, 0.5))

    def test_reports_about_a_direct_route_are_ignored(self):
        self.build(reply(503))
        _, route, _ = self.go()
        self.assertEqual(route.kind, 'direct')
        self.router.report_failure(route, 500)
        self.router.report_success(route, 0.1)
        self.assertEqual(self.health.get(self.upstream.url), None)

    def test_route_is_frozen(self):
        with self.assertRaises(Exception):
            Route('direct', 'x').kind = 'relay'

    def test_attempt_is_a_real_attempt(self):
        self.build(reply())
        attempt, _, _ = self.go()
        self.assertIsInstance(attempt, Attempt)
        self.assertIsInstance(attempt, RelayAttempt)

    def test_concurrent_turns_share_one_health_book_safely(self):
        self.build(reply(503), reply())
        results = []

        def turn():
            attempt, route = self.router.open(self.upstream.url + PATH, headers={}, body=b'{}')
            results.append((route.label, attempt.read_all()))
            attempt.close()

        threads = [threading.Thread(target=turn) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(label in ('r0', 'r1') for label, _ in results))


class CooldownSkipTests(RouterCase):
    def cool(self, index, status=402):
        self.health.mark_failure(self.relays[index].url, status)

    def test_a_relay_in_cooldown_is_never_contacted_and_the_request_goes_direct(self):
        self.build(reply(body=b'relay'))
        self.cool(0)  # 402: six hours
        attempt, route, body = self.go()
        self.assertEqual((route.kind, body), ('direct', b'direct'))
        self.assertEqual(self.hits(0), 0)

    def test_all_relays_cooling_with_direct_off_raises_without_a_request(self):
        self.build(reply(), reply(), direct=False)
        self.cool(0)
        self.cool(1)
        with self.assertRaises(RelayExhaustedError):
            self.go()
        self.assertEqual((self.hits(0), self.hits(1)), (0, 0))
        self.assertEqual(self.upstream.hits, [])

    def test_a_cooling_relay_behind_a_failing_one_is_not_tried_after_it(self):
        self.build(reply(503), reply(body=b'relay'))
        self.cool(1)
        _, route, body = self.go()
        self.assertEqual((route.kind, body), ('direct', b'direct'))
        self.assertEqual((self.hits(0), self.hits(1)), (1, 0))

    def test_hedge_never_starts_a_cooling_partner(self):
        self.build(reply(delay=0.6, body=b'slow'), reply(body=b'fast'))
        self.cool(1)
        _, route, body = self.go()
        self.assertEqual((route.label, body), ('r0', b'slow'))
        self.assertEqual(self.hits(1), 0)

    def test_candidate_that_cooled_after_ordering_is_skipped(self):
        self.build(reply(503), reply(body=b'second'), reply(body=b'third'))
        real = self.health.mark_failure

        def mark(url, status=None, error=None):
            real(url, status, error)
            if url == self.relays[0].url:
                real(self.relays[1].url, 402)  # another request cools r1 while this one runs

        self.health.mark_failure = mark
        _, route, body = self.go()
        self.assertEqual((route.label, body), ('r2', b'third'))
        self.assertEqual(self.hits(1), 0)

    def test_cooldown_that_has_expired_is_tried_again(self):
        self.build(reply(body=b'relay'))
        self.cool(0)
        self.health.clock = lambda: time.monotonic() + 7 * 3600
        _, route, body = self.go()
        self.assertEqual((route.kind, body), ('relay', b'relay'))



class EnvironmentProxyTests(RouterCase):
    URL = 'https://brave.example.com/x'

    def proxy_host(self, router, env, bypass=False):
        with patch('bleo.relay.getproxies', return_value=env), \
                patch('bleo.relay.proxy_bypass', return_value=bypass):
            attempt = router._make_attempt(self.URL, 'POST', {}, b'')
        self.addCleanup(attempt.close)
        return attempt._conn.host

    def test_uses_the_environment_proxy_when_none_is_given(self):
        router = self.build()
        self.assertEqual(self.proxy_host(router, {'https': 'http://corp-proxy:3128'}), 'corp-proxy')

    def test_no_proxy_bypass_goes_direct(self):
        router = self.build()
        self.assertEqual(self.proxy_host(router, {'https': 'http://corp-proxy:3128'}, bypass=True),
                         'brave.example.com')

    def test_an_explicit_proxy_wins_over_the_environment(self):
        router = self.build(proxy='http://mine:8080')
        self.assertEqual(self.proxy_host(router, {'https': 'http://corp-proxy:3128'}), 'mine')

    def test_an_unsupported_environment_proxy_is_ignored(self):
        router = self.build()
        with self.assertLogs('bleo.relay', 'WARNING'):
            host = self.proxy_host(router, {'https': 'socks5://corp-proxy:1080'})
        self.assertEqual(host, 'brave.example.com')


if __name__ == '__main__':
    unittest.main()
