"""The client and the API server send Brave requests through the saved relay pool."""

from http.client import HTTPConnection
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from bleo import Bleo, api
from bleo.exceptions import APIError
from bleo.relay import RelayRouter
from bleo.relay_state import HealthBook, RelayEntry, RelayPool
from helpers import ScriptedTransport, chunk, completion, sse
from test_relay import Server, reply

CHAT = '/v1/chat/completions'
TOKEN = 'test-token-0123456789-abcdefghijklmnop'


def stream_reply(texts=('OK',), gap=0.0, die=False):
    """An SSE reply. die=True closes the connection before the finish chunk and [DONE]."""
    def behavior(handler, server):
        handler.close_connection = True
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        events = [chunk(''), *[chunk(text) for text in texts]]
        if not die:
            events += [chunk(finish='stop'), '[DONE]']
        try:
            for event in events:
                handler.wfile.write(sse(event))
                handler.wfile.flush()
                if gap:
                    server.release.wait(gap)
        except OSError:
            pass
    return behavior


class RelayFixture:
    """Mixin for tests that need fake relays, a fake Brave and a router over them."""

    def setUpFixture(self, brave=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = directory.name
        self.health = HealthBook()
        self.brave = self.serve(brave or stream_reply(['direct text']))
        patcher = patch.multiple('bleo.client', _BASE=self.brave.url, _PREMIUM_BASE=self.brave.url)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch('bleo.client.time.sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def serve(self, behavior):
        server = Server(behavior)
        self.addCleanup(server.stop)
        return server

    def router_over(self, *behaviors, mode='auto'):
        self.relays = [self.serve(behavior) for behavior in behaviors]
        entries = [RelayEntry(server.url, f'r{i}', f'secret-r{i}', '') for i, server in enumerate(self.relays)]
        self.pool = RelayPool(os.path.join(self.home, 'relays.json'), entries=entries, mode=mode)
        self.router = RelayRouter(self.pool, health=self.health, hedge_grace=5.0,
                                  allow_insecure_for_tests=True)
        return self.router

    def failures(self, index):
        record = self.health.get(self.relays[index].url)
        return 0 if record is None else record.failure_count


class ClientRelayTests(RelayFixture, unittest.TestCase):
    def setUp(self):
        self.setUpFixture()

    def ask(self, relays, **kwargs):
        with Bleo(relays=relays, **kwargs) as client:
            text = client.ask('hello')
            return client, text

    def test_request_through_one_relay_succeeds_and_names_it(self):
        router = self.router_over(stream_reply(['via relay']))
        client, text = self.ask(router)
        self.assertEqual(text, 'via relay')
        self.assertEqual((client.last_route.kind, client.last_route.label), ('relay', 'r0'))
        self.assertEqual(client.last_route.url, self.relays[0].url)
        self.assertEqual(self.brave.hits, [])

    def test_relayed_request_carries_relay_headers_and_identity_encoding(self):
        router = self.router_over(stream_reply())
        self.ask(router)
        hit = self.relays[0].hits[0]
        self.assertEqual(hit.path, '/')
        self.assertEqual(hit.headers['x-relay-target'], self.brave.url)
        self.assertEqual(hit.headers['x-relay-path'], CHAT)
        self.assertEqual(hit.headers['x-relay-auth'], 'secret-r0')
        self.assertEqual(hit.headers['accept-encoding'], 'identity')
        self.assertIn('x-brave-key', hit.headers)
        self.assertIn('Signature keyId=', hit.headers['authorization'])

    def test_direct_request_has_no_relay_headers(self):
        client, text = self.ask(False)
        self.assertEqual(text, 'direct text')
        hit = self.brave.hits[0]
        self.assertFalse([name for name in hit.headers if name.startswith('x-relay-')])
        self.assertEqual(hit.headers['accept-encoding'], 'gzip, deflate')
        self.assertEqual((client.last_route.kind, client.last_route.url, client.last_route.label),
                         ('direct', self.brave.url, None))

    def test_relay_failure_falls_back_to_direct(self):
        router = self.router_over(reply(status=503, body=b'down'))
        client, text = self.ask(router)
        self.assertEqual(text, 'direct text')
        self.assertEqual(client.last_route.kind, 'direct')
        self.assertEqual(len(self.relays[0].hits), 1)
        self.assertEqual(len(self.brave.hits), 1)
        self.assertEqual(self.failures(0), 1)
        self.assertFalse([name for name in self.brave.hits[0].headers if name.startswith('x-relay-')])

    def test_empty_pool_takes_the_unchanged_direct_path(self):
        router = self.router_over()
        for relays in (router, False):
            with Bleo(relays=relays) as client:
                client._client = transport = ScriptedTransport(completion())
                self.assertEqual(client.ask('hello'), 'OK')
                if relays is False:
                    plain = transport.calls[0]
                else:
                    empty = transport.calls[0]
        self.assertEqual(empty[:2], plain[:2])
        self.assertEqual(empty[2], plain[2])
        self.assertEqual(empty[1], self.brave.url + CHAT)
        self.assertEqual(self.brave.hits, [])

    def test_empty_pool_request_on_the_wire_equals_a_direct_one(self):
        router = self.router_over()
        self.ask(router)
        self.ask(False)
        first, second = self.brave.hits
        self.assertEqual((first.method, first.path, first.body), (second.method, second.path, second.body))
        self.assertEqual(first.headers, second.headers)

    def test_mode_off_takes_the_direct_path_without_touching_relays(self):
        router = self.router_over(stream_reply(['via relay']), mode='off')
        client, text = self.ask(router)
        self.assertEqual(text, 'direct text')
        self.assertEqual(client.last_route.kind, 'direct')
        self.assertEqual(self.relays[0].hits, [])
        self.assertEqual(self.brave.hits[0].headers['accept-encoding'], 'gzip, deflate')

    def test_default_loads_the_saved_pool_and_a_missing_file_is_direct(self):
        with patch.dict(os.environ, {'BLEO_HOME': self.home}):
            with Bleo() as client:
                self.assertEqual(client.ask('hello'), 'direct text')
                self.assertEqual(client.last_route.kind, 'direct')
            relay = self.serve(stream_reply(['saved relay']))
            RelayPool(entries=[RelayEntry(relay.url, 'saved', 's3cret', '')]).save()
            insecure = patch('bleo.relay.validate_relay_url', lambda url, allow_insecure=False: url)
            with insecure, Bleo() as client:
                self.assertEqual(client.ask('hello'), 'saved relay')
                self.assertEqual(client.last_route.label, 'saved')

    def saved_pool_client(self, behavior, timeout):
        relay = self.serve(behavior)
        with patch.dict(os.environ, {'BLEO_HOME': self.home}):
            RelayPool(entries=[RelayEntry(relay.url, 'saved', 's', '')]).save()
        insecure = patch('bleo.relay.validate_relay_url', lambda url, allow_insecure=False: url)
        insecure.start()
        self.addCleanup(insecure.stop)
        environ = patch.dict(os.environ, {'BLEO_HOME': self.home})
        environ.start()
        self.addCleanup(environ.stop)
        return relay, Bleo(timeout=timeout)

    def assertTimesOutQuickly(self, behavior):
        relay, client = self.saved_pool_client(behavior, 0.25)
        started = time.monotonic()
        with client:
            text = client.ask('hello')
        elapsed = time.monotonic() - started
        self.assertEqual(len(relay.hits), 1)
        self.assertEqual(text, 'direct text')  # the stuck relay was abandoned for the direct path
        self.assertLess(elapsed, 3.0)

    def test_client_timeout_bounds_a_relay_that_delays_headers(self):
        self.assertTimesOutQuickly(reply(delay=10))

    def test_client_timeout_bounds_a_relay_that_stalls_mid_body(self):
        def stall(handler, server):
            handler.close_connection = True
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.send_header('Connection', 'close')
            handler.end_headers()
            handler.wfile.write(b': keepalive\n\n')
            handler.wfile.flush()
            server.release.wait(10)
        self.assertTimesOutQuickly(stall)

    def test_client_timeout_reaches_a_router_it_builds_but_not_one_it_is_given(self):
        _, client = self.saved_pool_client(stream_reply(), 7.5)
        with client:
            self.assertEqual((client._router.header_timeout, client._router.stall_timeout), (7.5, 7.5))
        router = self.router_over(stream_reply())
        with Bleo(relays=router, timeout=7.5) as client:
            self.assertIsNone(client._router.header_timeout)

    def test_relays_argument_is_validated(self):
        with self.assertRaises(ValueError):
            Bleo(relays='yes')

    def test_stream_dying_before_text_marks_the_relay_and_retries(self):
        router = self.router_over(stream_reply([], die=True), stream_reply(['second relay']))
        client, text = self.ask(router)
        self.assertEqual(text, 'second relay')
        self.assertEqual(client.last_route.label, 'r1')
        self.assertEqual(self.failures(0), 1)
        self.assertEqual((len(self.relays[0].hits), len(self.relays[1].hits)), (1, 1))
        self.assertEqual(self.brave.hits, [])

    def test_stream_dying_after_text_is_not_retried(self):
        router = self.router_over(stream_reply(['partial'], die=True), stream_reply(['second relay']))
        received = []
        with Bleo(relays=router) as client:
            with self.assertRaises(APIError):
                for text in client.stream('hello'):
                    received.append(text)
        self.assertEqual(received, ['partial'])
        self.assertEqual((len(self.relays[0].hits), len(self.relays[1].hits)), (1, 0))
        self.assertEqual(self.brave.hits, [])

    def test_status_errors_from_brave_are_not_blamed_on_the_relay(self):
        router = self.router_over(reply(status=401, body=b'{"error":"bad key"}'))
        with Bleo(relays=router) as client:
            with self.assertRaises(Exception) as caught:
                client.ask('hello')
        self.assertIn('authentication failed', str(caught.exception))
        self.assertEqual(self.failures(0), 0)

    def test_premium_credential_uses_the_premium_host_through_the_relay(self):
        router = self.router_over(stream_reply())
        with patch('bleo.client._PREMIUM_BASE', 'http://premium.invalid'):
            client, _ = self.ask(router, premium_credential='cookie-value')
        hit = self.relays[0].hits[0]
        self.assertEqual(hit.headers['x-relay-target'], 'http://premium.invalid')
        self.assertEqual(client.last_route.kind, 'relay')


class ApiSharedRouterTests(RelayFixture, unittest.TestCase):
    def setUp(self):
        self.setUpFixture()

    def serve_api(self, **kwargs):
        server = api.create_server(0, token=TOKEN, **kwargs)
        import threading
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.addCleanup(stop)
        return server

    def chat(self, server):
        connection = HTTPConnection('127.0.0.1', server.server_address[1], timeout=30)
        try:
            connection.request('POST', '/v1/chat/completions',
                               body=json.dumps({'messages': [{'role': 'user', 'content': 'hi'}]}),
                               headers={'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_one_router_is_shared_by_every_request(self):
        router = self.router_over(reply(status=503, body=b'down'))
        server = self.serve_api(relays=router)
        seen = []
        real = api.make_client

        def spy(model, **kwargs):
            seen.append(real(model, **kwargs))
            return seen[-1]
        with patch('bleo.api.make_client', side_effect=spy):
            self.assertEqual(self.chat(server)[0], 200)
            self.assertEqual(self.failures(0), 1)
            self.assertEqual(self.chat(server)[0], 200)
        self.assertIs(server.router, router)
        self.assertEqual([client._router for client in seen], [router, router])
        self.assertEqual(len({id(client) for client in seen}), 2)
        # The second request saw the same HealthBook: r0 was cooling, so it was never started.
        self.assertEqual(self.failures(0), 1)
        self.assertEqual(len(self.relays[0].hits), 1)
        self.assertEqual(len(self.brave.hits), 2)

    def test_requests_go_through_the_shared_relay(self):
        router = self.router_over(stream_reply(['relayed']))
        status, body = self.chat(self.serve_api(relays=router))
        self.assertEqual(status, 200)
        self.assertEqual(body['choices'][0]['message']['content'], 'relayed')
        self.assertEqual(len(self.relays[0].hits), 1)
        self.assertEqual(self.brave.hits, [])

    def test_request_body_cannot_change_routing(self):
        router = self.router_over(stream_reply(['relayed']))
        server = self.serve_api(relays=router)
        connection = HTTPConnection('127.0.0.1', server.server_address[1], timeout=30)
        connection.request('POST', '/v1/chat/completions',
                           body=json.dumps({'messages': [{'role': 'user', 'content': 'hi'}],
                                            'relays': ['https://evil.example'], 'relay': 'off'}),
                           headers={'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'})
        status = connection.getresponse().status
        connection.close()
        self.assertEqual(status, 200)
        self.assertEqual(len(self.relays[0].hits), 1)
        self.assertEqual(self.brave.hits, [])
        self.assertNotIn('evil', json.dumps([hit.headers for hit in self.relays[0].hits]))

    def test_default_server_has_no_router_when_the_pool_is_empty_or_off(self):
        with patch.dict(os.environ, {'BLEO_HOME': self.home}):
            self.assertIsNone(self.serve_api().router)
            relay = self.serve(stream_reply())
            RelayPool(entries=[RelayEntry(relay.url, 'saved', 's', '')], mode='off').save()
            self.assertIsNone(self.serve_api().router)
            status, _ = self.chat(self.serve_api())
        self.assertEqual(status, 200)
        self.assertEqual(relay.hits, [])
        self.assertEqual(len(self.brave.hits), 1)

    def test_default_server_builds_one_router_from_the_saved_pool(self):
        relay = self.serve(stream_reply())
        with patch.dict(os.environ, {'BLEO_HOME': self.home}):
            RelayPool(entries=[RelayEntry(relay.url, 'saved', 's', '')]).save()
            server = self.serve_api()
        self.assertIsInstance(server.router, RelayRouter)
        self.assertEqual([entry.label for entry in server.router.pool.entries], ['saved'])

    def test_relays_false_means_no_router(self):
        self.assertIsNone(self.serve_api(relays=False).router)


if __name__ == '__main__':
    unittest.main()
