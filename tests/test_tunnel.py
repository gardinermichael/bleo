import json
import subprocess
import threading
import unittest
from unittest.mock import patch

from bleo import tunnel
from bleo.exceptions import BleoError
from bleo.tunnel import Tunnel, TunnelError


API_PORT = 8317
TARGET = 'http://127.0.0.1:8317'
OWNED = {'TCP': {'8443': {'HTTPS': True}},
         'Web': {'box.tail.ts.net:8443': {'Handlers': {'/': {'Proxy': TARGET}}}}}
DNS_STATUS = json.dumps({'Self': {'DNSName': 'box.tail.ts.net.'}})
SECRET_LIKE = ('Authorization', 'Bearer')


def completed(args, stdout='', returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr='')


class FakeProc:
    def __init__(self, stderr=(), hang_on_term=False):
        self.stderr = stderr
        self.hang_on_term = hang_on_term
        self.terminated = False
        self.killed = False
        self.exited = False
        self.wait_calls = []

    def poll(self):
        return 0 if self.exited else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.exited = True

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.hang_on_term and not self.killed and timeout is not None:
            raise subprocess.TimeoutExpired('cloudflared', timeout)
        self.exited = True
        return 0


class FakeRunner:
    """Stands in for the subprocess module: scripted run() results and one fake Popen child."""

    def __init__(self, responses=None, proc=None):
        self.responses = {key: list(value) for key, value in (responses or {}).items()}
        self.proc = proc
        self.run_calls = []
        self.popen_calls = []

    def run(self, args, **kwargs):
        args = list(args)
        self.run_calls.append(args)
        queue = self.responses.get(tuple(args))
        if not queue:
            return completed(args)
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def Popen(self, args, **kwargs):
        self.popen_calls.append(list(args))
        return self.proc

    def ran(self, args):
        return list(args) in self.run_calls


STATUS = ('tailscale', 'serve', 'status', '--json')
INSTALL = ('tailscale', 'serve', '--bg', '--https=8443', TARGET)
OFF = ('tailscale', 'serve', '--https=8443', 'off')
TS_DNS = ('tailscale', 'status', '--json')


class CommandTests(unittest.TestCase):
    def test_quick_command(self):
        self.assertEqual(tunnel.cloudflare_command(API_PORT, quick=True),
                         ['cloudflared', 'tunnel', '--no-autoupdate', '--url', TARGET])

    def test_named_command(self):
        self.assertEqual(tunnel.cloudflare_command(API_PORT, name='bleo'),
                         ['cloudflared', 'tunnel', '--no-autoupdate', 'run', '--url', TARGET, 'bleo'])

    def test_neither_name_nor_quick_is_refused(self):
        with self.assertRaisesRegex(TunnelError, 'named tunnel required for streaming'):
            tunnel.cloudflare_command(API_PORT)

    def test_both_name_and_quick_is_refused(self):
        with self.assertRaises(TunnelError):
            tunnel.cloudflare_command(API_PORT, name='bleo', quick=True)

    def test_bad_port_is_refused(self):
        with self.assertRaises(TunnelError):
            tunnel.cloudflare_command(0, quick=True)

    def test_tunnel_error_is_bleo_error(self):
        self.assertTrue(issubclass(TunnelError, BleoError))


class ParseUrlTests(unittest.TestCase):
    def test_finds_quick_url_in_banner(self):
        line = '2026-11-06T10:00:00Z INF |  https://abc-def12.trycloudflare.com                                  |'
        self.assertEqual(tunnel.parse_quick_url(line), 'https://abc-def12.trycloudflare.com')

    def test_finds_url_followed_by_slash(self):
        self.assertEqual(tunnel.parse_quick_url('see https://xy.trycloudflare.com/ now'),
                         'https://xy.trycloudflare.com')

    def test_rejects_lines_without_quick_url(self):
        for line in ['INF Requesting new quick Tunnel on trycloudflare.com...',
                     'INF starting tunnel https://example.com',
                     'INF http://abc.trycloudflare.com is plain http',
                     'INF https://trycloudflare.com has no subdomain',
                     'INF https://api.trycloudflare.com.evil.example is not ours',
                     '']:
            self.assertIsNone(tunnel.parse_quick_url(line), line)


class MissingBinaryTests(unittest.TestCase):
    def test_missing_cloudflared_names_binary_and_hint(self):
        with patch('bleo.tunnel.shutil.which', return_value=None):
            with self.assertRaises(TunnelError) as caught:
                Tunnel('cloudflare', API_PORT, quick=True, runner=FakeRunner()).start()
        self.assertIn('cloudflared', str(caught.exception))
        self.assertIn('brew install cloudflared', str(caught.exception))

    def test_missing_tailscale_names_binary_and_hint(self):
        with patch('bleo.tunnel.shutil.which', return_value=None):
            with self.assertRaises(TunnelError) as caught:
                Tunnel('tailscale', API_PORT, runner=FakeRunner()).start()
        self.assertIn('brew install tailscale', str(caught.exception))


class CloudflareTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.which = patch('bleo.tunnel.shutil.which', return_value='/opt/bin/cloudflared')
        self.which.start()

    def tearDown(self):
        self.which.stop()

    def log(self, *args):
        self.logs.append(' '.join(str(arg) for arg in args))

    def test_quick_start_returns_url_and_stop_terminates_child(self):
        proc = FakeProc(stderr=['INF Requesting new quick Tunnel\n',
                                '|  https://abc-def12.trycloudflare.com  |\n'])
        runner = FakeRunner(proc=proc)
        with Tunnel('cloudflare', API_PORT, quick=True, runner=runner, log=self.log) as active:
            self.assertEqual(active.url, 'https://abc-def12.trycloudflare.com')
        self.assertEqual(runner.popen_calls, [tunnel.cloudflare_command(API_PORT, quick=True)])
        self.assertTrue(proc.terminated)
        self.assertTrue(any('quick tunnels do not support streaming' in line for line in self.logs))

    def test_named_start_has_no_url_and_runs_named_command(self):
        proc = FakeProc(stderr=['INF Starting tunnel\n',
                                'INF Registered tunnel connection connIndex=0\n'])
        runner = FakeRunner(proc=proc)
        with Tunnel('cloudflare', API_PORT, name='bleo', runner=runner, log=self.log) as active:
            self.assertIsNone(active.url)
        self.assertEqual(runner.popen_calls, [tunnel.cloudflare_command(API_PORT, name='bleo')])
        self.assertTrue(proc.terminated)

    def test_start_timeout_raises_and_stops_child(self):
        release = threading.Event()

        def blocked():
            release.wait(5)
            yield from ()

        proc = FakeProc(stderr=blocked())
        try:
            with self.assertRaisesRegex(TunnelError, 'did not report a tunnel within'):
                Tunnel('cloudflare', API_PORT, quick=True, runner=FakeRunner(proc=proc),
                       timeout=0.05, log=self.log).start()
            self.assertTrue(proc.terminated)
        finally:
            release.set()

    def test_child_exiting_before_url_raises(self):
        proc = FakeProc(stderr=['ERR failed to connect\n'])
        with self.assertRaisesRegex(TunnelError, 'exited before reporting a tunnel'):
            Tunnel('cloudflare', API_PORT, quick=True, runner=FakeRunner(proc=proc), log=self.log).start()

    def test_stop_kills_child_that_ignores_sigterm(self):
        proc = FakeProc(stderr=['|  https://ab-cd.trycloudflare.com  |\n'], hang_on_term=True)
        active = Tunnel('cloudflare', API_PORT, quick=True, runner=FakeRunner(proc=proc), log=self.log)
        active.start()
        active.stop()
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)

    def test_cloudflare_stop_is_idempotent(self):
        proc = FakeProc(stderr=['|  https://ab-cd.trycloudflare.com  |\n'])
        active = Tunnel('cloudflare', API_PORT, quick=True, runner=FakeRunner(proc=proc), log=self.log)
        active.start()
        active.stop()
        active.stop()
        self.assertEqual(proc.wait_calls, [tunnel.STOP_TIMEOUT])

    def test_logs_never_contain_credentials(self):
        proc = FakeProc(stderr=['|  https://ab-cd.trycloudflare.com  |\n'])
        with Tunnel('cloudflare', API_PORT, quick=True, runner=FakeRunner(proc=proc), log=self.log):
            pass
        text = '\n'.join(self.logs)
        for word in SECRET_LIKE:
            self.assertNotIn(word, text)


class TailscaleHelperTests(unittest.TestCase):
    def test_empty_and_empty_object_status_are_empty(self):
        self.assertEqual(tunnel.tailscale_status(FakeRunner({STATUS: [completed(STATUS, '')]})), {})
        self.assertEqual(tunnel.tailscale_status(FakeRunner({STATUS: [completed(STATUS, '{}')]})), {})

    def test_bad_json_and_failed_status_are_errors(self):
        with self.assertRaises(TunnelError):
            tunnel.tailscale_status(FakeRunner({STATUS: [completed(STATUS, 'not json')]}))
        with self.assertRaises(TunnelError):
            tunnel.tailscale_status(FakeRunner({STATUS: [completed(STATUS, '', returncode=1)]}))

    def test_occupied_port_detected_in_tcp_form(self):
        self.assertTrue(tunnel.https_port_in_use({'TCP': {'8443': {'HTTPS': True}}}, 8443))

    def test_occupied_port_detected_in_web_key_form(self):
        status = {'Web': {'other.tail.ts.net:8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:9999'}}}}}
        self.assertTrue(tunnel.https_port_in_use(status, 8443))
        self.assertFalse(tunnel.https_port_in_use(status, 443))

    def test_free_port_is_not_in_use(self):
        self.assertFalse(tunnel.https_port_in_use({}, 8443))

    def test_owned_only_when_root_proxies_exactly_to_target(self):
        self.assertTrue(tunnel.tailscale_owned(OWNED, 8443, API_PORT))
        self.assertFalse(tunnel.tailscale_owned(OWNED, 8443, 9999))
        self.assertFalse(tunnel.tailscale_owned({}, 8443, API_PORT))
        extra = {'Web': {'box.tail.ts.net:8443': {'Handlers': {
            '/': {'Proxy': TARGET}, '/admin': {'Proxy': 'http://127.0.0.1:1'}}}}}
        self.assertFalse(tunnel.tailscale_owned(extra, 8443, API_PORT))
        prefix = {'Web': {'box.tail.ts.net:8443': {'Handlers': {'/': {'Proxy': TARGET + '/x'}}}}}
        self.assertFalse(tunnel.tailscale_owned(prefix, 8443, API_PORT))


class TailscaleTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.which = patch('bleo.tunnel.shutil.which', return_value='/opt/bin/tailscale')
        self.which.start()

    def tearDown(self):
        self.which.stop()

    def log(self, *args):
        self.logs.append(' '.join(str(arg) for arg in args))

    def test_occupied_tcp_port_is_refused_before_install(self):
        runner = FakeRunner({STATUS: [completed(STATUS, json.dumps({'TCP': {'8443': {}}}))]})
        with self.assertRaisesRegex(TunnelError, 'already has a Serve mapping'):
            Tunnel('tailscale', API_PORT, runner=runner, log=self.log).start()
        self.assertFalse(runner.ran(INSTALL))

    def test_occupied_web_port_is_refused_before_install(self):
        other = {'Web': {'other.tail.ts.net:8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:1'}}}}}
        runner = FakeRunner({STATUS: [completed(STATUS, json.dumps(other))]})
        with self.assertRaises(TunnelError):
            Tunnel('tailscale', API_PORT, runner=runner, log=self.log).start()
        self.assertFalse(runner.ran(INSTALL))

    def test_start_installs_and_returns_public_url(self):
        runner = FakeRunner({STATUS: [completed(STATUS, '{}')],
                             INSTALL: [completed(INSTALL)],
                             TS_DNS: [completed(TS_DNS, DNS_STATUS)]})
        active = Tunnel('tailscale', API_PORT, runner=runner, log=self.log)
        self.assertEqual(active.start(), 'https://box.tail.ts.net:8443')
        self.assertTrue(runner.ran(INSTALL))

    def test_custom_https_port_is_used_everywhere(self):
        install = ('tailscale', 'serve', '--bg', '--https=9443', TARGET)
        off = ('tailscale', 'serve', '--https=9443', 'off')
        owned = {'Web': {'box.tail.ts.net:9443': {'Handlers': {'/': {'Proxy': TARGET}}}}}
        runner = FakeRunner({STATUS: [completed(STATUS, '{}'), completed(STATUS, json.dumps(owned))],
                             install: [completed(install)],
                             TS_DNS: [completed(TS_DNS, DNS_STATUS)],
                             off: [completed(off)]})
        with Tunnel('tailscale', API_PORT, https_port=9443, runner=runner, log=self.log) as active:
            self.assertEqual(active.url, 'https://box.tail.ts.net:9443')
        self.assertTrue(runner.ran(off))

    def test_missing_dns_name_rolls_back_owned_mapping(self):
        runner = FakeRunner({STATUS: [completed(STATUS, '{}'), completed(STATUS, json.dumps(OWNED))],
                             INSTALL: [completed(INSTALL)],
                             TS_DNS: [completed(TS_DNS, json.dumps({'Self': {}}))],
                             OFF: [completed(OFF)]})
        with self.assertRaisesRegex(TunnelError, 'no DNS name'):
            Tunnel('tailscale', API_PORT, runner=runner, log=self.log).start()
        self.assertTrue(runner.ran(OFF))

    def test_stop_removes_mapping_while_owned(self):
        runner = FakeRunner({STATUS: [completed(STATUS, '{}'), completed(STATUS, json.dumps(OWNED))],
                             INSTALL: [completed(INSTALL)],
                             TS_DNS: [completed(TS_DNS, DNS_STATUS)],
                             OFF: [completed(OFF)]})
        active = Tunnel('tailscale', API_PORT, runner=runner, log=self.log)
        active.start()
        active.stop()
        self.assertTrue(runner.ran(OFF))

    def test_stop_leaves_mapping_that_now_proxies_elsewhere(self):
        elsewhere = {'Web': {'box.tail.ts.net:8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:1'}}}}}
        runner = FakeRunner({STATUS: [completed(STATUS, '{}'), completed(STATUS, json.dumps(elsewhere))],
                             INSTALL: [completed(INSTALL)],
                             TS_DNS: [completed(TS_DNS, DNS_STATUS)],
                             OFF: [completed(OFF)]})
        active = Tunnel('tailscale', API_PORT, runner=runner, log=self.log)
        active.start()
        active.stop()
        self.assertFalse(runner.ran(OFF))
        self.assertTrue(any('leaving it unchanged' in line for line in self.logs))

    def test_stop_leaves_mapping_that_is_gone(self):
        runner = FakeRunner({STATUS: [completed(STATUS, '{}'), completed(STATUS, '{}')],
                             INSTALL: [completed(INSTALL)],
                             TS_DNS: [completed(TS_DNS, DNS_STATUS)],
                             OFF: [completed(OFF)]})
        active = Tunnel('tailscale', API_PORT, runner=runner, log=self.log)
        active.start()
        active.stop()
        self.assertFalse(runner.ran(OFF))

    def test_tailscale_stop_is_idempotent(self):
        runner = FakeRunner({STATUS: [completed(STATUS, '{}'), completed(STATUS, json.dumps(OWNED))],
                             INSTALL: [completed(INSTALL)],
                             TS_DNS: [completed(TS_DNS, DNS_STATUS)],
                             OFF: [completed(OFF)]})
        active = Tunnel('tailscale', API_PORT, runner=runner, log=self.log)
        active.start()
        active.stop()
        active.stop()
        self.assertEqual(runner.run_calls.count(list(OFF)), 1)

    def test_logs_never_contain_credentials(self):
        runner = FakeRunner({STATUS: [completed(STATUS, '{}'), completed(STATUS, json.dumps(OWNED))],
                             INSTALL: [completed(INSTALL)],
                             TS_DNS: [completed(TS_DNS, DNS_STATUS)],
                             OFF: [completed(OFF)]})
        with Tunnel('tailscale', API_PORT, runner=runner, log=self.log):
            pass
        text = '\n'.join(self.logs)
        self.assertIn('https://box.tail.ts.net:8443', text)
        for word in SECRET_LIKE:
            self.assertNotIn(word, text)


if __name__ == '__main__':
    unittest.main()
