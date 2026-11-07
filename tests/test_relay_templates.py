import base64
import importlib.resources
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bleo import relay_templates

TEMPLATE_ROOT = pathlib.Path(str(importlib.resources.files('bleo.relay_templates')))
CORE = 'relay-core.js'
WRAPPERS = ('cloudflare/worker.js', 'deno/main.ts', 'vercel/api/relay.js')
BRAVE_HOSTS = {'ai-chat.bsg.brave.com', 'ai-chat-premium.bsg.brave.com'}
SECRET = 'unit-test-secret'

EXPECTED_FILES = {
    'cloudflare': {'worker.js', 'wrangler.toml', 'README.md'},
    'deno': {'main.ts', 'README.md'},
    'vercel': {'api/relay.js', 'vercel.json', 'package.json', 'README.md'},
}


def _template_files():
    for path in TEMPLATE_ROOT.rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            yield path


class WriteTemplateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = pathlib.Path(tmp.name)

    def test_each_platform_writes_expected_files(self):
        for platform in relay_templates.PLATFORMS:
            with self.subTest(platform=platform):
                out = self.out / platform
                written = relay_templates.write_template(platform, out)
                rel = {str(p.relative_to(out)) for p in written}
                self.assertEqual(rel, EXPECTED_FILES[platform])
                for path in written:
                    self.assertTrue(path.is_file())
                    self.assertGreater(path.stat().st_size, 0)

    def test_written_bytes_match_package_source(self):
        out = self.out / 'deno'
        relay_templates.write_template('deno', out)
        self.assertEqual((out / 'main.ts').read_bytes(), (TEMPLATE_ROOT / 'deno' / 'main.ts').read_bytes())

    def test_refuses_to_overwrite(self):
        relay_templates.write_template('cloudflare', self.out)
        with self.assertRaises(FileExistsError):
            relay_templates.write_template('cloudflare', self.out)

    def test_refusal_writes_nothing(self):
        (self.out / 'wrangler.toml').write_text('keep me')
        with self.assertRaises(FileExistsError):
            relay_templates.write_template('cloudflare', self.out)
        self.assertFalse((self.out / 'worker.js').exists())
        self.assertEqual((self.out / 'wrangler.toml').read_text(), 'keep me')

    def test_unknown_platform_raises(self):
        with self.assertRaises(ValueError):
            relay_templates.write_template('netlify', self.out)

    def test_new_secret_is_64_hex_and_fresh(self):
        first = relay_templates.new_secret()
        self.assertRegex(first, r'^[0-9a-f]{64}$')
        self.assertNotEqual(first, relay_templates.new_secret())


class TemplateContentTests(unittest.TestCase):
    def test_no_secret_shaped_string_in_any_template_file(self):
        for path in _template_files():
            with self.subTest(path=path.name):
                self.assertNotRegex(path.read_text(encoding='utf-8'), r'[0-9a-fA-F]{64}')

    def test_js_ts_allowlist_is_exactly_brave_hosts(self):
        for rel in (CORE,) + WRAPPERS:
            with self.subTest(file=rel):
                text = (TEMPLATE_ROOT / rel).read_text(encoding='utf-8')
                match = re.search(r'ALLOWED_TARGETS\s*=\s*\[([^\]]*)\]', text)
                self.assertIsNotNone(match)
                hosts = set(re.findall(r'"https?://([^"/]+)"', match.group(1)))
                self.assertEqual(hosts, BRAVE_HOSTS)

    def test_wrappers_inline_core_verbatim(self):
        core = (TEMPLATE_ROOT / CORE).read_text(encoding='utf-8')
        for rel in WRAPPERS:
            with self.subTest(file=rel):
                self.assertIn(core, (TEMPLATE_ROOT / rel).read_text(encoding='utf-8'))

    def test_core_is_plain_script_and_reads_secret_from_env(self):
        core = (TEMPLATE_ROOT / CORE).read_text(encoding='utf-8')
        self.assertNotRegex(core, r'(?m)^(import|export) ')
        for rel in WRAPPERS:
            with self.subTest(file=rel):
                self.assertIn('BLEO_RELAY_SECRET', (TEMPLATE_ROOT / rel).read_text(encoding='utf-8'))


@unittest.skipUnless(shutil.which('node'), 'node not on PATH')
class NodeSyntaxTests(unittest.TestCase):
    def test_node_check_passes_for_core_and_js_wrappers(self):
        # Copied to .mjs so the check is ESM-explicit and independent of Node's syntax detection.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for rel in (CORE,) + WRAPPERS[:1] + WRAPPERS[2:]:
            with self.subTest(file=rel):
                copy = pathlib.Path(tmp.name) / (rel.replace('/', '_') + '.mjs')
                copy.write_bytes((TEMPLATE_ROOT / rel).read_bytes())
                proc = subprocess.run(['node', '--check', str(copy)], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)


@unittest.skipUnless(shutil.which('deno'), 'deno not on PATH')
class DenoCheckTests(unittest.TestCase):
    def test_deno_check_passes_for_main(self):
        env = dict(os.environ, DENO_NO_UPDATE_CHECK='1')
        proc = subprocess.run(['deno', 'check', str(TEMPLATE_ROOT / 'deno' / 'main.ts')],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)


# Appended to a copy of relay-core.js: reads one JSON request from stdin, runs handleRelay,
# prints the status, headers and body as JSON. Only the test harness uses this.
DRIVER = r'''
const __input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const __init = { method: __input.method, headers: __input.headers };
if (__input.body !== null) { __init.body = Buffer.from(__input.body, "base64"); __init.duplex = "half"; }
handleRelay(new Request(__input.url, __init), __input.secret, __input.options).then(async (res) => {
  const headers = {};
  res.headers.forEach((value, name) => { headers[name] = value; });
  const bytes = Buffer.from(await res.arrayBuffer());
  process.stdout.write(JSON.stringify({ status: res.status, headers, body: bytes.toString("base64") }));
});
'''


class _Upstream(BaseHTTPRequestHandler):
    """Stand-in for Brave Leo: records what arrives, replies with fixed UTF-8 text."""

    def log_message(self, *args):
        pass

    def _read_body(self):
        if 'chunked' in self.headers.get('transfer-encoding', '').lower():
            body = b''
            while True:
                size = int(self.rfile.readline().split(b';')[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline() not in (b'\r\n', b'\n', b''):
                        pass
                    return body
                body += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get('content-length', 0)))

    def _handle(self):
        body = self._read_body()
        self.server.received.append({
            'method': self.command,
            'path': self.path,
            'headers': [(name.lower(), value) for name, value in self.headers.items()],
            'body': body,
        })
        reply = 'ok: héllo ✓'.encode('utf-8')
        self.send_response(200)
        self.send_header('content-type', 'text/plain; charset=utf-8')
        self.send_header('content-length', str(len(reply)))
        self.send_header('x-upstream-marker', 'standin')
        self.end_headers()
        self.wfile.write(reply)

    do_GET = _handle
    do_POST = _handle


@unittest.skipUnless(shutil.which('node'), 'node not on PATH')
class RelayBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        core = (TEMPLATE_ROOT / CORE).read_text(encoding='utf-8')
        cls.driver = pathlib.Path(cls.tmp.name) / 'relay-under-test.cjs'
        cls.driver.write_text(core + '\n' + DRIVER, encoding='utf-8')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), _Upstream)
        cls.server.received = []
        cls.upstream = f'http://127.0.0.1:{cls.server.server_address[1]}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.server.received.clear()
        self.allow_stand_in = {'allowlist': [self.upstream], 'allowPrivateTargets': True}

    def relay(self, method='POST', headers=None, body=None, secret=SECRET, options=None):
        request_headers = {
            'x-relay-target': self.upstream,
            'x-relay-path': '/v1/chat?stream=1',
            'x-relay-auth': SECRET,
        }
        request_headers.update(headers or {})
        payload = {
            'url': 'http://relay.test/',
            'method': method,
            'headers': request_headers,
            'body': None if body is None else base64.b64encode(body).decode('ascii'),
            'secret': secret,
            'options': options,
        }
        proc = subprocess.run(['node', str(self.driver)], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        return out['status'], out['headers'], base64.b64decode(out['body'])

    def test_forwards_headers_and_utf8_body_unchanged(self):
        upstream_headers = {
            'authorization': 'Signature keyId="k1",algorithm="hs2019",headers="digest",signature="c2ln"',
            'digest': 'SHA-256=YWJj',
            'user-agent': 'bleo/0.2.0 (test)',
            'cookie': '__Secure-sku#brave-leo-premium=cred-value',
            'content-type': 'application/json',
        }
        body = json.dumps({'prompt': 'héllo — 日本語 ✓'}, ensure_ascii=False).encode('utf-8')
        status, headers, reply = self.relay(headers=upstream_headers, body=body,
                                            options=self.allow_stand_in)
        self.assertEqual(status, 200)
        self.assertEqual(reply, 'ok: héllo ✓'.encode('utf-8'))
        self.assertEqual(headers.get('x-upstream-marker'), 'standin')
        self.assertEqual(len(self.server.received), 1)
        got = self.server.received[0]
        self.assertEqual((got['method'], got['path']), ('POST', '/v1/chat?stream=1'))
        self.assertEqual(got['body'], body)
        received = {}
        for name, value in got['headers']:
            received.setdefault(name, value)
        for name, value in upstream_headers.items():
            self.assertEqual(received.get(name), value, name)
        for name in received:
            self.assertFalse(name.startswith('x-relay-'), name)

    def test_wrong_secret_is_401_with_exact_body(self):
        status, _, body = self.relay(headers={'x-relay-auth': 'wrong'}, body=b'{}',
                                     options=self.allow_stand_in)
        self.assertEqual(status, 401)
        self.assertEqual(body, b'{"error":"unauthorized"}')
        self.assertEqual(self.server.received, [])

    def test_missing_secret_is_500_for_every_request(self):
        for secret in (None, ''):
            with self.subTest(secret=secret):
                status, _, body = self.relay(method='GET', secret=secret,
                                             options=self.allow_stand_in)
                self.assertEqual(status, 500)
                self.assertEqual(body, b'{"error":"relay secret not configured"}')
        self.assertEqual(self.server.received, [])

    def test_disallowed_target_is_rejected(self):
        status, _, body = self.relay(method='GET', headers={'x-relay-target': 'https://evil.example.com'})
        self.assertEqual(status, 403)
        self.assertEqual(body, b'{"error":"Forbidden target"}')

    def test_loopback_target_rejected_by_default_guard(self):
        status, _, body = self.relay(method='GET', options=None)
        self.assertEqual(status, 403)
        self.assertEqual(body, b'{"error":"forbidden x-relay-target (private/loopback host)"}')
        self.assertEqual(self.server.received, [])

    def test_path_cannot_leave_target_host(self):
        status, _, body = self.relay(method='GET', headers={'x-relay-path': '//evil.example.com/x'},
                                     options=self.allow_stand_in)
        self.assertEqual(status, 403)
        self.assertEqual(body, b'{"error":"forbidden x-relay-path (host mismatch)"}')
        self.assertEqual(self.server.received, [])

    def test_method_not_allowed(self):
        status, _, body = self.relay(method='DELETE', options=self.allow_stand_in)
        self.assertEqual(status, 405)
        self.assertEqual(body, b'Method Not Allowed')
        self.assertEqual(self.server.received, [])

    def test_oversized_declared_body_is_413(self):
        status, _, body = self.relay(method='POST', headers={'content-length': str(32 * 1024 * 1024 + 1)},
                                     body=b'x', options=self.allow_stand_in)
        self.assertEqual(status, 413)
        self.assertEqual(body, b'Payload Too Large')
        self.assertEqual(self.server.received, [])


if __name__ == '__main__':
    unittest.main()
