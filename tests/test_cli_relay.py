"""bleo relay: manage the saved pool, write templates and probe one relay."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from functools import partial
from io import StringIO
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from bleo import Bleo, cli
from bleo.relay import RelayRouter
from bleo.relay_state import RelayEntry, RelayPool
from helpers import ScriptedTransport, completion
from test_client_relay import RelayFixture, stream_reply
from test_relay import reply

SECRET = 'relay-secret-aaaabbbbccccdddd'
OTHER = 'second-secret-eeeeffff00001111'


class CliRelayCase(RelayFixture, unittest.TestCase):
    def setUp(self):
        self.setUpFixture()
        self.environ = patch.dict(os.environ, {'BLEO_HOME': self.home})
        self.environ.start()
        self.addCleanup(self.environ.stop)
        os.environ.pop('BRAVE_LEO_PREMIUM_CREDENTIAL', None)
        self.everything = ''

    def run_cli(self, *args, router=None):
        out, err = StringIO(), StringIO()
        contexts = [patch('sys.argv', ['bleo', *args]), redirect_stdout(out), redirect_stderr(err)]
        if router is not None:
            contexts.append(patch('bleo.cli.RelayRouter', router))
        code = 0
        with ExitStack() as stack:
            for context in contexts:
                stack.enter_context(context)
            try:
                cli.main()
            except SystemExit as exit_:
                code = exit_.code or 0
        self.out, self.err = out.getvalue(), err.getvalue()
        self.everything += self.out + self.err
        return code

    def insecure_router(self):
        return partial(RelayRouter, allow_insecure_for_tests=True)

    def save_relays(self, *relays, mode='auto'):
        entries = [RelayEntry(server.url, label, secret, '') for server, label, secret in relays]
        RelayPool(entries=entries, mode=mode).save()

    def assertNoSecrets(self, *secrets):
        for secret in secrets or (SECRET, OTHER):
            self.assertNotIn(secret, self.everything)


class RelayManagementTests(CliRelayCase):
    def test_add_list_mode_remove_round_trip(self):
        self.assertEqual(self.run_cli('relay', 'add', 'https://one.example.com', '--auth', SECRET, '--label', 'one'), 0)
        self.assertEqual(self.run_cli('relay', 'add', 'https://two.example.com', '--auth', OTHER), 0)
        self.assertEqual(self.run_cli('relay', 'list'), 0)
        listing = self.out
        self.assertIn('mode: auto', listing)
        self.assertRegex(listing, r'\* one  https://one.example.com  auth: set')
        self.assertRegex(listing, r'  two  https://two.example.com  auth: set')
        self.assertEqual(self.run_cli('relay', 'mode', 'spread'), 0)
        self.run_cli('relay', 'list')
        self.assertIn('mode: spread', self.out)
        self.assertEqual(self.run_cli('relay', 'mode', 'sticky'), 0)
        self.assertEqual(RelayPool.load().mode, 'auto')
        self.assertEqual(self.run_cli('relay', 'mode', 'off'), 0)
        self.assertEqual(RelayPool.load().mode, 'off')
        self.assertEqual(self.run_cli('relay', 'remove', 'ONE'), 0)
        self.assertEqual(self.run_cli('relay', 'remove', 'https://two.example.com'), 0)
        self.run_cli('relay', 'list')
        self.assertIn('no relays saved', self.out)
        self.assertEqual(RelayPool.load().entries, ())
        self.assertNoSecrets()

    def test_saved_secret_is_stored_but_list_never_prints_it(self):
        self.run_cli('relay', 'add', 'https://one.example.com', '--auth', SECRET)
        self.assertEqual(RelayPool.load().entries[0].auth, SECRET)
        self.run_cli('relay', 'list')
        self.assertIn('auth: set', self.out)
        self.assertNoSecrets()

    def test_add_rejects_bad_input_without_echoing_the_secret(self):
        self.assertEqual(self.run_cli('relay', 'add', 'http://insecure.example.com', '--auth', SECRET), 1)
        self.assertEqual(self.run_cli('relay', 'add', 'https://one.example.com', '--auth', '  '), 1)
        self.assertEqual(self.run_cli('relay', 'remove', 'ghost'), 1)
        self.assertEqual(RelayPool.load().entries, ())
        self.assertNoSecrets()

    def test_template_writes_files_and_prints_one_fresh_secret(self):
        out = Path(self.home) / 'deployable'
        self.assertEqual(self.run_cli('relay', 'template', 'cloudflare', '--out', str(out)), 0)
        self.assertTrue((out / 'worker.js').is_file())
        secrets = re.findall(r'\b[0-9a-f]{64}\b', self.out)
        self.assertEqual(len(secrets), 1)
        self.assertIn('BLEO_RELAY_SECRET', self.out)
        self.assertIn('relay add', self.out)
        self.assertNotIn(secrets[0], (out / 'worker.js').read_text())
        self.run_cli('relay', 'template', 'deno', '--out', str(Path(self.home) / 'again'))
        self.assertNotEqual(re.findall(r'\b[0-9a-f]{64}\b', self.out), secrets)

    def test_template_defaults_to_a_folder_named_for_the_platform(self):
        previous = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, previous)
        self.assertEqual(self.run_cli('relay', 'template', 'deno'), 0)
        self.assertTrue((Path(self.home) / 'bleo-relay-deno' / 'main.ts').is_file())
        self.assertEqual(self.run_cli('relay', 'template', 'deno'), 1)  # refuses to overwrite

    def test_prompt_and_verbose_still_work_beside_the_relay_command(self):
        client = Bleo(relays=False)
        client._client = ScriptedTransport(completion(), completion())
        with patch('bleo.cli.Bleo', return_value=client):
            self.assertEqual(self.run_cli('-v', 'hello'), 0)
        self.assertEqual(self.out, 'OK\n')
        self.assertEqual(self.err, 'route: direct\n')
        with patch('bleo.cli.Bleo', return_value=client):
            self.assertEqual(self.run_cli('hello'), 0)
        self.assertEqual((self.out, self.err), ('OK\n', ''))

    def test_verbose_names_the_relay_that_served_the_reply(self):
        router = self.router_over(stream_reply(['relayed']))
        client = Bleo(relays=router)
        with patch('bleo.cli.Bleo', return_value=client):
            self.assertEqual(self.run_cli('-v', 'hello'), 0)
        self.assertEqual(self.out, 'relayed\n')
        self.assertEqual(self.err, 'route: r0\n')
        self.assertNoSecrets('secret-r0')


class RelayProbeTests(CliRelayCase):
    def test_probe_passes_against_a_working_relay_with_no_direct_request(self):
        relay = self.serve(stream_reply(['h', 'é', 'llo ✓'], gap=0.05))
        self.save_relays((relay, 'good', SECRET))
        self.assertEqual(self.run_cli('relay', 'probe', 'good', router=self.insecure_router()), 0)
        self.assertIn('route: relay good', self.out)
        self.assertIn('http status: 200', self.out)
        self.assertIn('content chunks: 3', self.out)
        spread = float(re.search(r'first to last: ([\d.]+)s', self.out).group(1))
        self.assertGreaterEqual(spread, 0.09)  # chunks arrived over time, not in one burst
        self.assertIn('reply decoded as UTF-8: yes', self.out)
        self.assertRegex(self.out, r'seconds to headers: \d+\.\d{3}')
        self.assertIn('result: PASS', self.out)
        self.assertEqual(self.brave.hits, [])
        hit = relay.hits[0]
        self.assertEqual(hit.headers['x-relay-auth'], SECRET)
        self.assertEqual(hit.headers['accept-encoding'], 'identity')
        body = json.loads(hit.body)
        self.assertTrue(body['stream'])
        self.assertIn('héllo ✓', body['messages'][0]['content'][0]['text'])
        self.assertNotIn('cookie', hit.headers)
        self.assertNoSecrets()

    def test_probe_fails_against_a_failing_relay_without_going_direct(self):
        relay = self.serve(reply(status=503, body=b'down'))
        self.save_relays((relay, 'bad', SECRET))
        self.assertEqual(self.run_cli('relay', 'probe', 'bad', '--no-direct', router=self.insecure_router()), 1)
        self.assertIn('result: FAIL', self.out)
        self.assertNotIn('PASS', self.out)
        self.assertEqual(len(relay.hits), 1)
        self.assertEqual(self.brave.hits, [])
        self.assertNoSecrets()

    def test_probe_fails_when_the_relay_is_unreachable(self):
        relay = self.serve(reply())
        self.save_relays((relay, 'gone', SECRET))
        relay.stop()
        self.assertEqual(self.run_cli('relay', 'probe', 'gone', router=self.insecure_router()), 1)
        self.assertIn('result: FAIL', self.out)
        self.assertEqual(self.brave.hits, [])
        self.assertNoSecrets()

    def test_probe_fails_when_the_relay_answers_ok_with_no_content(self):
        relay = self.serve(stream_reply([]))
        self.save_relays((relay, 'empty', SECRET))
        self.assertEqual(self.run_cli('relay', 'probe', 'empty', router=self.insecure_router()), 1)
        self.assertIn('content chunks: 0', self.out)
        self.assertIn('result: FAIL', self.out)

    def test_probe_needs_a_saved_relay(self):
        self.assertEqual(self.run_cli('relay', 'probe', 'ghost'), 1)
        self.assertIn('no relay named', self.err)
        self.assertEqual(self.brave.hits, [])

    def test_probe_does_not_change_the_saved_pool(self):
        first = self.serve(stream_reply())
        second = self.serve(stream_reply())
        self.save_relays((first, 'a', SECRET), (second, 'b', OTHER))
        path = Path(RelayPool.load().path)
        before = path.read_bytes()
        self.assertEqual(self.run_cli('relay', 'probe', 'b', router=self.insecure_router()), 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((len(first.hits), len(second.hits)), (0, 1))

    def test_probe_also_probes_the_premium_host_and_reports_it_separately(self):
        premium = 'premium-credential-value-12345'
        relay = self.serve(stream_reply(['ok']))
        self.save_relays((relay, 'both', SECRET))
        with patch.dict(os.environ, {'BRAVE_LEO_PREMIUM_CREDENTIAL': premium}), \
             patch('bleo.client._PREMIUM_BASE', 'http://premium.invalid'):
            self.assertEqual(self.run_cli('relay', 'probe', 'both', router=self.insecure_router()), 0)
        self.assertIn('via free host', self.out)
        self.assertIn('via premium host http://premium.invalid', self.out)
        self.assertEqual(self.out.count('result: PASS'), 2)
        free, paid = relay.hits
        self.assertEqual(free.headers['x-relay-target'], self.brave.url)
        self.assertNotIn('cookie', free.headers)
        self.assertEqual(paid.headers['x-relay-target'], 'http://premium.invalid')
        self.assertIn(premium, paid.headers['cookie'])
        self.assertNoSecrets(SECRET, premium)

    def test_probe_fails_overall_when_only_the_premium_probe_fails(self):
        calls = []

        def behavior(handler, server):
            calls.append(handler.headers.get('x-relay-target'))
            (stream_reply(['ok']) if len(calls) == 1 else reply(status=503))(handler, server)
        relay = self.serve(behavior)
        self.save_relays((relay, 'half', SECRET))
        with patch.dict(os.environ, {'BRAVE_LEO_PREMIUM_CREDENTIAL': 'cred-xyz'}):
            self.assertEqual(self.run_cli('relay', 'probe', 'half', router=self.insecure_router()), 1)
        self.assertEqual(self.out.count('result: PASS'), 1)
        self.assertEqual(self.out.count('result: FAIL'), 1)
        self.assertNoSecrets(SECRET, 'cred-xyz')


if __name__ == '__main__':
    unittest.main()
