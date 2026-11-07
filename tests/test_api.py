from contextlib import contextmanager, redirect_stderr, redirect_stdout
from email.message import Message
from http.client import HTTPConnection
from io import StringIO
from pathlib import Path
import json
import os
import queue
import signal
import socket
import stat
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from bleo import api
from bleo import Bleo, ClaudeHaiku, GptLuna, list_models
from bleo._http import Response
from bleo.tunnel import QUICK_TUNNEL_HOST_SUFFIX, TunnelError
from helpers import Raw, chunk, completion, sse

TOKEN = 'test-token-0123456789-abcdefghijklmnop'


class GatedRaw:
    """Upstream body whose chunks the test releases one by one."""

    def __init__(self):
        self.status = 200
        self.headers = Message()
        self.headers['Content-Type'] = 'text/event-stream'
        self.items = queue.Queue()
        self.closed = threading.Event()

    def getcode(self):
        return self.status

    def push(self, *objects):
        self.items.put(sse(*objects))

    def read1(self, size=-1):
        return self.items.get(timeout=15)

    read = read1

    def close(self):
        self.closed.set()


class FakeTransport:
    def __init__(self, upstream):
        self.upstream = upstream
        self.closed = False

    @contextmanager
    def stream(self, method, url, **kwargs):
        payload = json.loads(kwargs['content'])
        self.upstream.calls.append({'method': method, 'url': url, 'headers': kwargs['headers'],
                                    'content': kwargs['content'], 'payload': payload})
        raw = self.upstream.respond(payload)
        if isinstance(raw, BaseException):
            raise raw
        try:
            yield Response(raw)
        finally:
            raw.close()

    def close(self):
        self.closed = True


class Upstream:
    """Fake Brave. ``respond`` maps a request payload to a raw response and may be replaced."""

    def __init__(self):
        self.calls = []
        self.sessions = []
        self.transports = []
        self.respond = lambda payload: completion('OK')

    def session(self, **kwargs):
        self.sessions.append(kwargs)
        transport = FakeTransport(self)
        self.transports.append(transport)
        return transport


class Reply:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body)

    def events(self):
        out = []
        for block in self.body.decode().split('\n\n'):
            if block.startswith('data: '):
                data = block[6:]
                out.append(data if data == '[DONE]' else json.loads(data))
        return out


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patch.dict(os.environ, {'BLEO_API_TOKEN': TOKEN, 'BLEO_HOME': self.home.name}, clear=True).start()
        self.upstream = Upstream()
        patch('bleo.client.Session', side_effect=self.upstream.session).start()
        patch('bleo.client.time.sleep').start()
        self.addCleanup(patch.stopall)
        self.start_server()

    def start_server(self, **kwargs):
        self.server = api.create_server(0, token=TOKEN, **kwargs)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
        self.addCleanup(stop)

    def request(self, method, path, body=None, *, token=TOKEN, headers=None, raw_body=None):
        conn = HTTPConnection('127.0.0.1', self.port, timeout=15)
        self.addCleanup(conn.close)
        send = dict(headers or {})
        if token is not None:
            send.setdefault('Authorization', f'Bearer {token}')
        data = raw_body if raw_body is not None else (None if body is None else json.dumps(body))
        conn.request(method, path, body=data, headers=send)
        response = conn.getresponse()
        return Reply(response.status, response, response.read())

    def chat(self, payload=None, **kwargs):
        body = {'messages': [{'role': 'user', 'content': 'hi'}]}
        body.update(payload or {})
        return self.request('POST', '/v1/chat/completions', body, **kwargs)

    def raw_exchange(self, head):
        with socket.create_connection(('127.0.0.1', self.port), timeout=15) as sock:
            sock.sendall(head.encode())
            sock.shutdown(socket.SHUT_WR)
            data = b''
            while True:
                part = sock.recv(65536)
                if not part:
                    break
                data += part
        status = int(data.split(b' ', 2)[1])
        return status, json.loads(data.split(b'\r\n\r\n', 1)[1])


def upstream_text(*pieces, model='served-model'):
    return Raw(sse(*[chunk(piece, model=model) for piece in pieces], chunk(finish='stop', model=model), '[DONE]'),
               headers={'Content-Type': 'text/event-stream'})


class AuthTests(ApiCase):
    def test_missing_or_wrong_token_is_401_and_never_reaches_upstream(self):
        cases = [(None, None), ('wrong-token', None), (TOKEN[:-1], None), ('', None),
                 (None, f'Basic {TOKEN}'), (None, TOKEN)]
        for path, method in (('/v1/models', 'GET'), ('/models', 'GET'), ('/v1/chat/completions', 'POST'),
                             ('/nope', 'GET')):
            for token, authorization in cases:
                with self.subTest(path=path, token=token, authorization=authorization):
                    headers = {'Authorization': authorization} if authorization else {}
                    body = {'messages': [{'role': 'user', 'content': 'x'}]} if method == 'POST' else None
                    reply = self.request(method, path, body, token=token, headers=headers)
                    self.assertEqual(reply.status, 401)
                    self.assertEqual(reply.json()['error']['type'], 'authentication_error')
                    self.assertEqual(reply.headers.getheader('WWW-Authenticate'), 'Bearer')
        self.assertEqual(self.upstream.calls, [])

    def test_health_needs_no_token(self):
        reply = self.request('GET', '/health', token=None)
        self.assertEqual((reply.status, reply.json()), (200, {'status': 'ok'}))

    def test_valid_token_is_accepted_case_insensitive_scheme(self):
        reply = self.request('GET', '/v1/models', token=None, headers={'Authorization': f'bearer {TOKEN}'})
        self.assertEqual(reply.status, 200)

    def test_empty_token_cannot_start_a_server(self):
        with self.assertRaises(ValueError):
            api.create_server(0, token='')

    def test_server_binds_loopback_and_has_no_host_option(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            api.main(['--host', '0.0.0.0'])


class SecurityTests(ApiCase):
    def test_body_cannot_override_credentials_proxy_gateways_or_routing(self):
        override = {'proxy': 'http://evil.example:8080', 'gateways': ['https://evil.example/v1'],
                    'gateway_cooldown': 0, 'x_brave_key': 'evil-key', 'pre_shared_key': 'evil-psk',
                    'signing_key_id': 'evil-id', 'premium_credential': 'evil-cookie',
                    'base_url': 'https://evil.example', 'relays': ['https://evil.example']}
        reply = self.chat(override)
        self.assertEqual(reply.status, 200)
        call = self.upstream.calls[0]
        self.assertEqual(call['url'], 'https://ai-chat.bsg.brave.com/v1/chat/completions')
        self.assertEqual(call['headers']['x-brave-key'], 'qztbjzBqJueQZLFkwTTJrieu8Vw3789u')
        self.assertNotIn('Cookie', call['headers'])
        self.assertIn('keyId="linux-148-release"', call['headers']['Authorization'])
        self.assertEqual([session['proxy'] for session in self.upstream.sessions], [None])
        self.assertNotIn('evil', json.dumps(call))
        self.assertEqual(set(call['payload']),
                         {'messages', 'brave_capability', 'model', 'stream', 'system_language'})

    def test_every_request_uses_its_own_client_that_is_closed(self):
        clients = []
        real = api.make_client

        def spy(model):
            clients.append(real(model))
            return clients[-1]
        with patch('bleo.api.make_client', side_effect=spy):
            self.chat()
            self.chat()
            self.chat({'stream': True})
        self.assertEqual(len(clients), 3)
        self.assertEqual(len({id(item) for item in clients}), 3)
        self.assertEqual(len({id(item._client) for item in clients}), 3)
        self.assertTrue(all(item._client.closed for item in clients))
        self.assertTrue(all(item.history.messages == [] for item in clients))

    def test_complete_never_touches_history_and_sets_only_last_model(self):
        self.upstream.respond = lambda payload: upstream_text('fine')
        with Bleo(history=True) as client:
            client.history.add_user('earlier')
            before = list(client.history.messages)
            text = ''.join(client.complete([{'role': 'user', 'content': 'q'}]))
            self.assertEqual(text, 'fine')
            self.assertEqual(client.history.messages, before)
            self.assertEqual(client.last_model, 'served-model')
        sent = self.upstream.calls[0]['payload']['messages']
        self.assertEqual(sent, [{'role': 'user', 'content': [{'type': 'text', 'text': 'q'}]}])

    def test_concurrent_requests_keep_their_own_messages_and_models(self):
        barrier = threading.Barrier(2, timeout=15)

        def respond(payload):
            barrier.wait()  # both requests are inside the upstream call at the same moment
            last = payload['messages'][-1]['content'][0]['text']
            return upstream_text(f'echo:{last}', model=f'served-{payload["model"]}')
        self.upstream.respond = respond
        results = {}

        def run(name, model):
            results[name] = self.chat({'model': model, 'messages': [
                {'role': 'system', 'content': f'sys-{name}'}, {'role': 'user', 'content': f'q-{name}'}]})
        threads = [threading.Thread(target=run, args=('alpha', 'haiku')),
                   threading.Thread(target=run, args=('beta', 'luna'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        alpha, beta = results['alpha'].json(), results['beta'].json()
        self.assertEqual(alpha['choices'][0]['message']['content'], 'echo:q-alpha')
        self.assertEqual(beta['choices'][0]['message']['content'], 'echo:q-beta')
        self.assertEqual(alpha['model'], f'served-{ClaudeHaiku}')
        self.assertEqual(beta['model'], f'served-{GptLuna}')
        sent = {call['payload']['messages'][0]['content'][0]['text']: call['payload']['model']
                for call in self.upstream.calls}
        self.assertEqual(sent, {'sys-alpha': ClaudeHaiku, 'sys-beta': GptLuna})


class NoTokenInOutputTests(ApiCase):
    def test_startup_output_has_no_token_but_names_the_token_file(self):
        out, err = StringIO(), StringIO()
        with patch.dict(os.environ, {'BLEO_API_TOKEN': ''}), \
                patch('bleo.api._make_stop_event', side_effect=lambda: preset_event()), \
                redirect_stdout(out), redirect_stderr(err):
            code = api.main(['--port', '0'])
        token = (Path(self.home.name) / 'api-token').read_text().strip()
        self.assertEqual(code, 0)
        self.assertIn('listening on http://127.0.0.1:', out.getvalue())
        self.assertIn(str(Path(self.home.name) / 'api-token'), out.getvalue())
        self.assertNotIn(token, out.getvalue() + err.getvalue())

    def test_startup_with_env_token_does_not_print_it(self):
        out, err = StringIO(), StringIO()
        with patch('bleo.api._make_stop_event', side_effect=lambda: preset_event()), \
                redirect_stdout(out), redirect_stderr(err):
            api.main(['--port', '0'])
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        self.assertIn('BLEO_API_TOKEN', out.getvalue())

    def test_401_and_500_paths_log_and_reply_without_token(self):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            denied = self.chat(token='guess', headers={'Authorization': f'Bearer {TOKEN}x'})
            with patch('bleo.api.make_client', side_effect=RuntimeError(f'boom {TOKEN}')):
                failed = self.chat()
                streamed = self.chat({'stream': True})
        self.assertEqual((denied.status, failed.status, streamed.status), (401, 500, 500))
        everything = out.getvalue() + err.getvalue() + denied.body.decode() + failed.body.decode()
        self.assertNotIn(TOKEN, everything)
        self.assertNotIn('boom', everything)
        self.assertIn('RuntimeError', err.getvalue())

    def test_upstream_failure_response_has_no_token(self):
        self.upstream.respond = lambda payload: OSError(f'connect failed {TOKEN}')
        reply = self.chat()
        self.assertEqual(reply.status, 502)
        self.assertNotIn(TOKEN, reply.body.decode())


def preset_event():
    event = threading.Event()
    event.set()
    return event


class TokenFileTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name) / 'state'
        patch.dict(os.environ, {'BLEO_HOME': str(self.root)}, clear=True).start()
        self.addCleanup(patch.stopall)

    def test_first_run_creates_private_file_and_directory(self):
        token = api.load_token()
        path = self.root / 'api-token'
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(api.load_token(), token)

    def test_loose_permissions_are_tightened(self):
        api.load_token()
        path = self.root / 'api-token'
        path.chmod(0o644)
        api.load_token()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_env_token_wins_and_writes_no_file(self):
        with patch.dict(os.environ, {'BLEO_API_TOKEN': ' from-env '}):
            self.assertEqual(api.load_token(), 'from-env')
        self.assertFalse((self.root / 'api-token').exists())

    def test_empty_token_file_is_refused(self):
        self.root.mkdir(mode=0o700)
        (self.root / 'api-token').write_text('\n')
        with self.assertRaises(api.BleoError):
            api.load_token()

    def test_token_show_refuses_without_a_terminal(self):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = api.main(['token', '--show'])
        token = (self.root / 'api-token').read_text().strip()
        self.assertNotEqual(code, 0)
        self.assertNotIn(token, out.getvalue() + err.getvalue())
        self.assertIn('terminal', err.getvalue())

    def test_token_show_prints_on_a_terminal(self):
        class Terminal(StringIO):
            def isatty(self):
                return True
        out = Terminal()
        with redirect_stdout(out):
            code = api.main(['token', '--show'])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), (self.root / 'api-token').read_text().strip())

    def test_token_without_show_prints_only_the_location(self):
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(api.main(['token']), 0)
        self.assertIn(str(self.root / 'api-token'), out.getvalue())
        self.assertNotIn((self.root / 'api-token').read_text().strip(), out.getvalue())


class MessageTests(ApiCase):
    def test_full_multi_turn_conversation_reaches_upstream(self):
        reply = self.chat({'messages': [
            {'role': 'system', 'content': 'be brief'}, {'role': 'user', 'content': 'one'},
            {'role': 'assistant', 'content': 'two'},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'three'},
                                         {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]}]})
        self.assertEqual(reply.status, 200)
        self.assertEqual(self.upstream.calls[0]['payload']['messages'], [
            {'role': 'system', 'content': [{'type': 'text', 'text': 'be brief'}]},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'one'}]},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'two'}]},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'three'},
                                         {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]}])

    def test_tool_roles_and_tool_calls_are_rejected_naming_the_field(self):
        cases = [
            ([{'role': 'user', 'content': 'a'}, {'role': 'tool', 'content': 'b', 'tool_call_id': 'x'}], 'messages[1].role'),
            ([{'role': 'function', 'content': 'b', 'name': 'f'}], 'messages[0].role'),
            ([{'role': 'assistant', 'content': None, 'tool_calls': [{'id': '1'}]}], 'messages[0].tool_calls'),
            ([{'role': 'assistant', 'content': 'x', 'function_call': {'name': 'f'}}], 'messages[0].function_call'),
            ([{'role': 'developer', 'content': 'x'}], 'messages[0].role'),
            ([{'role': 'user', 'content': [{'type': 'input_audio'}]}], 'messages[0].content[0].type'),
            ([{'role': 'assistant', 'content': [{'type': 'image_url', 'image_url': {'url': 'x'}}]}], 'messages[0].content[0]'),
            ([{'role': 'user', 'content': 5}], 'messages[0].content'),
            ([{'role': 'user'}], 'messages[0].content'),
            ([], 'messages'), ('hi', 'messages'), ([5], 'messages[0]'),
        ]
        for messages, param in cases:
            with self.subTest(param=param, messages=messages):
                reply = self.chat({'messages': messages})
                self.assertEqual(reply.status, 400)
                self.assertEqual(reply.json()['error']['param'], param)
        self.assertEqual(self.upstream.calls, [])

    def test_missing_messages_is_400(self):
        reply = self.request('POST', '/v1/chat/completions', {'model': 'luna'})
        self.assertEqual((reply.status, reply.json()['error']['param']), (400, 'messages'))


class ParameterTests(ApiCase):
    def test_unsupported_parameters_return_400_naming_the_parameter(self):
        cases = {'tools': [{'type': 'function'}], 'tool_choice': 'auto', 'functions': [{'name': 'f'}],
                 'function_call': 'auto', 'parallel_tool_calls': False, 'logprobs': True, 'top_logprobs': 2,
                 'n': 2, 'response_format': {'type': 'json_object'}}
        for name, value in cases.items():
            with self.subTest(name):
                reply = self.chat({name: value})
                self.assertEqual(reply.status, 400)
                error = reply.json()['error']
                self.assertEqual(error['param'], name)
                self.assertEqual(error['code'], 'unsupported_parameter')
                self.assertEqual(set(error), {'message', 'type', 'param', 'code'})
        self.assertEqual(self.upstream.calls, [])

    def test_accepted_parameters_are_ignored_and_never_forwarded(self):
        reply = self.chat({'temperature': 0.2, 'top_p': 0.9, 'presence_penalty': 1, 'frequency_penalty': 1,
                           'seed': 7, 'user': 'u', 'stream_options': {'include_usage': True}, 'n': 1,
                           'logprobs': False, 'tools': None, 'response_format': {'type': 'text'},
                           'some_unknown_field': 1})
        self.assertEqual(reply.status, 200)
        payload = self.upstream.calls[0]['payload']
        self.assertEqual(set(payload), {'messages', 'brave_capability', 'model', 'stream', 'system_language'})

    def test_bad_types_are_400(self):
        cases = [({'stream': 'yes'}, 'stream'), ({'stop': 5}, 'stop'), ({'stop': ['a', '']}, 'stop'),
                 ({'stop': list('abcde')}, 'stop'), ({'max_tokens': 0}, 'max_tokens'),
                 ({'max_tokens': '5'}, 'max_tokens'), ({'max_tokens': True}, 'max_tokens'),
                 ({'max_completion_tokens': -1}, 'max_completion_tokens'), ({'n': 'one'}, 'n'),
                 ({'model': 5}, 'model'), ({'model': ''}, 'model'), ({'model': 'no-such-model'}, 'model'),
                 ({'response_format': 'text'}, 'response_format')]
        for extra, param in cases:
            with self.subTest(extra=extra):
                reply = self.chat(extra)
                self.assertEqual((reply.status, reply.json()['error']['param']), (400, param))

    def test_model_aliases_resolve_and_default_is_luna(self):
        self.chat({'model': 'haiku'})
        self.chat()
        self.assertEqual([call['payload']['model'] for call in self.upstream.calls], [ClaudeHaiku, GptLuna])

    def test_server_default_model_is_used_when_request_names_none(self):
        self.start_server(default_model='haiku')
        self.chat()
        self.assertEqual(self.upstream.calls[-1]['payload']['model'], ClaudeHaiku)

    def test_response_model_is_reported_model_with_request_fallback(self):
        self.upstream.respond = lambda payload: upstream_text('x', model='brave-says')
        self.assertEqual(self.chat().json()['model'], 'brave-says')
        self.upstream.respond = lambda payload: Raw(
            sse({'choices': [{'delta': {'content': 'x'}, 'finish_reason': None}]}, chunk(finish='stop', model=None), '[DONE]'))
        self.assertEqual(self.chat({'model': 'haiku'}).json()['model'], ClaudeHaiku)

    def test_non_stream_response_shape(self):
        reply = self.chat()
        body = reply.json()
        self.assertEqual(reply.headers.getheader('Content-Type'), 'application/json')
        self.assertEqual(body['object'], 'chat.completion')
        self.assertTrue(body['id'].startswith('chatcmpl-'))
        self.assertIsInstance(body['created'], int)
        self.assertEqual(len(body['choices']), 1)
        self.assertEqual(body['choices'][0], {'index': 0, 'finish_reason': 'stop',
                                              'message': {'role': 'assistant', 'content': 'OK'}})
        self.assertNotIn('usage', body)

    def test_models_endpoint_lists_models(self):
        for path in ('/v1/models', '/models'):
            body = self.request('GET', path).json()
            self.assertEqual(body['object'], 'list')
            self.assertEqual([item['id'] for item in body['data']], list_models())
            self.assertTrue(all(item['object'] == 'model' for item in body['data']))
        self.assertIn(GptLuna, [item['id'] for item in body['data']])


class LimitTests(ApiCase):
    def content(self, reply):
        body = reply.json()
        return body['choices'][0]['message']['content'], body['choices'][0]['finish_reason']

    def test_stop_string_cuts_output_across_chunks(self):
        self.upstream.respond = lambda payload: upstream_text('Hello wor', 'ld END', 'ING and more')
        self.assertEqual(self.content(self.chat({'stop': 'ENDING'})), ('Hello world ', 'stop'))

    def test_stop_list_uses_earliest_occurrence(self):
        self.upstream.respond = lambda payload: upstream_text('a-b-c-d')
        self.assertEqual(self.content(self.chat({'stop': ['c', 'b']})), ('a-', 'stop'))

    def test_max_tokens_cuts_at_quarter_of_characters_with_length_reason(self):
        self.upstream.respond = lambda payload: upstream_text('0123456789', 'abcdefghij')
        self.assertEqual(self.content(self.chat({'max_tokens': 3})), ('0123456789ab', 'length'))
        self.assertEqual(self.content(self.chat({'max_completion_tokens': 2})), ('01234567', 'length'))
        self.assertEqual(self.content(self.chat({'max_tokens': 100, 'max_completion_tokens': 1})), ('0123', 'length'))

    def test_output_within_limits_finishes_with_stop(self):
        self.upstream.respond = lambda payload: upstream_text('short')
        self.assertEqual(self.content(self.chat({'max_tokens': 50, 'stop': 'zzz'})), ('short', 'stop'))
        self.assertEqual(self.content(self.chat({'max_tokens': 2})), ('short', 'stop'))
        self.assertEqual(self.content(self.chat({'max_tokens': 1})), ('shor', 'length'))
        self.assertEqual(self.content(self.chat({'max_tokens': 2, 'stop': 'rt'})), ('sho', 'stop'))

    def test_stream_stop_and_length(self):
        self.upstream.respond = lambda payload: upstream_text('Hello wor', 'ld END', 'ING and more')
        events = self.chat({'stream': True, 'stop': 'ENDING'}).events()
        text = ''.join(e['choices'][0]['delta'].get('content', '') for e in events[:-1])
        self.assertEqual(text, 'Hello world ')
        self.assertEqual(events[-2]['choices'][0]['finish_reason'], 'stop')
        events = self.chat({'stream': True, 'max_tokens': 3}).events()
        text = ''.join(e['choices'][0]['delta'].get('content', '') for e in events[:-1])
        self.assertEqual(text, 'Hello world ')
        self.assertEqual(events[-2]['choices'][0]['finish_reason'], 'length')

    def test_limiter_never_leaks_partial_stop_text(self):
        limiter = api.Limiter(('STOP',), None)
        emitted = []
        for piece in 'abST', 'O', 'x':
            text, reason = limiter.feed(piece)
            emitted.append(text)
            self.assertIsNone(reason)
        self.assertEqual(''.join(emitted) + limiter.flush()[0], 'abSTOx')


class StreamTests(ApiCase):
    def test_sse_sequence_role_content_finish_done(self):
        self.upstream.respond = lambda payload: upstream_text('Hel', 'lo')
        reply = self.chat({'stream': True})
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.headers.getheader('Content-Type'), 'text/event-stream')
        events = reply.events()
        self.assertEqual(events[-1], '[DONE]')
        chunks = events[:-1]
        self.assertTrue(all(c['object'] == 'chat.completion.chunk' for c in chunks))
        self.assertEqual(len({c['id'] for c in chunks}), 1)
        self.assertEqual(chunks[0]['choices'][0]['delta'], {'role': 'assistant', 'content': ''})
        self.assertEqual([c['choices'][0]['delta'] for c in chunks[1:-1]], [{'content': 'Hel'}, {'content': 'lo'}])
        self.assertEqual(chunks[-1]['choices'][0], {'index': 0, 'delta': {}, 'finish_reason': 'stop'})
        self.assertTrue(all(c['choices'][0]['finish_reason'] is None for c in chunks[:-1]))
        self.assertEqual({c['model'] for c in chunks}, {'served-model'})
        self.assertTrue(reply.body.endswith(b'data: [DONE]\n\n'))

    def test_upstream_error_mid_stream_becomes_sse_error_then_done(self):
        self.upstream.respond = lambda payload: Raw(
            sse(chunk('part'), {'error': {'type': '500', 'message': 'internal detail'}}))
        reply = self.chat({'stream': True})
        self.assertEqual(reply.status, 200)
        events = reply.events()
        self.assertEqual(events[0]['choices'][0]['delta']['role'], 'assistant')
        self.assertEqual(events[1]['choices'][0]['delta'], {'content': 'part'})
        self.assertEqual(set(events[2]), {'error'})
        self.assertEqual(set(events[2]['error']), {'message', 'type', 'param', 'code'})
        self.assertEqual(events[2]['error']['type'], 'upstream_error')
        self.assertEqual(events[3], '[DONE]')
        self.assertEqual(len(events), 4)
        self.assertNotIn('internal detail', reply.body.decode())
        self.assertEqual(reply.body.count(b'HTTP/1'), 0)

    def test_upstream_failure_before_any_output_is_a_normal_http_error(self):
        self.upstream.respond = lambda payload: Raw(b'', status=401)
        reply = self.chat({'stream': True})
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.json()['error']['code'], 'upstream_auth_failed')
        self.upstream.respond = lambda payload: Raw(b'', status=400)
        self.assertEqual(self.chat().status, 502)

    def test_rate_limit_maps_to_429_with_retry_after(self):
        self.upstream.respond = lambda payload: Raw(b'', status=429, headers={'Retry-After': '3'})
        reply = self.chat()
        self.assertEqual(reply.status, 429)
        self.assertEqual(reply.json()['error']['type'], 'rate_limit_error')

    def test_client_disconnect_closes_the_upstream_generator(self):
        gate = GatedRaw()
        self.upstream.respond = lambda payload: gate
        gate.push(chunk('first '))
        conn = HTTPConnection('127.0.0.1', self.port, timeout=15)
        self.addCleanup(conn.close)
        conn.request('POST', '/v1/chat/completions', json.dumps(
            {'stream': True, 'messages': [{'role': 'user', 'content': 'go'}]}),
            {'Authorization': f'Bearer {TOKEN}'})
        response = conn.getresponse()
        seen = b''
        while b'first ' not in seen:
            seen += response.fp.readline()
        self.assertFalse(gate.closed.is_set())
        response.close()  # the response holds a duplicate of the socket
        conn.close()
        for _ in range(400):
            if gate.closed.wait(0.02):
                break
            gate.push(chunk('x' * 4096))
        self.assertTrue(gate.closed.is_set(), 'upstream was not abandoned after the client left')
        self.assertTrue(self.upstream.transports[0].closed)

    def assert_json_error(self, reply, status, code, error_type='upstream_error'):
        self.assertEqual(reply.status, status)
        self.assertEqual(reply.headers.getheader('Content-Type'), 'application/json')
        body = reply.json()
        self.assertEqual(set(body['error']), {'message', 'type', 'param', 'code'})
        self.assertEqual((body['error']['type'], body['error']['code']), (error_type, code))
        self.assertNotIn(TOKEN, reply.body.decode())
        self.assertNotIn(b'data: ', reply.body)

    def test_upstream_timeout_before_first_chunk_is_504_json_for_stream_and_non_stream(self):
        self.upstream.respond = lambda payload: TimeoutError(f'secret-detail {TOKEN}')
        for stream in (False, True):
            with self.subTest(stream=stream):
                reply = self.chat({'stream': stream})
                self.assert_json_error(reply, 504, 'upstream_timeout')
                self.assertNotIn('secret-detail', reply.body.decode())

    def test_socket_timeout_is_also_504(self):
        self.upstream.respond = lambda payload: socket.timeout('read timed out')
        self.assert_json_error(self.chat({'stream': True}), 504, 'upstream_timeout')

    def test_upstream_connection_error_before_first_chunk_is_502_json(self):
        for error in (ConnectionResetError(f'reset {TOKEN}'), ConnectionRefusedError('refused'),
                      BrokenPipeError('pipe'), OSError('dns')):
            self.upstream.respond = lambda payload, error=error: error
            for stream in (False, True):
                with self.subTest(error=type(error).__name__, stream=stream):
                    self.assert_json_error(self.chat({'stream': stream}), 502, 'upstream_unavailable')

    def test_upstream_failure_while_priming_the_stream_iterator_is_http_error(self):
        class FailingRead(Raw):
            def read1(self, size=-1):
                raise TimeoutError('stalled')
        self.upstream.respond = lambda payload: FailingRead(b'', headers={'Content-Type': 'text/event-stream'})
        self.assert_json_error(self.chat({'stream': True}), 504, 'upstream_timeout')

    def test_upstream_failure_after_first_chunk_is_sse_error_then_done(self):
        for error, code in ((TimeoutError(f'stalled {TOKEN}'), 'upstream_timeout'),
                            (ConnectionResetError(f'reset {TOKEN}'), 'upstream_unavailable')):
            class FailingAfterFirst(Raw):
                def read1(self, size=-1, error=error):
                    data = super().read1(size)
                    if data:
                        return data
                    raise error
            self.upstream.respond = lambda payload, cls=FailingAfterFirst: cls(
                sse(chunk('part ')), headers={'Content-Type': 'text/event-stream'})
            with self.subTest(code=code):
                reply = self.chat({'stream': True})
                self.assertEqual(reply.status, 200)
                events = reply.events()
                self.assertEqual(events[1]['choices'][0]['delta'], {'content': 'part '})
                self.assertEqual(events[-2]['error']['code'], code)
                self.assertEqual(events[-1], '[DONE]')
                self.assertNotIn(TOKEN, reply.body.decode())

    def test_client_disconnect_mid_stream_logs_no_traceback_or_error(self):
        gate = GatedRaw()
        self.upstream.respond = lambda payload: gate
        gate.push(chunk('first '))
        err = StringIO()
        with redirect_stderr(err):
            conn = HTTPConnection('127.0.0.1', self.port, timeout=15)
            self.addCleanup(conn.close)
            conn.request('POST', '/v1/chat/completions', json.dumps(
                {'stream': True, 'messages': [{'role': 'user', 'content': 'go'}]}),
                {'Authorization': f'Bearer {TOKEN}'})
            response = conn.getresponse()
            seen = b''
            while b'first ' not in seen:
                seen += response.fp.readline()
            response.close()
            conn.close()
            for _ in range(400):
                if gate.closed.wait(0.02):
                    break
                gate.push(chunk('x' * 4096))
            self.assertTrue(gate.closed.is_set())
            self.assertTrue(self.upstream.transports[0].closed)
        self.assertNotIn('Traceback', err.getvalue())
        self.assertNotIn('internal error', err.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())

    def test_client_gone_before_a_non_stream_reply_is_silent(self):
        gate = GatedRaw()
        self.upstream.respond = lambda payload: gate
        err = StringIO()
        with redirect_stderr(err):
            with socket.create_connection(('127.0.0.1', self.port), timeout=15) as sock:
                body = json.dumps({'messages': [{'role': 'user', 'content': 'go'}]})
                sock.sendall((f'POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n'
                              f'Authorization: Bearer {TOKEN}\r\nContent-Length: {len(body)}\r\n\r\n'
                              f'{body}').encode())
                for _ in range(400):
                    if self.upstream.calls:
                        break
                    gate.closed.wait(0.02)
            gate.push(chunk('late'), {'model': 'm', 'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
            self.assertTrue(gate.closed.wait(5))
        self.assertNotIn('Traceback', err.getvalue())
        self.assertNotIn('internal error', err.getvalue())


class RequestHygieneTests(ApiCase):
    def head(self, *lines):
        return ('POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n'
                f'Authorization: Bearer {TOKEN}\r\nConnection: close\r\n' + ''.join(line + '\r\n' for line in lines) + '\r\n')

    def test_missing_content_length_is_400(self):
        status, body = self.raw_exchange(self.head())
        self.assertEqual((status, body['error']['code']), (400, 'invalid_content_length'))

    def test_malformed_content_length_is_400(self):
        for value in ('abc', '-5', '1e3', '12 34', '', '+7', '٣'):
            with self.subTest(value=value):
                status, body = self.raw_exchange(self.head(f'Content-Length: {value}'))
                self.assertEqual((status, body['error']['code']), (400, 'invalid_content_length'))

    def test_oversized_body_is_413_without_reading_it(self):
        status, body = self.raw_exchange(self.head(f'Content-Length: {api.MAX_BODY_BYTES + 1}'))
        self.assertEqual((status, body['error']['code']), (413, 'body_too_large'))
        self.assertEqual(self.upstream.calls, [])

    def test_body_at_the_cap_is_not_rejected_for_size(self):
        status, body = self.raw_exchange(self.head('Content-Length: 0'))
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], 'invalid_json')

    def test_chunked_bodies_are_refused(self):
        status, body = self.raw_exchange(self.head('Transfer-Encoding: chunked', 'Content-Length: 2') + '{}')
        self.assertEqual(status, 400)

    def test_short_body_is_400(self):
        status, _ = self.raw_exchange(self.head('Content-Length: 50') + '{"a":1}')
        self.assertEqual(status, 400)

    def test_bad_json_and_wrong_shapes_are_400_and_openai_shaped(self):
        for raw in ('{nope', '[1,2]', '"text"', 'null', '\xff'.encode('latin-1').decode('latin-1'), '[' * 100000):
            with self.subTest(raw=raw[:20]):
                reply = self.request('POST', '/v1/chat/completions', raw_body=raw.encode('latin-1', 'replace'))
                self.assertEqual(reply.status, 400)
                self.assertEqual(set(reply.json()['error']), {'message', 'type', 'param', 'code'})

    def test_unknown_route_is_404_after_auth(self):
        self.assertEqual(self.request('GET', '/v2/nothing').status, 404)
        self.assertEqual(self.request('POST', '/v1/models', {}).status, 404)

    def test_aliases_without_v1_work(self):
        self.assertEqual(self.request('POST', '/chat/completions',
                                      {'messages': [{'role': 'user', 'content': 'x'}]}).status, 200)


class QuickTunnelTests(ApiCase):
    def test_quick_tunnel_host_with_stream_is_400(self):
        for headers in ({'Host': 'abc-def' + QUICK_TUNNEL_HOST_SUFFIX},
                        {'Host': 'abc-def' + QUICK_TUNNEL_HOST_SUFFIX + ':443'},
                        {'Host': 'ABC' + QUICK_TUNNEL_HOST_SUFFIX.upper()},
                        {'X-Forwarded-Host': 'abc' + QUICK_TUNNEL_HOST_SUFFIX},
                        {'X-Forwarded-Host': 'proxy.local, abc' + QUICK_TUNNEL_HOST_SUFFIX}):
            with self.subTest(headers=headers):
                reply = self.chat({'stream': True}, headers=headers)
                self.assertEqual(reply.status, 400)
                error = reply.json()['error']
                self.assertEqual(error['param'], 'stream')
                self.assertIn('quick tunnels', error['message'])
        self.assertEqual(self.upstream.calls, [])

    def test_quick_tunnel_host_without_stream_and_other_hosts_still_work(self):
        host = {'Host': 'abc' + QUICK_TUNNEL_HOST_SUFFIX}
        self.assertEqual(self.chat(headers=host).status, 200)
        self.assertEqual(self.chat({'stream': False}, headers=host).status, 200)
        self.assertEqual(self.chat({'stream': True}, headers={'Host': 'api.example.com'}).status, 200)
        self.assertEqual(self.chat({'stream': True}, headers={'Host': 'nottrycloudflare.com'}).status, 200)


class TunnelWiringTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patch.dict(os.environ, {'BLEO_HOME': self.home.name}, clear=True).start()
        patch('bleo.api._make_stop_event', side_effect=preset_event).start()
        self.addCleanup(patch.stopall)

    def run_main(self, argv, tunnel_mock):
        out, err = StringIO(), StringIO()
        with patch('bleo.tunnel.Tunnel', tunnel_mock), redirect_stdout(out), redirect_stderr(err):
            code = api.main(argv)
        token = (Path(self.home.name) / 'api-token').read_text().strip()
        self.assertNotIn(token, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_named_cloudflare_tunnel_starts_after_listening_and_stops_on_exit(self):
        mock = Mock()
        mock.return_value.url = None
        code, out, _ = self.run_main(['--port', '0', '--tunnel', 'cloudflare', '--tunnel-name', 'mine'], mock)
        port = int(out.split('listening on http://127.0.0.1:')[1].split()[0].split(':')[-1])
        args, kwargs = mock.call_args
        self.assertEqual((code, args), (0, ('cloudflare', port)))
        self.assertEqual((kwargs['name'], kwargs['quick'], kwargs['https_port']), ('mine', False, 8443))
        mock.return_value.start.assert_called_once_with()
        mock.return_value.stop.assert_called_once_with()
        self.assertIn('named tunnel mine connected', out)
        self.assertIn('cloudflared tunnel route dns mine <hostname>', out)
        self.assertLess(out.index('listening'), out.index('named tunnel mine connected'))

    def test_quick_and_tailscale_options_are_passed_through(self):
        mock = Mock()
        mock.return_value.url = 'https://abc.trycloudflare.com'
        self.run_main(['--port', '0', '--tunnel', 'cloudflare', '--quick'], mock)
        self.assertEqual((mock.call_args[1]['quick'], mock.call_args[1]['name']), (True, None))
        mock = Mock()
        mock.return_value.url = 'https://host.ts.net:9443'
        _, out, _ = self.run_main(['--port', '0', '--tunnel', 'tailscale', '--tailscale-port', '9443'], mock)
        self.assertEqual((mock.call_args[0][0], mock.call_args[1]['https_port']), ('tailscale', 9443))
        self.assertIn('public URL: https://host.ts.net:9443', out)

    def test_tunnel_error_exits_nonzero_with_message_and_cleans_up(self):
        mock = Mock()
        mock.return_value.start.side_effect = TunnelError('cloudflared is not installed; install it')
        code, _, err = self.run_main(['--port', '0', '--tunnel', 'cloudflare', '--tunnel-name', 'n'], mock)
        self.assertNotEqual(code, 0)
        self.assertIn('cloudflared is not installed', err)
        mock.return_value.stop.assert_called()

    def test_constructor_error_exits_nonzero(self):
        mock = Mock(side_effect=TunnelError('named tunnel required for streaming'))
        code, _, err = self.run_main(['--port', '0', '--tunnel', 'cloudflare'], mock)
        self.assertNotEqual(code, 0)
        self.assertIn('named tunnel required', err)

    def test_tunnel_flags_without_their_tunnel_are_usage_errors(self):
        for argv in (['--quick'], ['--tunnel-name', 'x'], ['--tunnel', 'tailscale', '--quick'],
                     ['--tunnel', 'cloudflare', '--tailscale-port', '9000'], ['--model', 'nope']):
            with self.subTest(argv=argv), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                api.main(argv)

    def test_sigterm_stops_the_tunnel(self):
        mock = Mock()
        mock.return_value.url = 'named tunnel n'
        timer = threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
        out, err = StringIO(), StringIO()
        with patch('bleo.api._make_stop_event', side_effect=threading.Event), \
                patch('bleo.tunnel.Tunnel', mock), redirect_stdout(out), redirect_stderr(err):
            timer.start()
            code = api.main(['--port', '0', '--tunnel', 'cloudflare', '--tunnel-name', 'n'])
        timer.join()
        self.assertEqual(code, 0)
        mock.return_value.stop.assert_called_once_with()



class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patch.dict(os.environ, {'BLEO_HOME': self.home.name}, clear=True).start()
        self.addCleanup(patch.stopall)

    def exhausted(self, cause):
        try:
            raise api.RelayExhaustedError('Relays exhausted', last_status=503) from cause
        except api.RelayExhaustedError as error:
            return error

    def test_direct_fallback_timeout_is_a_504(self):
        error = api.upstream_error(self.exhausted(TimeoutError('slow')))
        self.assertEqual((error.status, error.code), (504, 'upstream_timeout'))

    def test_direct_fallback_connection_failure_is_upstream_unavailable(self):
        error = api.upstream_error(self.exhausted(ConnectionResetError('reset')))
        self.assertEqual((error.status, error.code), (502, 'upstream_unavailable'))

    def test_other_exhaustion_stays_an_upstream_error(self):
        error = api.upstream_error(api.RelayExhaustedError('Relays exhausted and direct fallback is off'))
        self.assertEqual((error.status, error.code), (502, 'upstream_error'))

    def test_shared_router_uses_the_upstream_timeout(self):
        from bleo.relay_state import RelayPool
        RelayPool().add('https://relay.example.com', auth='s')
        router = api.shared_router()
        self.assertEqual((router.header_timeout, router.stall_timeout),
                         (api.UPSTREAM_TIMEOUT, api.UPSTREAM_TIMEOUT))


if __name__ == '__main__':
    unittest.main()
