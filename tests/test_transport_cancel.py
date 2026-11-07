import errno
import gzip
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import queue
import socket
import threading
import time
import unittest
from unittest.mock import patch

from bleo._http import Attempt, AttemptCancelled, Session

CREDENTIALS = (
    {'Authorization': 'Bearer secret'},
    {'Authorization': 'Digest username="u", response="r"'},
    {'Cookie': 'session=secret'},
    {'X-Relay-Auth': 'secret'},
    {'Digest': 'sha-256=abc'},
)


class Server:
    """Local HTTP server whose handler is a plain function(handler)."""

    def __init__(self, behavior):
        self.requests = []
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def handle_any(self):
                length = int(self.headers.get('Content-Length') or 0)
                self.rfile.read(length)
                owner.requests.append((self.command, self.path, dict(self.headers)))
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


def wait_for(event, handler, seconds=5):
    event.wait(seconds)


def send(handler, body=b'', status=200, headers=None, length=None):
    handler.send_response(status)
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.send_header('Content-Length', str(len(body) if length is None else length))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


class Runner(threading.Thread):
    """Runs fn in a thread, keeping its result or exception and when it finished."""

    def __init__(self, fn):
        super().__init__(daemon=True)
        self.fn = fn
        self.result = self.error = self.finished_at = None

    def run(self):
        try:
            self.result = self.fn()
        except BaseException as error:
            self.error = error
        self.finished_at = time.monotonic()


class TransportTestCase(unittest.TestCase):
    def serve(self, behavior):
        server = Server(behavior)
        self.addCleanup(server.stop)
        return server


class CancelTests(TransportTestCase):
    def test_cancel_while_waiting_for_headers(self):
        server = self.serve(lambda h, s: s.release.wait(5))
        attempt = Attempt(server.url, body=b'{}')
        self.addCleanup(attempt.close)
        runner = Runner(attempt.open)
        runner.start()
        time.sleep(0.2)
        self.assertTrue(runner.is_alive())
        started = time.monotonic()
        attempt.cancel()
        runner.join(1)
        self.assertFalse(runner.is_alive())
        self.assertIsInstance(runner.error, AttemptCancelled)
        self.assertLess(runner.finished_at - started, 1)
        self.assertTrue(attempt.cancelled)

    def test_cancel_before_open_makes_no_request(self):
        server = self.serve(lambda h, s: send(h, b'x'))
        attempt = Attempt(server.url)
        attempt.cancel()
        attempt.cancel()
        with self.assertRaises(AttemptCancelled):
            attempt.open()
        self.assertEqual(server.requests, [])

    def test_cancel_while_connecting(self):
        attempt = Attempt('http://127.0.0.1:1/', header_timeout=5)
        started_connect = threading.Event()
        real = attempt._conn._create_connection

        def slow_connect(*args):
            started_connect.set()
            return real(*args)

        # A connect that never completes: listen backlog filled so SYNs go unanswered is
        # not portable, so exercise the cancel check inside the polling loop instead.
        with patch('bleo._http.select.select', side_effect=lambda r, w, x, t: (time.sleep(t), ([], [], []))[1]), \
                patch('socket.socket.connect_ex', return_value=errno.EINPROGRESS):
            attempt._conn._create_connection = slow_connect
            runner = Runner(attempt.open)
            runner.start()
            started_connect.wait(2)
            time.sleep(0.1)
            started = time.monotonic()
            attempt.cancel()
            runner.join(1)
        self.assertIsInstance(runner.error, AttemptCancelled)
        self.assertLess(runner.finished_at - started, 0.5)

    def test_cancel_during_tls_handshake(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)  # completes TCP but never answers the ClientHello
        attempt = Attempt(f'https://127.0.0.1:{listener.getsockname()[1]}/', header_timeout=5)
        self.addCleanup(attempt.close)
        runner = Runner(attempt.open)
        runner.start()
        time.sleep(0.3)
        self.assertTrue(runner.is_alive())
        started = time.monotonic()
        attempt.cancel()
        runner.join(1)
        self.assertIsInstance(runner.error, AttemptCancelled)
        self.assertLess(runner.finished_at - started, 0.5)

    def test_cancel_mid_body(self):
        sent = threading.Event()

        def behavior(handler, server):
            handler.send_response(200)
            handler.send_header('Content-Length', '1000')
            handler.end_headers()
            handler.wfile.write(b'hello')
            handler.wfile.flush()
            sent.set()
            server.release.wait(5)

        server = self.serve(behavior)
        attempt = Attempt(server.url, body=b'{}')
        self.addCleanup(attempt.close)
        got = queue.Queue()

        def read():
            attempt.open()
            for chunk in attempt.iter_chunks():
                got.put(chunk)

        runner = Runner(read)
        runner.start()
        self.assertEqual(got.get(timeout=2), b'hello')
        time.sleep(0.2)
        self.assertTrue(runner.is_alive())
        started = time.monotonic()
        attempt.cancel()
        cancel_took = time.monotonic() - started
        runner.join(1)
        self.assertLess(cancel_took, 0.2)
        self.assertFalse(runner.is_alive())
        self.assertIsInstance(runner.error, AttemptCancelled)
        self.assertLess(runner.finished_at - started, 1)

    def test_cancel_mid_lines_and_read_all(self):
        def behavior(handler, server):
            handler.send_response(200)
            handler.send_header('Content-Length', '1000')
            handler.end_headers()
            handler.wfile.write(b'one\n')
            handler.wfile.flush()
            server.release.wait(5)

        server = self.serve(behavior)
        for consume in (lambda a: list(a.iter_lines()), lambda a: a.read_all()):
            with self.subTest(consume=consume):
                attempt = Attempt(server.url)
                self.addCleanup(attempt.close)
                attempt.open()
                runner = Runner(lambda: consume(attempt))
                runner.start()
                time.sleep(0.2)
                attempt.cancel()
                runner.join(1)
                self.assertIsInstance(runner.error, AttemptCancelled)

    def test_two_attempts_race_exactly_one_winner(self):
        for round_number in range(15):
            barrier = threading.Barrier(2, timeout=5)

            def behavior(handler, server):
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    return
                send(handler, b'ok')

            server = self.serve(behavior)
            attempts = [Attempt(server.url, body=b'{}'), Attempt(server.url, body=b'{}')]
            lock = threading.Lock()
            winners = []

            def race(me, other):
                try:
                    me.open()
                except AttemptCancelled:
                    return 'cancelled'
                with lock:
                    if not winners:
                        winners.append(me)
                        other.cancel()
                        return 'won'
                me.cancel()
                return 'lost'

            runners = [Runner(lambda a=a, b=b: race(a, b)) for a, b in (attempts, attempts[::-1])]
            for runner in runners:
                runner.start()
            for runner in runners:
                runner.join(5)
            self.assertFalse(any(runner.is_alive() for runner in runners), round_number)
            self.assertEqual([r.error for r in runners], [None, None])
            self.assertEqual(len(winners), 1)
            winner = winners[0]
            loser = attempts[1 - attempts.index(winner)]
            self.assertTrue(loser.cancelled)
            self.assertFalse(winner.cancelled)
            self.assertEqual(winner.read_all(), b'ok')
            winner.close()
            self.assertIsNone(loser._conn.sock)
            self.assertIsNone(loser._wake)
            server.stop()

    def test_response_completing_after_cancel_is_closed(self):
        server = self.serve(lambda h, s: send(h, b'late'))
        attempt = Attempt(server.url)
        real = HTTPConnection.getresponse

        def cancel_after_response(conn):
            response = real(conn)
            attempt.cancel()  # arrives while this thread is still inside open()
            return response

        with patch.object(HTTPConnection, 'getresponse', cancel_after_response):
            with self.assertRaises(AttemptCancelled):
                attempt.open()
        self.assertTrue(attempt.cancelled)
        self.assertIsNone(attempt._conn.sock)
        self.assertIsNone(attempt._wake)
        self.assertIsNone(attempt.status)

    def test_cancel_when_idle_closes_socket(self):
        server = self.serve(lambda h, s: send(h, b'x'))
        attempt = Attempt(server.url)
        attempt.open()
        self.assertIsNotNone(attempt._conn.sock)
        attempt.cancel()
        self.assertIsNone(attempt._conn.sock)
        self.assertIsNone(attempt._wake)
        with self.assertRaises(AttemptCancelled):
            attempt.read_all()

    def test_close_after_success_releases_socket(self):
        server = self.serve(lambda h, s: send(h, b'abc'))
        with Attempt(server.url) as attempt:
            attempt.open()
            self.assertEqual(attempt.read_all(), b'abc')
            self.assertEqual(attempt.status, 200)
            self.assertGreaterEqual(attempt.elapsed_headers, 0)
        self.assertIsNone(attempt._conn.sock)
        self.assertIsNone(attempt._wake)
        self.assertFalse(attempt.cancelled)


class DeadlineTests(TransportTestCase):
    def test_header_deadline_on_silent_server(self):
        server = self.serve(lambda h, s: s.release.wait(5))
        attempt = Attempt(server.url, header_timeout=0.3)
        self.addCleanup(attempt.close)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            attempt.open()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(attempt.cancelled)
        self.assertIsNone(attempt._conn.sock)

    def test_header_deadline_bounds_total_not_each_read(self):
        def behavior(handler, server):
            handler.wfile.write(b'HTTP/1.1 200 OK\r\n')
            for _ in range(50):  # steady bytes, so no single read ever times out
                handler.wfile.write(b'X-Slow: 1\r\n')
                handler.wfile.flush()
                time.sleep(0.1)

        server = self.serve(behavior)
        attempt = Attempt(server.url, header_timeout=0.5)
        self.addCleanup(attempt.close)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            attempt.open()
        self.assertLess(time.monotonic() - started, 1.5)

    def test_body_stall_deadline(self):
        def behavior(handler, server):
            handler.send_response(200)
            handler.send_header('Content-Length', '1000')
            handler.end_headers()
            handler.wfile.write(b'start')
            handler.wfile.flush()
            server.release.wait(5)

        server = self.serve(behavior)
        attempt = Attempt(server.url, stall_timeout=0.3, header_timeout=5)
        self.addCleanup(attempt.close)
        attempt.open()
        chunks = attempt.iter_chunks()
        self.assertEqual(next(chunks), b'start')
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            next(chunks)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertFalse(attempt.cancelled)

    def test_steady_slow_body_is_not_a_stall(self):
        def behavior(handler, server):
            handler.send_response(200)
            handler.send_header('Content-Length', '6')
            handler.end_headers()
            for _ in range(6):
                handler.wfile.write(b'x')
                handler.wfile.flush()
                time.sleep(0.1)

        server = self.serve(behavior)
        with Attempt(server.url, stall_timeout=0.35) as attempt:
            self.assertEqual(attempt.open().read_all(), b'xxxxxx')


class RedirectTests(TransportTestCase):
    def setUp(self):
        self.other = self.serve(lambda h, s: send(h, b'other'))
        location = self.other.url.replace('127.0.0.1', 'localhost')
        self.origin = self.serve(lambda h, s: send(h, b'', status=302, headers={'Location': location + '/stolen'}))

    def assert_not_followed(self):
        self.assertEqual(self.other.requests, [])
        self.assertEqual(len(self.origin.requests), 1)

    def test_attempt_does_not_follow_redirects_with_credentials(self):
        for headers in CREDENTIALS:
            with self.subTest(headers=headers):
                with Attempt(self.origin.url, headers=headers, body=b'{}') as attempt:
                    attempt.open()
                    self.assertEqual(attempt.status, 302)
                    self.assertIn('stolen', attempt.headers['Location'])
                self.origin.requests.clear()
                self.assertEqual(self.other.requests, [])

    def test_attempt_never_follows_even_without_credentials(self):
        for method in ('POST', 'GET'):
            with Attempt(self.origin.url, method=method) as attempt:
                self.assertEqual(attempt.open().status, 302)
        self.assertEqual(self.other.requests, [])

    def test_session_does_not_follow_redirects_with_credentials(self):
        session = Session(timeout=2)
        for headers in CREDENTIALS:
            for method in ('POST', 'GET'):
                with self.subTest(headers=headers, method=method):
                    response = session.open(method, self.origin.url, headers=headers, content=b'{}' if method == 'POST' else None)
                    self.assertEqual(response.status_code, 302)
                    response.raw.close()
        self.assertEqual(self.other.requests, [])

    def test_session_default_headers_with_credentials_do_not_follow(self):
        session = Session(timeout=2, headers={'Authorization': 'Bearer secret'})
        response = session.open('GET', self.origin.url)
        self.assertEqual(response.status_code, 302)
        response.raw.close()
        self.assertEqual(self.other.requests, [])

    def test_session_post_without_credentials_does_not_follow(self):
        response = Session(timeout=2).open('POST', self.origin.url, content=b'{}')
        self.assertEqual(response.status_code, 302)
        response.raw.close()
        self.assertEqual(self.other.requests, [])

    def test_session_plain_get_still_follows(self):
        response = Session(timeout=2).get(self.origin.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.read(), b'other')
        self.assertEqual(len(self.other.requests), 1)
        self.assertNotIn('Authorization', self.other.requests[0][2])


class BodyTests(TransportTestCase):
    def test_gzip_body_decodes(self):
        raw = 'città 🦆\nseconda riga\n'.encode()
        server = self.serve(lambda h, s: send(h, gzip.compress(raw), headers={'Content-Encoding': 'gzip'}))
        with Attempt(server.url, method='GET') as attempt:
            self.assertEqual(attempt.open().read_all(), raw)
        with Attempt(server.url, method='GET') as attempt:
            self.assertEqual(list(attempt.open().iter_lines()), ['città 🦆', 'seconda riga'])

    def test_post_body_and_headers_reach_server(self):
        server = self.serve(lambda h, s: send(h, b'{}'))
        with Attempt(server.url + '/v1/x?y=1', headers={'X-Test': 'yes'}, body='{"a": 1}') as attempt:
            attempt.open().read_all()
        method, path, headers = server.requests[0]
        self.assertEqual((method, path, headers['X-Test']), ('POST', '/v1/x?y=1', 'yes'))
        self.assertEqual(headers['Content-Length'], '8')

    def test_error_status_is_returned_not_raised(self):
        server = self.serve(lambda h, s: send(h, b'nope', status=503))
        with Attempt(server.url) as attempt:
            self.assertEqual(attempt.open().status, 503)
            self.assertEqual(attempt.read_all(), b'nope')

    def test_http_proxy_gets_absolute_url(self):
        proxy = self.serve(lambda h, s: send(h, b'via proxy'))
        with Attempt('http://example.invalid/path?q=1', proxy=proxy.url, headers={'X-Test': '1'}, method='GET') as attempt:
            self.assertEqual(attempt.open().read_all(), b'via proxy')
        self.assertEqual(proxy.requests[0][1], 'http://example.invalid/path?q=1')

    def test_rejects_bad_urls(self):
        with self.assertRaises(ValueError):
            Attempt('ftp://example.com/')
        with self.assertRaises(ValueError):
            Attempt('http://example.com/', proxy='socks5://127.0.0.1:1')


if __name__ == '__main__':
    unittest.main()
