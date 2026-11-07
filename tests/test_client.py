from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.client import IncompleteRead
import base64
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from bleo import APIError, AuthError, Bleo, ClaudeHaiku, GptLuna, RatelimitError
from bleo.client import _image_data_url, _make_digest, _make_hmac_signature
from helpers import Raw, ScriptedTransport, chunk, completion, sse


class ClientTests(unittest.TestCase):
    def setUp(self):
        # An empty BLEO_HOME keeps the user's saved relays out of these offline tests.
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.environment = patch.dict(os.environ, {'BLEO_HOME': home.name}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.sleep = patch('bleo.client.time.sleep').start()
        self.addCleanup(patch.stopall)

    def client(self, *responses, **kwargs):
        client = Bleo(**kwargs)
        client._client = ScriptedTransport(*responses)
        self.addCleanup(client.close)
        return client

    def test_luna_is_default_in_client_and_history(self):
        with Bleo() as client:
            self.assertEqual(client.model, GptLuna)
            self.assertEqual(client.history.model, GptLuna)

    def test_current_endpoint_payload_and_utf8_signature(self):
        client = self.client(completion(), pre_shared_key='test-key')
        self.assertEqual(client.ask('Città 🦆'), 'OK')
        method, url, options = client._client.calls[0]
        self.assertEqual((method, url), ('POST', 'https://ai-chat.bsg.brave.com/v1/chat/completions'))
        payload = client._client.payload()
        self.assertEqual(payload['model'], GptLuna)
        self.assertEqual(payload['messages'], [{'role': 'user', 'content': [{'type': 'text', 'text': 'Città 🦆'}]}])
        self.assertEqual(payload['brave_capability'], ['chat'])
        self.assertTrue(payload['stream'])
        self.assertNotIn('events', payload)
        self.assertIn('Città 🦆', options['content'])
        self.assertEqual(options['headers']['digest'], 'SHA-256=' + _make_digest(options['content']))
        self.assertNotIn('br', options['headers']['Accept-Encoding'].split(', '))

    def test_signature_known_utf8_vector(self):
        body = '{"message":"città"}'
        self.assertEqual(_make_digest(body), 'bG64zVp02ugMm8lHPOvqgyk5z+jYszlGfNZdyFhIsR8=')
        self.assertEqual(_make_hmac_signature(body, 'secret'), 'xTJsx1nhD2ulKxQ3Qn8CZpaWVmwhBNloMhpCSL8WJk8=')

    def test_environment_keys_and_explicit_precedence(self):
        env = {'BRAVE_X_BRAVE_KEY': 'env-public', 'BRAVE_PRE_SHARED_KEY': 'env-secret',
               'BRAVE_SIGNING_KEY_ID': 'custom-id', 'BRAVE_LEO_PREMIUM_CREDENTIAL': 'ticket'}
        with patch.dict(os.environ, env), Bleo(x_brave_key='explicit') as client:
            headers = client._headers('{}')
            self.assertEqual(headers['x-brave-key'], 'explicit')
            self.assertIn('keyId="custom-id"', headers['Authorization'])
            self.assertEqual(headers['Cookie'], '__Secure-sku#brave-leo-premium=ticket')
            self.assertEqual(client.pre_shared_key, 'env-secret')

    def test_empty_signing_key_disables_signature(self):
        with Bleo(pre_shared_key='') as client:
            headers = client._headers('{}', stream=False)
            self.assertNotIn('Authorization', headers)
            self.assertNotIn('digest', headers)
            self.assertEqual(headers['Accept'], 'application/json')

    def test_bundled_signing_key_id_stays_paired(self):
        with Bleo() as client:
            self.assertEqual(client.signing_key_id, 'linux-148-release')

    def test_premium_credential_selects_premium_host(self):
        client = self.client(completion(), premium_credential='ticket')
        client.ask('hello')
        self.assertIn('ai-chat-premium.bsg.brave.com', client._client.calls[0][1])

    def test_alias_override_does_not_change_default(self):
        client = self.client(completion())
        client.ask('hello', model='haiku')
        self.assertEqual(client._client.payload()['model'], ClaudeHaiku)
        self.assertEqual(client.model, GptLuna)

    def test_multimodal_blocks_are_preserved(self):
        client = self.client(completion())
        prompt = ['Describe', {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]
        client.ask(prompt)
        blocks = client._client.payload()['messages'][0]['content']
        self.assertEqual(blocks[0], {'type': 'text', 'text': 'Describe'})
        self.assertEqual(blocks[1], prompt[1])

    def test_image_call_uses_vision_model_when_selected_model_is_text_only(self):
        client = self.client(completion(), model=ClaudeHaiku)
        encoded = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nimage').decode()
        with patch('bleo.client._image_data_url', return_value=encoded):
            self.assertEqual(client.ask_with_image('Review this image', 'https://images.example.test/photo.jpg'), 'OK')
        payload = client._client.payload()
        self.assertEqual(payload['model'], 'qwen-3-235b')
        blocks = payload['messages'][0]['content']
        self.assertEqual(blocks[0], {'type': 'text', 'text': 'Review this image'})
        self.assertEqual(blocks[1]['image_url']['url'], encoded)

    def test_image_call_keeps_default_when_vision_capable(self):
        client = self.client(completion())
        encoded = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nimage').decode()
        with patch('bleo.client._image_data_url', return_value=encoded):
            self.assertEqual(client.ask_with_image('Review this image', 'https://images.example.test/photo.jpg'), 'OK')
        self.assertEqual(client._client.payload()['model'], GptLuna)

    def test_remote_image_is_downloaded_and_inlined(self):
        data = b'\x89PNG\r\n\x1a\nimage'
        response = Mock()
        response.headers.get_content_type.return_value = 'image/png'
        response.read.return_value = data
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        address = ('family', 0, 0, '', ('93.184.216.34', 443))
        with patch('bleo.client.socket.getaddrinfo', return_value=[address]), \
                patch('bleo.client.build_opener', return_value=opener):
            result = _image_data_url('https://images.example.test/photo.png')
        self.assertEqual(result, 'data:image/png;base64,' + base64.b64encode(data).decode())
        opener.open.assert_called_once()

    def test_image_data_rejects_invalid_bytes_and_private_hosts(self):
        with self.assertRaisesRegex(ValueError, 'valid JPEG'):
            _image_data_url('data:image/png;base64,AA==')
        address = ('family', 0, 0, '', ('127.0.0.1', 443))
        with patch('bleo.client.socket.getaddrinfo', return_value=[address]), \
                self.assertRaisesRegex(ValueError, 'public host'):
            _image_data_url('https://localhost/image.png')

    def test_invalid_content_and_non_chat_models_fail_before_network(self):
        client = self.client()
        for prompt in ('', '  ', None, [], [42], [{}]):
            with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                client.ask(prompt)
        for model in ('near-glm-5', 'near-glm-5-1', 'brave-summary'):
            with self.subTest(model=model), self.assertRaises(ValueError):
                client.ask('hello', model=model)
        self.assertEqual(client._client.calls, [])

    def test_timeout_and_delay_validation(self):
        for kwargs in ({'timeout': 0}, {'timeout': -1}, {'max_retry_delay': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Bleo(**kwargs)

    def test_stream_skips_role_usage_and_receipts(self):
        raw = Raw(sse({'choices': [{'delta': {'role': 'assistant'}}]}, chunk('Hi '), chunk('there'),
                      chunk(finish='stop'), {'usage': {'total_tokens': 5}},
                      {'object': 'brave-chat.contentReceipt'}, '[DONE]'))
        client = self.client(raw)
        self.assertEqual(list(client.stream('hello')), ['Hi ', 'there'])
        self.assertEqual(client.last_model, 'served-model')
        self.assertTrue(raw.closed)

    def test_multiline_sse_and_comments(self):
        body = b': heartbeat\r\nevent: completion\r\ndata: {"choices":\r\ndata: [{"delta":{"content":"OK"},"finish_reason":"stop"}]}\r\n\r\ndata: [DONE]\r\n\r\n'
        client = self.client(Raw(body, chunk_size=1))
        self.assertEqual(client.ask('hello'), 'OK')

    def test_eof_after_stop_is_valid(self):
        client = self.client(Raw(sse(chunk('OK', finish='stop'))))
        self.assertEqual(client.ask('hello'), 'OK')

    def test_extra_continuation_turns_after_stop_are_ignored(self):
        raw = Raw(sse(chunk('{"ok":true}'), chunk(finish='stop'),
                      chunk('The previous reply was complete.'), chunk(finish='stop'), '[DONE]'))
        client = self.client(raw, history=True)
        self.assertEqual(json.loads(client.ask('Return JSON')), {'ok': True})
        self.assertEqual(client.history.messages[-1].content, '{"ok":true}')
        self.assertTrue(raw.closed)

    def test_json_completion_and_content_parts(self):
        body = {'model': 'actual', 'choices': [{'message': {'content': [{'type': 'text', 'text': 'Hi'}, {'type': 'text', 'text': '!'}]}, 'finish_reason': 'stop'}]}
        client = self.client(Raw(json.dumps(body).encode(), headers={'Content-Type': 'application/json; charset=utf-8'}))
        self.assertEqual(client.ask('hello'), 'Hi!')
        self.assertEqual(client.last_model, 'actual')

    def test_empty_invalid_and_error_responses_raise(self):
        cases = [sse('[DONE]'), sse('{bad json'), sse([]), sse({'error': {'message': 'failed'}}),
                 sse({'choices': {}}), sse({'choices': [None]}),
                 sse({'choices': [{'delta': None}]}),
                 sse({'choices': [{'delta': {'content': 123}}]})]
        for body in cases:
            with self.subTest(body=body):
                client = self.client(Raw(body), max_retries=1)
                with self.assertRaises(APIError):
                    client.ask('hello')

    def test_truncated_output_is_not_retried_or_remembered(self):
        client = self.client(Raw(sse(chunk('partial'))), history=True)
        stream = client.stream('hello')
        self.assertEqual(next(stream), 'partial')
        with self.assertRaisesRegex(APIError, 'ended before completion'):
            next(stream)
        self.assertEqual(len(client._client.calls), 1)
        self.assertEqual(client.history.messages, [])
        self.sleep.assert_not_called()

    def test_length_or_filter_stop_is_an_error(self):
        for reason in ('length', 'content_filter', 'tool_calls'):
            with self.subTest(reason=reason):
                client = self.client(Raw(sse(chunk('partial'), chunk(finish=reason))))
                with self.assertRaisesRegex(APIError, reason):
                    client.ask('hello')
                self.assertEqual(len(client._client.calls), 1)

    def test_auth_errors_are_terminal(self):
        for status in (401, 403):
            with self.subTest(status=status):
                client = self.client(Raw(b'private diagnostic', status=status))
                with self.assertRaises(AuthError) as caught:
                    client.ask('hello')
                self.assertNotIn('private diagnostic', str(caught.exception))
                self.assertEqual(len(client._client.calls), 1)
        self.sleep.assert_not_called()

    def test_bad_request_preserves_status_and_body_without_retry(self):
        client = self.client(Raw(b'bad model', status=400))
        with self.assertRaises(APIError) as caught:
            client.ask('hello')
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.body, 'bad model')
        self.sleep.assert_not_called()

    def test_server_and_connection_errors_retry_before_output(self):
        for error in (Raw(b'busy', status=503), OSError('connection reset'), IncompleteRead(b'')):
            with self.subTest(error=error):
                client = self.client(error, completion())
                self.assertEqual(client.ask('hello'), 'OK')
                self.assertEqual(len(client._client.calls), 2)
        self.assertEqual(self.sleep.call_count, 3)

    def test_rate_limit_honors_retry_after(self):
        client = self.client(Raw(b'busy', status=429, headers={'Retry-After': '2'}), completion())
        self.assertEqual(client.ask('hello'), 'OK')
        self.sleep.assert_called_once_with(2)

    def test_rate_limit_http_date(self):
        date = datetime.now(timezone.utc) + timedelta(seconds=15)
        client = self.client(Raw(b'busy', status=429, headers={'Retry-After': format_datetime(date)}), completion())
        self.assertEqual(client.ask('hello'), 'OK')
        delay = self.sleep.call_args.args[0]
        self.assertGreater(delay, 12)
        self.assertLessEqual(delay, 15)

    def test_long_rate_limit_from_json_is_returned_without_sleep(self):
        date = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        body = json.dumps({'error': {'rate_limit_expires_at': date}}).encode()
        client = self.client(Raw(body, status=429))
        with self.assertRaises(RatelimitError) as caught:
            client.ask('hello')
        self.assertGreater(caught.exception.retry_after, 3500)
        self.sleep.assert_not_called()

    def test_stream_rate_limit_retries_before_output(self):
        client = self.client(Raw(sse({'error': {'type': '429-limit'}})), completion())
        self.assertEqual(client.ask('hello'), 'OK')
        self.sleep.assert_called_once_with(60)

    def test_exhaustion_does_not_sleep_after_last_attempt(self):
        client = self.client(OSError('offline'), OSError('offline'), OSError('offline'))
        with self.assertRaises(OSError):
            client.ask('hello')
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1.5, 3])
        self.assertEqual(len(client._client.calls), 3)

    def test_failure_after_text_never_duplicates_output(self):
        client = self.client(Raw(sse(chunk('one'), {'error': {'type': '429-limit'}})), completion())
        stream = client.stream('hello')
        self.assertEqual(next(stream), 'one')
        with self.assertRaises(RatelimitError):
            next(stream)
        self.assertEqual(len(client._client.calls), 1)
        self.sleep.assert_not_called()

    def test_ask_stream_and_stream_commit_history_once(self):
        client = self.client(completion('first'), completion('second'), completion('third'), history=True)
        self.assertEqual(client.ask('one'), 'first')
        self.assertEqual(''.join(client.stream('two')), 'second')
        self.assertEqual(''.join(client.ask_stream('three')), 'third')
        self.assertEqual([message.content for message in client.history.messages],
                         ['one', 'first', 'two', 'second', 'three', 'third'])
        sent = client._client.payload(2)['messages']
        self.assertEqual([m['role'] for m in sent], ['user', 'assistant', 'user', 'assistant', 'user'])

    def test_history_opt_in_and_per_request_override(self):
        client = self.client(completion(), completion(), completion(), completion())
        client.ask('one')
        self.assertEqual(client.history.messages, [])
        client.ask('two', remember=True)
        client.enable_history()
        client.ask('three', remember=False)
        self.assertEqual(len(client._client.payload(2)['messages']), 1)
        client.ask('four')
        self.assertEqual(len(client._client.payload(3)['messages']), 3)
        client.disable_history()
        self.assertFalse(client.history_enabled)
        self.assertEqual(client.history.messages, [])

    def test_failed_turn_keeps_previous_history(self):
        client = self.client(completion(), Raw(b'bad request', status=400), history=True)
        client.ask('first')
        saved = client.history.to_messages()
        with self.assertRaises(APIError):
            client.ask('failed')
        self.assertEqual(client.history.to_messages(), saved)
        client.reset()
        self.assertEqual(client.history.messages, [])

    def test_cancelled_stream_closes_response_without_history(self):
        raw = completion()
        client = self.client(raw, history=True)
        stream = client.stream('hello')
        self.assertEqual(next(stream), 'OK')
        stream.close()
        self.assertTrue(raw.closed)
        self.assertEqual(client.history.messages, [])

    def test_last_model_is_cleared_between_requests(self):
        client = self.client(completion(), Raw(b'bad request', status=400))
        client.ask('hello')
        self.assertEqual(client.last_model, 'served-model')
        with self.assertRaises(APIError):
            client.ask('again')
        self.assertIsNone(client.last_model)

    def test_blank_server_model_is_unknown(self):
        client = self.client(Raw(sse(chunk('OK', finish='stop', model=' '))))
        self.assertEqual(client.ask('hello'), 'OK')
        self.assertIsNone(client.last_model)

    def test_context_manager_closes_transport_on_exception(self):
        client = self.client()
        with self.assertRaises(RuntimeError):
            with client:
                raise RuntimeError('test')
        self.assertTrue(client._client.closed)


class RatelimitTests(unittest.TestCase):
    def test_numeric_negative_and_invalid_delays(self):
        for value, expected in [('2.5', 2.5), (-5, 0), (None, None), ('bad', None),
                                ('nan', None), ('inf', None), ({}, None)]:
            with self.subTest(value=value):
                self.assertEqual(RatelimitError(retry_after=value).retry_after, expected)

    def test_past_date_never_has_negative_delay(self):
        self.assertEqual(RatelimitError(retry_after='2000-01-01T00:00:00Z').retry_after, 0)
