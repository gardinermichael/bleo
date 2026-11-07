"""Publish the local bleo API through Cloudflare Tunnel or Tailscale Serve.

The API always listens on 127.0.0.1; this module only exposes that port.
Nothing here prints or logs a token: ``log`` receives URLs, port numbers and
status text only.
"""
import json
import re
import shutil
import subprocess
import threading

from .exceptions import BleoError


DEFAULT_TAILSCALE_PORT = 8443
QUICK_TUNNEL_HOST_SUFFIX = '.trycloudflare.com'
STARTUP_TIMEOUT = 30.0
STOP_TIMEOUT = 5.0
COMMAND_TIMEOUT = 30.0
REGISTERED_MARKER = 'Registered tunnel connection'
_QUICK_URL = re.compile(r'https://[A-Za-z0-9-]+\.trycloudflare\.com(?![A-Za-z0-9.-])')
_INSTALL_HINTS = {
    'cloudflared': 'brew install cloudflared',
    'tailscale': 'brew install tailscale',
}


class TunnelError(BleoError):
    """A tunnel could not be built, started or safely stopped."""


def _check_port(port, label):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise TunnelError(f'{label} must be a port number from 1 to 65535')
    return port


def _local_target(api_port):
    return f'http://127.0.0.1:{_check_port(api_port, "api_port")}'


def cloudflare_command(api_port, *, name=None, quick=False):
    """Return the cloudflared argv for a named tunnel or a quick tunnel."""
    if name and quick:
        raise TunnelError('pass either a tunnel name or --quick, not both')
    if not name and not quick:
        raise TunnelError('named tunnel required for streaming; pass --quick for a trycloudflare URL without streaming')
    target = _local_target(api_port)
    if quick:
        return ['cloudflared', 'tunnel', '--no-autoupdate', '--url', target]
    return ['cloudflared', 'tunnel', '--no-autoupdate', 'run', '--url', target, name]


def parse_quick_url(line):
    """Return the trycloudflare https URL found in a cloudflared log line, if any."""
    match = _QUICK_URL.search(line)
    return match.group(0) if match else None


def _run_ok(runner, args, action):
    try:
        done = runner.run(args, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TunnelError(f'{action} could not run: {error}') from error
    if done.returncode != 0:
        raise TunnelError(f'{action} exited with status {done.returncode}')
    return done


def tailscale_status(runner=subprocess):
    """Return ``tailscale serve status --json`` as a dict; no config gives {}."""
    done = _run_ok(runner, ['tailscale', 'serve', 'status', '--json'], 'tailscale serve status')
    text = (done.stdout or '').strip()
    if text in ('', '{}'):
        return {}
    try:
        status = json.loads(text)
    except ValueError as error:
        raise TunnelError('tailscale serve status did not return JSON') from error
    if not isinstance(status, dict):
        raise TunnelError('tailscale serve status did not return a JSON object')
    return status


def _web_entries_on_port(status, https_port):
    web = status.get('Web') or {}
    return {
        key: value
        for key, value in web.items()
        if key.rpartition(':')[2] == str(https_port)
    }


def https_port_in_use(status, https_port):
    """True when the HTTPS port is mapped under TCP or as the port of any Web key."""
    tcp = status.get('TCP') or {}
    return str(https_port) in tcp or bool(_web_entries_on_port(status, https_port))


def _proxies_root_only_to(site, target):
    handlers = site.get('Handlers') if isinstance(site, dict) else None
    if not isinstance(handlers, dict) or set(handlers) != {'/'}:
        return False
    root = handlers['/']
    return isinstance(root, dict) and root.get('Proxy') == target


def tailscale_owned(status, https_port, api_port):
    """True only when the mapping on that HTTPS port proxies "/" to exactly this bleo."""
    entries = _web_entries_on_port(status, https_port)
    if not entries:
        return False
    target = _local_target(api_port)
    return all(_proxies_root_only_to(site, target) for site in entries.values())


def _tailscale_install_command(api_port, https_port):
    return ['tailscale', 'serve', '--bg', f'--https={https_port}', _local_target(api_port)]


def _tailscale_off_command(https_port):
    return ['tailscale', 'serve', f'--https={https_port}', 'off']


def _tailscale_dns_name(runner):
    done = _run_ok(runner, ['tailscale', 'status', '--json'], 'tailscale status')
    try:
        status = json.loads(done.stdout or '')
    except ValueError as error:
        raise TunnelError('tailscale status did not return JSON') from error
    node = status.get('Self') if isinstance(status, dict) else None
    dns = node.get('DNSName') if isinstance(node, dict) else None
    if not isinstance(dns, str) or not dns.rstrip('.'):
        raise TunnelError('tailscale reports no DNS name for this device; check MagicDNS and HTTPS certificates')
    return dns.rstrip('.')


def _require_binary(binary):
    if shutil.which(binary) is None:
        raise TunnelError(f'{binary} is not installed or not on PATH; install it with `{_INSTALL_HINTS[binary]}`')


class Tunnel:
    """Context manager that publishes the local API and tears the tunnel down on exit."""

    def __init__(self, kind, api_port, *, name=None, quick=False,
                 https_port=DEFAULT_TAILSCALE_PORT, log=print, runner=subprocess,
                 timeout=STARTUP_TIMEOUT):
        if kind not in ('cloudflare', 'tailscale'):
            raise TunnelError(f'unknown tunnel kind: {kind!r}')
        self.kind = kind
        self.api_port = _check_port(api_port, 'api_port')
        self.name = name
        self.quick = quick
        self.https_port = _check_port(https_port, 'https_port')
        self.url = None
        self._log = log
        self._runner = runner
        self._timeout = timeout
        self._proc = None
        self._tailscale_mapped = False
        if kind == 'cloudflare':
            cloudflare_command(self.api_port, name=name, quick=quick)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop()
        return False

    def start(self):
        if self.kind == 'cloudflare':
            self.url = self._start_cloudflare()
        else:
            self.url = self._start_tailscale()
        return self.url

    def stop(self):
        if self.kind == 'cloudflare':
            self._stop_cloudflare()
        else:
            self._stop_tailscale()

    def _start_cloudflare(self):
        _require_binary('cloudflared')
        command = cloudflare_command(self.api_port, name=self.name, quick=self.quick)
        if self.quick:
            self._log('warning: quick tunnels do not support streaming; stream=true through the quick-tunnel host is refused with 400')
        self._proc = self._runner.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        text=True, bufsize=1)
        found = threading.Event()
        result = {}
        threading.Thread(target=self._watch_stderr, args=(self._proc, found, result), daemon=True).start()
        found.wait(self._timeout)
        value = result.get('value')
        if value is None:
            if result.get('eof'):
                reason = 'cloudflared exited before reporting a tunnel'
            else:
                reason = f'cloudflared did not report a tunnel within {self._timeout:g} s'
            self.stop()
            raise TunnelError(reason)
        self._log(f'tunnel up: {value} -> 127.0.0.1:{self.api_port}')
        return value

    def _watch_stderr(self, proc, found, result):
        # Keeps draining stderr after the URL is found so cloudflared never blocks on a full pipe.
        try:
            for line in proc.stderr:
                if 'value' in result:
                    continue
                value = self._startup_value(line)
                if value:
                    result['value'] = value
                    found.set()
        finally:
            result['eof'] = True
            found.set()

    def _startup_value(self, line):
        if self.quick:
            return parse_quick_url(line)
        if REGISTERED_MARKER in line:
            return f'named tunnel {self.name}'
        return None

    def _stop_cloudflare(self):
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except OSError:
            pass
        self._log('cloudflared tunnel stopped')

    def _start_tailscale(self):
        _require_binary('tailscale')
        status = tailscale_status(self._runner)
        if https_port_in_use(status, self.https_port):
            raise TunnelError(f'tailscale HTTPS port {self.https_port} already has a Serve mapping; pick another with --tailscale-port')
        # Marked before the install runs: stop() removes only what still proxies to this bleo.
        self._tailscale_mapped = True
        try:
            _run_ok(self._runner, _tailscale_install_command(self.api_port, self.https_port),
                    'tailscale serve install')
            dns = _tailscale_dns_name(self._runner)
        except TunnelError:
            self.stop()
            raise
        url = f'https://{dns}:{self.https_port}'
        self._log(f'tunnel up: {url} -> 127.0.0.1:{self.api_port}')
        return url

    def _stop_tailscale(self):
        if not self._tailscale_mapped:
            return
        self._tailscale_mapped = False
        try:
            status = tailscale_status(self._runner)
        except TunnelError as error:
            self._log(f'could not re-read tailscale serve status; leaving HTTPS port {self.https_port} unchanged: {error}')
            return
        if not tailscale_owned(status, self.https_port, self.api_port):
            self._log(f'HTTPS port {self.https_port} no longer proxies to 127.0.0.1:{self.api_port}; leaving it unchanged')
            return
        try:
            _run_ok(self._runner, _tailscale_off_command(self.https_port), 'tailscale serve removal')
        except TunnelError as error:
            self._log(f'could not remove tailscale mapping on HTTPS port {self.https_port}: {error}')
            return
        self._log(f'removed tailscale mapping on HTTPS port {self.https_port}')
