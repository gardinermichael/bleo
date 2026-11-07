"""bleo relay deploy: each platform's REST API is faked by a local HTTP server."""

from email import message_from_bytes
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import patch

from bleo import cli, relay_deploy
from bleo.relay_state import RelayEntry, RelayPool, validate_relay_url
from test_cli_relay import CliRelayCase
from test_client_relay import stream_reply

TOKEN = 'tok-PLATFORMTOKEN-0123456789abcdef'
HEX64 = re.compile(r'[0-9a-f]{64}')
HOSTS = ('ai-chat.bsg.brave.com', 'ai-chat-premium.bsg.brave.com')


class Request:
    def __init__(self, method, path, headers, body):
        self.method, self.path, self.headers, self.body = method, path, headers, body

    @property
    def json(self):
        return json.loads(self.body)

    def parts(self):
        """Multipart form parts as {name: text}."""
        message = message_from_bytes(b'Content-Type: ' + self.headers['Content-Type'].encode() + b'\r\n\r\n' + self.body)
        return {part.get_param('name', header='content-disposition'): part.get_payload(decode=True).decode()
                for part in message.get_payload()}


class Platform:
    """A recording fake REST API. route(request) returns (status, body, headers)."""

    def __init__(self, route):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def handle_any(self):
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                request = Request(self.command, self.path, self.headers, body)
                owner.requests.append(request)
                outcome = route(request)
                status, payload = outcome[0], outcome[1]
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                for name, value in (outcome[2] if len(outcome) > 2 else {}).items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any

        class Quiet(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self.httpd = Quiet(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_port}'
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def calls(self):
        return [f'{r.method} {r.path}' for r in self.requests]

    def raw(self):
        return b''.join(r.body for r in self.requests).decode()


def cloudflare_routes(accounts=None, subdomain='me', fail=None):
    accounts = [{'id': 'acc1', 'name': 'Mine'}] if accounts is None else accounts

    def route(request):
        key = f'{request.method} {request.path}'
        if fail and key in fail:
            return fail[key]
        if key == 'GET /accounts':
            return 200, {'success': True, 'result': accounts}
        if key == 'GET /accounts/acc1/workers/subdomain':
            return 200, {'result': {'subdomain': subdomain} if subdomain else {}}
        if request.method in ('PUT', 'POST', 'DELETE') and request.path.startswith('/accounts/acc1/workers/scripts/'):
            return 200, {'success': True}
        return 404, {'errors': [{'message': 'no such route'}]}
    return route


def vercel_routes(fail=None, polls=('QUEUED', 'READY'), url='relay-abc.vercel.app'):
    states = list(polls)

    def route(request):
        key = f'{request.method} {request.path}'
        if fail and key in fail:
            return fail[key]
        if key == 'POST /v11/projects':
            return 200, {'id': 'prj_1', 'name': request.json['name']}
        if key == 'POST /v10/projects/prj_1/env':
            return 201, {'created': {}}
        if key == 'POST /v13/deployments':
            return 200, {'id': 'dpl_1', 'projectId': 'prj_1'}
        if key == 'PATCH /v9/projects/prj_1':
            return 200, {}
        if key == 'GET /v13/deployments/dpl_1':
            state = states.pop(0) if len(states) > 1 else states[0]
            return 200, {'readyState': state, 'url': url}
        if request.method == 'DELETE':
            return 200, {}
        return 404, {'error': {'message': 'no such route'}}
    return route


def deno_routes(fail=None, revision=None, domains=None):
    revision = revision or {'status': 'succeeded', 'timelines': [{'name': 'Production', 'hostnames': ['relay-x.deno.net']}]}

    def route(request):
        key = f'{request.method} {request.path}'
        if fail and key in fail:
            return fail[key]
        if key == 'POST /apps':
            return 200, {'id': 'app1', 'slug': request.json['slug']}
        if key == 'PATCH /apps/app1':
            return 200, {}
        if key == 'POST /apps/app1/deploy':
            return 200, {'id': 'rev1'}
        if key == 'GET /revisions/rev1':
            return 200, revision
        if key == 'GET /domains':
            return 200, domains or []
        if request.method == 'DELETE':
            return 200, {}
        return 404, {'error': {'message': 'no such route'}}
    return route


def fake_probe_ok(args, ui):
    print('probe fake\n  result: PASS')
    return 0


class DeployCase(CliRelayCase):
    def setUp(self):
        super().setUp()
        self.platforms = []
        for name in ('CLOUDFLARE_API_TOKEN', 'DENO_DEPLOY_TOKEN', 'VERCEL_TOKEN', 'CLOUDFLARE_ACCOUNT_ID'):
            os.environ.pop(name, None)
        os.environ['CLOUDFLARE_API_TOKEN'] = os.environ['DENO_DEPLOY_TOKEN'] = os.environ['VERCEL_TOKEN'] = TOKEN
        self.patch(patch('bleo.cli._relay_probe', fake_probe_ok))
        self.patch(patch.dict(relay_deploy.POLL_INTERVAL, {'deno': 0.001, 'vercel': 0.001}))
        self.patch(patch.object(relay_deploy, 'POLL_TIMEOUT', 2.0))

    def patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake(self, platform, route):
        server = Platform(route)
        self.addCleanup(server.stop)
        self.patch(patch.dict(relay_deploy.API_BASES, {platform: server.url}))
        self.platforms.append(server)
        return server

    def deploy(self, platform, *extra):
        return self.run_cli('relay', 'deploy', platform, '--name', 'relay-x', *extra)

    def sent_secrets(self):
        return {s for server in self.platforms for s in HEX64.findall(server.raw())}

    def pool_secret(self):
        return RelayPool.load().entries[0].auth

    def assertClean(self, *secrets):
        self.assertNotIn(TOKEN, self.everything)
        for secret in secrets or self.sent_secrets():
            self.assertNotIn(secret, self.everything)

    def assertAuthorized(self, server):
        self.assertTrue(server.requests)
        for request in server.requests:
            self.assertEqual(request.headers['Authorization'], f'Bearer {TOKEN}')

    def assertWrapper(self, script):
        for host in HOSTS:
            self.assertIn(host, script)
        self.assertIn('BLEO_RELAY_SECRET', script)


class CloudflareDeployTests(DeployCase):
    def test_deploys_in_order_with_the_secret_only_as_a_binding(self):
        server = self.fake('cloudflare', cloudflare_routes())
        self.assertEqual(self.deploy('cloudflare'), 0, self.everything)
        base = '/accounts/acc1/workers'
        self.assertEqual(server.calls(), ['GET /accounts', f'PUT {base}/scripts/relay-x',
                                          f'POST {base}/scripts/relay-x/subdomain', f'GET {base}/subdomain'])
        self.assertAuthorized(server)
        parts = server.requests[1].parts()
        metadata = json.loads(parts['metadata'])
        secret = self.pool_secret()
        self.assertEqual(metadata['bindings'], [{'type': 'secret_text', 'name': 'BLEO_RELAY_SECRET', 'text': secret}])
        self.assertEqual(metadata['main_module'], 'index.js')
        self.assertWrapper(parts['index.js'])
        self.assertEqual(parts['index.js'], (Path(cli.__file__).parent / 'relay_templates/cloudflare/worker.js').read_text())
        self.assertNotIn(secret, parts['index.js'])
        self.assertEqual(server.raw().count(secret), 1)
        entry = RelayPool.load().entries[0]
        self.assertEqual((entry.url, entry.label), ('https://relay-x.me.workers.dev', 'cloudflare'))
        self.assertEqual(self.sent_secrets(), {secret})
        for line in ('platform: cloudflare', 'name: relay-x', 'url: https://relay-x.me.workers.dev',
                     'label: cloudflare', 'secret: set on platform'):
            self.assertIn(line, self.out)
        self.assertClean()

    def test_one_account_needs_no_account_id(self):
        self.fake('cloudflare', cloudflare_routes(accounts=[{'id': 'acc1', 'name': 'Only'}]))
        self.assertEqual(self.deploy('cloudflare', '--label', 'cf'), 0)
        self.assertEqual(RelayPool.load().entries[0].label, 'cf')

    def test_several_accounts_without_an_account_id_fails_listing_names(self):
        server = self.fake('cloudflare', cloudflare_routes(
            accounts=[{'id': 'acc1', 'name': 'Alpha Org'}, {'id': 'acc2', 'name': 'Beta Org'}]))
        self.assertEqual(self.deploy('cloudflare'), 1)
        self.assertIn('Alpha Org', self.err)
        self.assertIn('Beta Org', self.err)
        self.assertIn('CLOUDFLARE_ACCOUNT_ID', self.err)
        self.assertNotIn('acc2', self.err)
        self.assertEqual(server.calls(), ['GET /accounts'])
        self.assertEqual(RelayPool.load().entries, ())
        self.assertClean()

    def test_account_id_from_the_environment_picks_the_account(self):
        accounts = [{'id': 'acc9', 'name': 'Other'}, {'id': 'acc1', 'name': 'Mine'}]
        server = self.fake('cloudflare', cloudflare_routes(accounts=accounts))
        os.environ['CLOUDFLARE_ACCOUNT_ID'] = 'acc1'
        self.addCleanup(os.environ.pop, 'CLOUDFLARE_ACCOUNT_ID', None)
        self.assertEqual(self.deploy('cloudflare'), 0)
        self.assertIn('PUT /accounts/acc1/workers/scripts/relay-x', server.calls())
        os.environ['CLOUDFLARE_ACCOUNT_ID'] = 'nope'
        self.assertEqual(self.deploy('cloudflare', '--label', 'again'), 1)
        self.assertIn('does not match', self.err)

    def test_missing_workers_subdomain_removes_the_script(self):
        server = self.fake('cloudflare', cloudflare_routes(subdomain=None))
        self.assertEqual(self.deploy('cloudflare'), 1)
        self.assertIn('workers.dev subdomain is unavailable', self.err)
        self.assertEqual(server.calls()[-1], 'DELETE /accounts/acc1/workers/scripts/relay-x')
        self.assertEqual(RelayPool.load().entries, ())
        self.assertClean()

    def test_api_errors_are_clear_and_leave_the_pool_alone(self):
        upload = 'PUT /accounts/acc1/workers/scripts/relay-x'
        cases = [({'GET /accounts': (401, {'errors': [{'message': 'Invalid API Token'}]})}, 'authentication failed'),
                 ({upload: (403, {'errors': [{'message': 'no Workers permission'}]})}, 'no Workers permission'),
                 ({upload: (500, {'errors': [{'message': 'boom'}]})}, '(HTTP 500): boom'),
                 ({upload: (403, {'errors': [{'message': 'quota exceeded'}]})}, 'plan or usage limit')]
        for fail, expected in cases:
            with self.subTest(expected=expected):
                server = self.fake('cloudflare', cloudflare_routes(fail=fail))
                self.assertEqual(self.deploy('cloudflare'), 1)
                self.assertIn(expected, self.err)
                self.assertEqual(RelayPool.load().entries, ())
                self.assertNotIn('Traceback', self.err)
                server.requests.clear()
        self.assertClean()

    def test_an_error_that_echoes_the_token_and_secret_is_scrubbed(self):
        def echo(request):
            leaked = ' '.join(HEX64.findall(request.body.decode()))
            return 500, {'errors': [{'message': f'bad {TOKEN} {leaked}'}]}
        server = self.fake('cloudflare', lambda request: cloudflare_routes()(request) if request.method == 'GET' else echo(request))
        self.assertEqual(self.deploy('cloudflare'), 1)
        self.assertIn('[hidden]', self.err)
        self.assertTrue(self.sent_secrets())
        self.assertClean()

    def test_a_redirect_is_not_followed(self):
        target = self.fake('cloudflare', lambda request: (200, {'result': []}))
        server = self.fake('cloudflare', lambda request: (302, b'', {'Location': target.url + '/accounts'}))
        self.assertEqual(self.deploy('cloudflare'), 1)
        self.assertIn('redirect', self.err)
        self.assertEqual(target.requests, [])
        self.assertEqual(len(server.requests), 1)


class VercelDeployTests(DeployCase):
    def test_deploys_in_order_with_the_secret_only_as_an_env_var(self):
        server = self.fake('vercel', vercel_routes())
        self.assertEqual(self.deploy('vercel'), 0, self.everything)
        self.assertEqual(server.calls(), ['POST /v11/projects', 'POST /v10/projects/prj_1/env', 'POST /v13/deployments',
                                          'PATCH /v9/projects/prj_1', 'GET /v13/deployments/dpl_1',
                                          'GET /v13/deployments/dpl_1'])
        self.assertAuthorized(server)
        secret = self.pool_secret()
        env = server.requests[1].json
        self.assertEqual((env['key'], env['value'], env['target']), ('BLEO_RELAY_SECRET', secret, ['production']))
        files = {f['file']: f['data'] for f in server.requests[2].json['files']}
        self.assertEqual(set(files), {'api/relay.js', 'package.json', 'vercel.json'})
        self.assertWrapper(files['api/relay.js'])
        for text in files.values():
            self.assertNotIn(secret, text)
        self.assertEqual(server.raw().count(secret), 1)
        self.assertEqual(server.requests[2].json['project'], 'prj_1')
        entry = RelayPool.load().entries[0]
        self.assertEqual((entry.url, entry.label, entry.auth), ('https://relay-abc.vercel.app', 'vercel', secret))
        self.assertIn('secret: set on platform', self.out)
        self.assertClean()

    def test_api_errors_clean_up_and_leave_the_pool_alone(self):
        cases = [({'POST /v11/projects': (401, {'error': {'message': 'Not authorized'}})}, 'authentication failed', 0),
                 ({'POST /v11/projects': (409, {'error': {'message': 'exists'}})}, '(HTTP 409): exists', 0),
                 ({'POST /v10/projects/prj_1/env': (403, {'error': {'message': 'no env access'}})}, 'no env access', 1),
                 ({'POST /v13/deployments': (500, {'error': {'message': 'boom'}})}, '(HTTP 500): boom', 1)]
        for fail, expected, deletes in cases:
            with self.subTest(expected=expected):
                server = self.fake('vercel', vercel_routes(fail=fail))
                self.assertEqual(self.deploy('vercel'), 1)
                self.assertIn(expected, self.err)
                self.assertEqual(RelayPool.load().entries, ())
                self.assertEqual(server.calls().count('DELETE /v9/projects/prj_1'), deletes)
                if 'env' in expected:
                    self.assertNotIn('POST /v13/deployments', server.calls())  # nothing uploaded without the secret
        self.assertClean()

    def test_a_failed_build_removes_the_deployment_and_project(self):
        server = self.fake('vercel', vercel_routes(polls=('ERROR',)))
        self.assertEqual(self.deploy('vercel'), 1)
        self.assertIn('Deployment failed with state: ERROR', self.err)
        self.assertEqual(server.calls()[-2:], ['DELETE /v13/deployments/dpl_1', 'DELETE /v9/projects/prj_1'])
        self.assertEqual(RelayPool.load().entries, ())

    def test_a_deployment_that_never_gets_ready_times_out(self):
        self.patch(patch.object(relay_deploy, 'POLL_TIMEOUT', 0.05))
        server = self.fake('vercel', vercel_routes(polls=('BUILDING',)))
        self.assertEqual(self.deploy('vercel'), 1)
        self.assertIn('timed out', self.err)
        self.assertIn('DELETE /v9/projects/prj_1', server.calls())


class DenoDeployTests(DeployCase):
    def test_deploys_in_order_with_the_secret_only_as_an_env_var(self):
        server = self.fake('deno', deno_routes())
        self.assertEqual(self.deploy('deno'), 0, self.everything)
        self.assertEqual(server.calls(), ['POST /apps', 'PATCH /apps/app1', 'POST /apps/app1/deploy',
                                          'GET /revisions/rev1'])
        self.assertAuthorized(server)
        secret = self.pool_secret()
        env = server.requests[1].json['env_vars']
        self.assertEqual(env, [{'key': 'BLEO_RELAY_SECRET', 'value': secret, 'secret': True}])
        asset = server.requests[2].json['assets']['main.ts']
        self.assertWrapper(asset['content'])
        self.assertNotIn(secret, asset['content'])
        self.assertEqual(server.requests[0].json['slug'], 'relay-x')
        self.assertEqual(server.raw().count(secret), 1)
        entry = RelayPool.load().entries[0]
        self.assertEqual((entry.url, entry.label, entry.auth), ('https://relay-x.deno.net', 'deno', secret))
        self.assertClean()

    def test_falls_back_to_the_managed_domain_for_the_url(self):
        revision = {'status': 'succeeded', 'timelines': []}
        self.fake('deno', deno_routes(revision=revision, domains=[{'domain': 'custom.example.com'}, {'domain': '*.org.deno.net'}]))
        self.assertEqual(self.deploy('deno'), 0)
        self.assertEqual(RelayPool.load().entries[0].url, 'https://relay-x.org.deno.net')

    def test_no_url_removes_the_app(self):
        server = self.fake('deno', deno_routes(revision={'status': 'succeeded'}))
        self.assertEqual(self.deploy('deno'), 1)
        self.assertIn('could not determine the public URL', self.err)
        self.assertEqual(server.calls()[-1], 'DELETE /apps/app1')

    def test_api_errors_clean_up_and_leave_the_pool_alone(self):
        cases = [({'POST /apps': (401, b'token rejected')}, 'authentication failed', 0),
                 ({'POST /apps': (409, {'error': {'message': 'dup'}})}, 'already exists', 0),
                 ({'PATCH /apps/app1': (403, {'error': {'message': 'cannot set env'}})}, 'cannot set env', 1),
                 ({'POST /apps/app1/deploy': (500, {'error': {'message': 'boom'}})}, '(HTTP 500): boom', 1)]
        for fail, expected, deletes in cases:
            with self.subTest(expected=expected):
                server = self.fake('deno', deno_routes(fail=fail))
                self.assertEqual(self.deploy('deno'), 1)
                self.assertIn(expected, self.err)
                self.assertEqual(RelayPool.load().entries, ())
                self.assertEqual(server.calls().count('DELETE /apps/app1'), deletes)
                if 'env' in expected:
                    self.assertNotIn('POST /apps/app1/deploy', server.calls())
        self.assertClean()

    def test_a_failed_build_removes_the_app(self):
        server = self.fake('deno', deno_routes(revision={'status': 'failed', 'failure_reason': 'syntax'}))
        self.assertEqual(self.deploy('deno'), 1)
        self.assertIn('build failed (syntax)', self.err)
        self.assertEqual(server.calls()[-1], 'DELETE /apps/app1')


class DeployPoolAndTokenTests(DeployCase):
    def test_a_taken_label_is_refused_before_any_request(self):
        server = self.fake('cloudflare', cloudflare_routes())
        RelayPool(entries=[RelayEntry('https://old.example.com', 'cloudflare', 'old-secret-0000000000', '')]).save()
        self.assertEqual(self.deploy('cloudflare'), 1)
        self.assertIn("already exists", self.err)
        self.assertIn('--replace', self.err)
        self.assertEqual(server.requests, [])
        self.assertEqual([e.url for e in RelayPool.load().entries], ['https://old.example.com'])
        self.assertEqual(self.deploy('cloudflare', '--label', 'CLOUDFLARE'), 1)  # labels compare without case
        self.assertEqual(server.requests, [])

    def test_replace_removes_the_old_entry_after_the_new_deploy_succeeds(self):
        server = self.fake('cloudflare', cloudflare_routes())
        RelayPool(entries=[RelayEntry('https://old.example.com', 'cloudflare', 'old-secret-0000000000', ''),
                           RelayEntry('https://keep.example.com', 'keep', 'keep-secret-000000000', '')]).save()
        self.assertEqual(self.deploy('cloudflare', '--replace'), 0)
        entries = {e.label: e for e in RelayPool.load().entries}
        self.assertEqual(set(entries), {'cloudflare', 'keep'})
        self.assertEqual(entries['cloudflare'].url, 'https://relay-x.me.workers.dev')
        self.assertNotEqual(entries['cloudflare'].auth, 'old-secret-0000000000')
        self.assertTrue(server.requests)

    def test_a_failed_replace_keeps_the_old_entry(self):
        self.fake('cloudflare', cloudflare_routes(fail={'PUT /accounts/acc1/workers/scripts/relay-x': (500, {})}))
        RelayPool(entries=[RelayEntry('https://old.example.com', 'cloudflare', 'old-secret-0000000000', '')]).save()
        self.assertEqual(self.deploy('cloudflare', '--replace'), 1)
        self.assertEqual([e.url for e in RelayPool.load().entries], ['https://old.example.com'])

    def test_the_token_is_read_from_its_environment_variable_not_prompted(self):
        self.fake('cloudflare', cloudflare_routes())
        with patch('bleo.relay_deploy.getpass.getpass') as prompt:
            self.assertEqual(self.deploy('cloudflare'), 0)
        prompt.assert_not_called()

    def test_without_the_variable_the_token_is_asked_for_hidden(self):
        for name in ('CLOUDFLARE_API_TOKEN', 'DENO_DEPLOY_TOKEN', 'VERCEL_TOKEN'):
            os.environ.pop(name)
        server = self.fake('vercel', vercel_routes())
        with patch('bleo.relay_deploy._can_prompt', return_value=True), \
                patch('bleo.relay_deploy.getpass.getpass', return_value=f'  {TOKEN}  ') as prompt:
            self.assertEqual(self.deploy('vercel'), 0)
        prompt.assert_called_once()
        self.assertAuthorized(server)
        self.assertNotIn(TOKEN, prompt.call_args[0][0])
        self.assertClean()

    def test_an_empty_token_is_refused(self):
        for name in ('CLOUDFLARE_API_TOKEN', 'DENO_DEPLOY_TOKEN', 'VERCEL_TOKEN'):
            os.environ.pop(name)
        server = self.fake('deno', deno_routes())
        with patch('bleo.relay_deploy._can_prompt', return_value=True), \
                patch('bleo.relay_deploy.getpass.getpass', return_value=' '):
            self.assertEqual(self.deploy('deno'), 1)
        self.assertEqual(server.requests, [])

    def test_without_a_terminal_the_token_is_not_prompted_for(self):
        for name in ('CLOUDFLARE_API_TOKEN', 'DENO_DEPLOY_TOKEN', 'VERCEL_TOKEN'):
            os.environ.pop(name)
        server = self.fake('cloudflare', cloudflare_routes())
        with patch('bleo.relay_deploy._can_prompt', return_value=False), \
                patch('bleo.relay_deploy.getpass.getpass') as prompt:
            self.assertEqual(self.deploy('cloudflare'), 1)
        prompt.assert_not_called()
        self.assertEqual(server.requests, [])

    def test_the_token_and_secret_reach_no_file_but_the_secret_reaches_only_the_pool(self):
        self.fake('vercel', vercel_routes())
        self.assertEqual(self.deploy('vercel'), 0)
        secret = self.pool_secret()
        hits = {}
        for root, _, names in os.walk(self.home):
            for name in names:
                text = Path(root, name).read_bytes().decode('utf-8', 'replace')
                hits[name] = (TOKEN in text, secret in text)
        self.assertTrue(all(not token for token, _ in hits.values()))
        self.assertEqual([name for name, (_, has) in hits.items() if has], ['relays.json'])

    def test_default_name_and_label(self):
        server = self.fake('cloudflare', cloudflare_routes())
        self.assertEqual(self.run_cli('relay', 'deploy', 'cloudflare'), 0)
        name = re.search(r'^name: (bleo-relay-[a-z0-9]{6})$', self.out, re.M).group(1)
        self.assertIn(f'/scripts/{name}', server.calls()[1])
        self.assertEqual(RelayPool.load().entries[0].label, 'cloudflare')
        self.assertNotEqual(relay_deploy.default_name(), relay_deploy.default_name())

    def test_names_are_made_safe_for_each_platform(self):
        self.assertEqual(relay_deploy.platform_name('cloudflare', 'My_Relay!!'), 'my-relay')
        self.assertEqual(relay_deploy.platform_name('deno', 'ab'), 'ab-relay')
        self.assertEqual(len(relay_deploy.platform_name('deno', 'x' * 50)), 32)
        self.assertEqual(len(relay_deploy.platform_name('cloudflare', 'x' * 80)), 58)
        with self.assertRaises(ValueError):
            relay_deploy.platform_name('vercel', '!!!')
        self.assertEqual(self.run_cli('relay', 'deploy', 'vercel', '--name', '!!!'), 1)
        self.assertIn('--name', self.err)


class DeployProbeTests(DeployCase):
    def setUp(self):
        super().setUp()
        self.fake('cloudflare', cloudflare_routes())
        self.patch(patch.object(cli, 'DEPLOY_PROBE_INTERVAL', 0.001))

    def test_no_probe_skips_it(self):
        with patch('bleo.cli._relay_probe') as probe:
            self.assertEqual(self.deploy('cloudflare', '--no-probe'), 0)
        probe.assert_not_called()
        self.assertIn('probe: skipped', self.out)

    def test_the_probe_is_retried_until_it_passes(self):
        results = iter([1, 1, 0])
        calls = []

        def probe(args, ui):
            calls.append(args.label)
            code = next(results)
            print(f'  attempt {len(calls)}\n  result: {"PASS" if code == 0 else "FAIL"}')
            return code
        self.patch(patch.object(cli, 'DEPLOY_PROBE_WINDOW', 30.0))
        with patch('bleo.cli._relay_probe', probe):
            self.assertEqual(self.deploy('cloudflare'), 0)
        self.assertEqual(calls, ['cloudflare'] * 3)
        self.assertIn('attempt 3', self.out)
        self.assertNotIn('attempt 1', self.out)  # only the last attempt is shown
        self.assertIn('probe result: PASS', self.out)

    def test_the_probe_fails_after_the_window_and_the_relay_stays_in_the_pool(self):
        calls = []

        def probe(args, ui):
            calls.append(1)
            print('  result: FAIL')
            return 1
        self.patch(patch.object(cli, 'DEPLOY_PROBE_WINDOW', 0.05))
        with patch('bleo.cli._relay_probe', probe):
            self.assertEqual(self.deploy('cloudflare'), 1)
        self.assertGreater(len(calls), 1)
        self.assertIn('probe result: FAIL', self.out)
        self.assertIn('bleo relay remove cloudflare', self.out)
        self.assertEqual(RelayPool.load().entries[0].url, 'https://relay-x.me.workers.dev')
        self.assertClean()

    def test_the_real_probe_runs_against_the_new_relay_with_no_direct_request(self):
        relay = self.serve(stream_reply(['hello']))
        insecure = partial(validate_relay_url, allow_insecure=True)
        self.patch(patch('bleo.relay_state.validate_relay_url', insecure))
        self.patch(patch.object(cli, 'RelayRouter', self.insecure_router()))
        with patch('bleo.relay_deploy.deploy', return_value=relay.url) as deploy, \
                patch('bleo.cli._relay_probe', RealProbe.function):
            self.assertEqual(self.deploy('cloudflare'), 0)
        secret = self.pool_secret()
        self.assertEqual(relay.hits[0].headers['x-relay-auth'], secret)
        self.assertEqual(self.brave.hits, [])
        self.assertIn('route: relay cloudflare', self.out)
        self.assertIn('result: PASS', self.out)
        self.assertEqual(deploy.call_args[0][3], secret)
        self.assertClean(secret)


class RealProbe:
    """The unpatched probe, captured at import time before any test patches it."""
    function = cli._relay_probe


if __name__ == '__main__':
    unittest.main()
