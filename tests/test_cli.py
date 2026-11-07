from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bleo import Bleo, ClaudeHaiku, GptLuna
from bleo import cli
from helpers import Raw, ScriptedTransport, completion


class CliTests(unittest.TestCase):
    def setUp(self):
        # An empty BLEO_HOME keeps the user's saved relays out of these offline tests.
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        environment = patch.dict('os.environ', {'BLEO_HOME': home.name})
        environment.start()
        self.addCleanup(environment.stop)

    def run_cli(self, args, *responses, inputs=()):
        self.output, self.errors = StringIO(), StringIO()
        self.transport = ScriptedTransport(*responses)
        self.client = Bleo()
        self.client._client = self.transport

        def factory(**kwargs):
            self.options = kwargs
            self.client.history_enabled = kwargs['history']
            return self.client

        with patch('sys.argv', ['bleo', *args]), patch('bleo.cli.Bleo', side_effect=factory), \
             patch('builtins.input', side_effect=inputs), redirect_stdout(self.output), redirect_stderr(self.errors):
            cli.main()

    def test_one_shot_uses_luna_and_closes(self):
        self.run_cli(['hello'], completion())
        self.assertEqual(self.options['model'], GptLuna)
        self.assertEqual(self.output.getvalue(), 'OK\n')
        self.assertTrue(self.transport.closed)

    def test_no_stream_and_explicit_options(self):
        self.run_cli(['--no-stream', '--model', 'luna', '--signing-key-id', 'test-id',
                      '--proxy', 'http://localhost:8080', 'hello'], completion())
        self.assertEqual(self.options['model'], 'luna')
        self.assertEqual(self.options['signing_key_id'], 'test-id')
        self.assertEqual(self.options['proxy'], 'http://localhost:8080')
        self.assertEqual(self.output.getvalue(), 'OK\n')

    def test_list_models_is_offline_and_labels_access(self):
        self.run_cli(['--list-models', '--access', 'free'])
        text = self.output.getvalue()
        self.assertIn('claude-3-haiku', text)
        self.assertIn('compatibility ID', text)
        self.assertIn('bedrock-openai.gpt-5.6-luna', text)
        self.assertNotIn('claude-3-sonnet', text)
        self.assertEqual(self.transport.calls, [])

    def test_models_defaults_to_free_catalog(self):
        self.run_cli(['--models'])
        text = self.output.getvalue()
        self.assertIn('Models · Bleo · free', text)
        self.assertIn('claude-3-haiku', text)
        self.assertNotIn('claude-3-sonnet', text)

    def test_models_all_includes_premium_catalog(self):
        self.run_cli(['--models-all'])
        text = self.output.getvalue()
        self.assertIn('Models · Bleo', text)
        self.assertIn('claude-3-sonnet', text)

    def test_refresh_models_is_explicit(self):
        with patch('bleo.cli.refresh_models') as refresh:
            self.run_cli(['--refresh-models'])
        refresh.assert_called_once_with()
        self.assertIn('claude-3-haiku', self.output.getvalue())

    def test_failure_returns_nonzero_without_traceback_and_closes(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(['hello'], Raw(b'bad credentials', status=401))
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('authentication failed', self.errors.getvalue())
        self.assertNotIn('Traceback', self.errors.getvalue())
        self.assertTrue(self.transport.closed)

    def test_interactive_history_records_each_turn_once(self):
        self.run_cli([], completion('first'), completion('second'),
                     inputs=['one', 'two', '/history', '/quit'])
        self.assertTrue(self.options['history'])
        self.assertEqual(len(self.client.history.messages), 4)
        self.assertIn('assistant: second', self.output.getvalue())
        self.assertTrue(self.transport.closed)

    def test_interactive_model_switch_and_clear(self):
        self.run_cli([], completion(), inputs=['/model haiku', 'hello', '/clear', '/q'])
        self.assertEqual(self.client.model, ClaudeHaiku)
        self.assertEqual(self.transport.payload()['model'], ClaudeHaiku)
        self.assertEqual(self.client.history.messages, [])
        self.assertEqual(self.client.history.model, ClaudeHaiku)

    def test_read_file_commits_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prompt.txt'
            path.write_text('Città 🦆', encoding='utf-8')
            self.run_cli([], completion(), inputs=[f'/read {path}', '/quit'])
        self.assertEqual([m.content for m in self.client.history.messages], ['Città 🦆', 'OK'])

    def test_repl_recovers_from_request_and_file_errors(self):
        self.run_cli([], Raw(b'forbidden', status=403), completion(),
                     inputs=['failed', '/read /missing/file', 'hello', '/q'])
        self.assertIn('authentication failed', self.errors.getvalue())
        self.assertIn('/missing/file', self.errors.getvalue())
        self.assertEqual([m.content for m in self.client.history.messages], ['hello', 'OK'])
        self.assertTrue(self.transport.closed)

    def test_eof_closes_client(self):
        self.run_cli([], inputs=[EOFError])
        self.assertTrue(self.transport.closed)

    def test_interrupt_returns_130_and_closes_client(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(['hello'], KeyboardInterrupt())
        self.assertEqual(caught.exception.code, 130)
        self.assertTrue(self.transport.closed)
