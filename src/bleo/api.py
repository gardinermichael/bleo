"""OpenAI-compatible HTTP API over bleo, built on the Python standard library.

Routes: ``GET /health`` (open), ``GET /v1/models`` and ``POST /v1/chat/completions``
(both need ``Authorization: Bearer <token>``). The server always binds 127.0.0.1;
use ``--tunnel`` to publish it. The token is never printed or logged.

Each request builds its own ``Bleo`` through ``make_client`` and its own message list, so
conversations, cookies and the reported model never mix between requests. Nothing in a
request body can change credentials, proxy, gateways or routing.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import signal
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import tunnel
from .client import Bleo
from .exceptions import AuthError, BleoError, RatelimitError, RelayExhaustedError
from .relay import RelayRouter
from .relay_state import RelayPool
from .models import list_models, resolve_model

HOST = '127.0.0.1'
DEFAULT_PORT = 8317
MAX_BODY_BYTES = 20 * 1024 * 1024
MAX_DRAIN_BYTES = 1024 * 1024
CHARS_PER_TOKEN = 4
MAX_STOP_SEQUENCES = 4
SOCKET_TIMEOUT = 60.0
UPSTREAM_TIMEOUT = 60.0

_ROLES = ('system', 'user', 'assistant')
# Rejected whenever present and not null.
_REJECTED_PARAMS = ('tools', 'tool_choice', 'functions', 'function_call',
                    'parallel_tool_calls', 'top_logprobs')
_CHAT_PATHS = ('/v1/chat/completions', '/chat/completions')
_MODEL_PATHS = ('/v1/models', '/models')


def make_client(model, relays=False):
    """Return a fresh, request-local client. relays is the server's shared RelayRouter, or
    False for direct requests (a request never reads the relay file itself)."""
    return Bleo(model=model, relays=relays)


def shared_router(relays=None):
    """The one router every request shares: None loads the saved pool and returns None when
    it is empty or off; False means no relays; a RelayRouter is used as given."""
    if relays is False or isinstance(relays, RelayRouter):
        return relays or None
    pool = RelayPool.load()
    if not pool.entries or pool.mode == 'off':
        return None
    # Bleo's own timeout, which a router passed in from outside would otherwise bypass.
    return RelayRouter(pool, header_timeout=UPSTREAM_TIMEOUT, stall_timeout=UPSTREAM_TIMEOUT)


class ApiError(Exception):
    def __init__(self, status, message, *, param=None, code=None, type_='invalid_request_error',
                 retry_after=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.param = param
        self.code = code
        self.type = type_
        self.retry_after = retry_after

    def body(self):
        return {'error': {'message': self.message, 'type': self.type,
                          'param': self.param, 'code': self.code}}


# ── token ────────────────────────────────────────────────────────────────

def bleo_home():
    return Path(os.environ.get('BLEO_HOME') or Path.home() / '.bleo')


def token_path():
    return bleo_home() / 'api-token'


def _env_token():
    return (os.environ.get('BLEO_API_TOKEN') or '').strip()


def load_token():
    """Return the API token: BLEO_API_TOKEN, else the token file (created on first use)."""
    env = _env_token()
    if env:
        return env
    path = token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token = path.read_text(encoding='utf-8').strip()
        if not token:
            raise BleoError(f'API token file {path} is empty; delete it to generate a new token') from None
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
        return token
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(token + '\n')
    return token


def _token_source():
    return 'set by BLEO_API_TOKEN' if _env_token() else f'file {token_path()}'


# ── request parsing ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: tuple
    stream: bool
    stop: tuple
    max_tokens: int | None


def _bad(message, param, code='invalid_value'):
    return ApiError(400, message, param=param, code=code)


def _unsupported(message, param):
    return ApiError(400, message, param=param, code='unsupported_parameter')


def _parse_part(part, where, role):
    if not isinstance(part, dict):
        raise _bad('Content parts must be objects', where)
    kind = part.get('type')
    if kind == 'text' and isinstance(part.get('text'), str):
        return {'type': 'text', 'text': part['text']}
    if kind == 'image_url':
        image = part.get('image_url')
        url = image.get('url') if isinstance(image, dict) else image
        if role != 'user':
            raise _bad('Images are only allowed in user messages', where)
        if not isinstance(url, str) or not url:
            raise _bad("'image_url' needs a url string", where)
        return {'type': 'image_url', 'image_url': {'url': url}}
    raise _unsupported("Content parts must be 'text' or 'image_url'", f'{where}.type')


def _parse_content(content, where, role):
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        return [_parse_part(part, f'{where}[{index}]', role) for index, part in enumerate(content)]
    raise _bad('Content must be a string or a non-empty list of parts', where)


def _parse_messages(value):
    if not isinstance(value, list) or not value:
        raise _bad("'messages' must be a non-empty array", 'messages')
    messages = []
    for index, message in enumerate(value):
        where = f'messages[{index}]'
        if not isinstance(message, dict):
            raise _bad('Each message must be an object', where)
        role = message.get('role')
        if role in ('tool', 'function'):
            raise _unsupported(f"Role '{role}' is not supported", f'{where}.role')
        if role not in _ROLES:
            raise _bad("Role must be 'system', 'user' or 'assistant'", f'{where}.role')
        for field in ('tool_calls', 'function_call'):
            if message.get(field):
                raise _unsupported(f"'{field}' is not supported", f'{where}.{field}')
        messages.append({'role': role, 'content': _parse_content(message.get('content'), f'{where}.content', role)})
    return tuple(messages)


def _parse_model(value, default):
    if value is None:
        return default
    if not isinstance(value, str):
        raise _bad("'model' must be a string", 'model')
    resolved = resolve_model(value) if value.strip() else None
    if resolved not in list_models():
        raise ApiError(400, f'Unknown model: {value[:80]!r}', param='model', code='model_not_found')
    return resolved


def _parse_stop(value):
    if value is None:
        return ()
    stops = [value] if isinstance(value, str) else value
    if (not isinstance(stops, list) or not stops or len(stops) > MAX_STOP_SEQUENCES
            or not all(isinstance(item, str) and item for item in stops)):
        raise _bad(f"'stop' must be a non-empty string or up to {MAX_STOP_SEQUENCES} non-empty strings", 'stop')
    return tuple(stops)


def _parse_max_tokens(payload):
    limits = []
    for name in ('max_tokens', 'max_completion_tokens'):
        value = payload.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _bad(f"'{name}' must be a positive integer", name)
        limits.append(value)
    return min(limits) if limits else None


def _check_unsupported(payload):
    for name in _REJECTED_PARAMS:
        if payload.get(name) is not None:
            raise _unsupported(f"'{name}' is not supported", name)
    if payload.get('logprobs') not in (None, False):
        raise _unsupported("'logprobs' is not supported", 'logprobs')
    count = payload.get('n')
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count != 1):
        raise _unsupported('Only n=1 is supported', 'n')
    fmt = payload.get('response_format')
    if fmt is not None and not (isinstance(fmt, dict) and fmt.get('type') == 'text'):
        raise _unsupported("Only response_format type 'text' is supported", 'response_format')


def parse_chat_request(payload, default_model):
    if not isinstance(payload, dict):
        raise ApiError(400, 'Request body must be a JSON object', code='invalid_request')
    _check_unsupported(payload)
    stream = payload.get('stream')
    if stream is not None and not isinstance(stream, bool):
        raise _bad("'stream' must be a boolean", 'stream')
    return ChatRequest(model=_parse_model(payload.get('model'), default_model),
                       messages=_parse_messages(payload.get('messages')),
                       stream=bool(stream), stop=_parse_stop(payload.get('stop')),
                       max_tokens=_parse_max_tokens(payload))


def _is_quick_tunnel(headers):
    values = [headers.get('Host', '')] + headers.get('X-Forwarded-Host', '').split(',')
    for value in values:
        host = value.strip().lower()
        if host.count(':') == 1:
            host = host.rsplit(':', 1)[0]
        if host.endswith(tunnel.QUICK_TUNNEL_HOST_SUFFIX):
            return True
    return False


# ── output limits ────────────────────────────────────────────────────────

class Limiter:
    """Cut text at a stop sequence or an approximate token limit; one per request."""

    def __init__(self, stop, max_tokens):
        self._stop = stop
        self._hold = max((len(item) for item in stop), default=1) - 1
        self._max_chars = None if max_tokens is None else max_tokens * CHARS_PER_TOKEN
        self._buffer = ''
        self._emitted = 0

    def _cap(self, text, reason):
        if self._max_chars is not None:
            room = self._max_chars - self._emitted
            if len(text) > room:
                text, reason = text[:room], 'length'
            elif reason is None and len(text) == room and self._buffer:
                reason = 'length'
        self._emitted += len(text)
        return text, reason

    def feed(self, chunk):
        """Return (text to emit, finish reason or None)."""
        self._buffer += chunk
        hits = [hit for hit in (self._buffer.find(item) for item in self._stop) if hit >= 0]
        if hits:
            text, self._buffer = self._buffer[:min(hits)], ''
            return self._cap(text, 'stop')
        cut = len(self._buffer) - min(self._hold, len(self._buffer))
        text, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return self._cap(text, None)

    def flush(self):
        text, self._buffer = self._buffer, ''
        return self._cap(text, None)


def limited(chunks, limiter):
    """Yield (text, reason); the item carrying a reason is always the last."""
    for chunk in chunks:
        text, reason = limiter.feed(chunk)
        if reason:
            yield text, reason
            return
        if text:
            yield text, None
    text, reason = limiter.flush()
    yield text, reason or 'stop'


# ── errors ───────────────────────────────────────────────────────────────

def _log_internal(error):
    # Only the exception type: messages and tracebacks could carry request data.
    print(f'bleo-api: internal error: {type(error).__name__}', file=sys.stderr, flush=True)


def upstream_error(error):
    """Map an exception from the upstream path to an ApiError, or None if it is not one."""
    if isinstance(error, AuthError):
        return ApiError(502, 'Upstream authentication failed; check bleo service credentials',
                        type_='upstream_error', code='upstream_auth_failed')
    if isinstance(error, RatelimitError):
        return ApiError(429, str(error) or 'Upstream rate limit reached', type_='rate_limit_error',
                        code='rate_limit_exceeded', retry_after=error.retry_after)
    if isinstance(error, RelayExhaustedError) and isinstance(error.__cause__, (OSError, HTTPException)):
        error = error.__cause__  # the direct fallback's own failure, mapped as it is without relays
    elif isinstance(error, BleoError):
        return ApiError(502, str(error) or 'Upstream error', type_='upstream_error', code='upstream_error')
    if isinstance(error, ValueError):
        return ApiError(400, str(error), code='invalid_request')
    if isinstance(error, TimeoutError):
        return ApiError(504, 'Upstream timed out', type_='upstream_error', code='upstream_timeout')
    if isinstance(error, (OSError, HTTPException)):
        return ApiError(502, 'Upstream connection failed', type_='upstream_error', code='upstream_unavailable')
    return None


class _ClientGone(Exception):
    """The client socket failed while we wrote to or read from it: a real disconnect."""


@contextmanager
def _client_io():
    """Wrap client-socket I/O only. Upstream errors must never pass through here as disconnects."""
    try:
        yield
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
        raise _ClientGone from None


def _server_error():
    return ApiError(500, 'Internal server error', type_='server_error', code='internal_error')


# ── server ───────────────────────────────────────────────────────────────

def _chunk(completion_id, created, model, delta, finish=None):
    return {'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model,
            'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}


class _Handler(BaseHTTPRequestHandler):
    server_version = 'bleo-api'
    timeout = SOCKET_TIMEOUT

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        # Requests are never logged: paths and headers can carry the token.
        return

    def do_GET(self):  # noqa: N802
        self._dispatch('GET')

    def do_POST(self):  # noqa: N802
        self._dispatch('POST')

    def _dispatch(self, method):
        self._streaming = False
        self._body_read = False
        try:
            self._route(method)
        except ApiError as error:
            self._reply_error(error)
        except _ClientGone:
            pass
        except Exception as error:  # noqa: BLE001 - last resort
            _log_internal(error)
            self._reply_error(_server_error())

    def _route(self, method):
        path = urlsplit(self.path).path
        if method == 'GET' and path == '/health':
            return self._send_json(200, {'status': 'ok'})
        self._authenticate()
        if method == 'GET' and path in _MODEL_PATHS:
            return self._send_json(200, {'object': 'list', 'data': [
                {'id': name, 'object': 'model', 'created': 0, 'owned_by': 'brave'} for name in list_models()]})
        if method == 'POST' and path in _CHAT_PATHS:
            return self._chat()
        raise ApiError(404, 'Not found', type_='not_found_error', code='not_found')

    def _authenticate(self):
        scheme, _, credential = self.headers.get('Authorization', '').partition(' ')
        good = hmac.compare_digest(credential.strip().encode('utf-8'), self.server.token.encode('utf-8'))
        if scheme.lower() != 'bearer' or not good:
            raise ApiError(401, 'Missing or invalid API token', type_='authentication_error',
                           code='invalid_api_key')

    # replies
    def _send_json(self, status, payload, extra=()):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        for name, value in extra:
            self.send_header(name, value)
        with _client_io():
            self.end_headers()
            self.wfile.write(body)

    def _drain(self):
        # Closing with unread request bytes can reset the connection before the client reads the error.
        raw = self.headers.get('Content-Length', '')
        if self._body_read or not (raw.isascii() and raw.isdigit()) or int(raw) > MAX_DRAIN_BYTES:
            return
        try:
            self.rfile.read(int(raw))
        except OSError:
            pass

    def _reply_error(self, error):
        if self._streaming:
            return
        extra = []
        if error.status == 401:
            extra.append(('WWW-Authenticate', 'Bearer'))
        if error.retry_after is not None:
            extra.append(('Retry-After', str(int(error.retry_after) + 1)))
        try:
            self._drain()
            self._send_json(error.status, error.body(), extra)
        except _ClientGone:
            pass

    def _read_json(self):
        if 'chunked' in self.headers.get('Transfer-Encoding', '').lower():
            raise ApiError(400, 'Chunked request bodies are not supported', code='invalid_request')
        raw = self.headers.get('Content-Length')
        if raw is None or not (raw.isascii() and raw.isdigit()):
            raise ApiError(400, 'A valid Content-Length header is required', code='invalid_content_length')
        length = int(raw)
        if length > MAX_BODY_BYTES:
            raise ApiError(413, f'Request body exceeds {MAX_BODY_BYTES} bytes', code='body_too_large')
        with _client_io():
            body = self.rfile.read(length)
        self._body_read = True
        if len(body) != length:
            raise ApiError(400, 'Request body is shorter than Content-Length', code='invalid_request')
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            raise ApiError(400, 'Invalid JSON body', code='invalid_json') from None

    # chat
    def _chat(self):
        request = parse_chat_request(self._read_json(), self.server.default_model)
        if request.stream and _is_quick_tunnel(self.headers):
            raise ApiError(400, 'Cloudflare quick tunnels do not support streaming; use stream=false '
                                'or a named tunnel', param='stream', code='streaming_unsupported')
        limiter = Limiter(request.stop, request.max_tokens)
        try:
            shared = {} if self.server.router is None else {'relays': self.server.router}
            with make_client(request.model, **shared) as client:
                chunks = client.complete(list(request.messages), model=request.model)
                try:
                    if request.stream:
                        self._chat_stream(client, chunks, limiter, request)
                    else:
                        self._chat_json(client, chunks, limiter, request)
                finally:
                    chunks.close()
        except (ApiError, _ClientGone):
            raise
        except Exception as error:  # noqa: BLE001
            mapped = upstream_error(error)
            if mapped is None:
                raise
            raise mapped from None

    @staticmethod
    def _served(client, request):
        return client.last_model or request.model

    def _chat_json(self, client, chunks, limiter, request):
        parts, reason = [], 'stop'
        for text, reason in limited(chunks, limiter):
            parts.append(text)
        self._send_json(200, {
            'id': f'chatcmpl-{uuid.uuid4().hex[:24]}', 'object': 'chat.completion',
            'created': int(time.time()), 'model': self._served(client, request),
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': ''.join(parts)},
                         'finish_reason': reason}]})

    def _event(self, payload):
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        with _client_io():
            self.wfile.write(f'data: {data}\n\n'.encode('utf-8'))

    def _chat_stream(self, client, chunks, limiter, request):
        pieces = limited(chunks, limiter)
        try:
            # Prime before sending headers so early upstream failures are real HTTP errors.
            item = next(pieces)
            completion_id, created = f'chatcmpl-{uuid.uuid4().hex[:24]}', int(time.time())
            model = self._served(client, request)
            self._streaming = True
            self.send_response(200)
            for name, value in (('Content-Type', 'text/event-stream'), ('Cache-Control', 'no-cache'),
                                ('Connection', 'close'), ('X-Accel-Buffering', 'no')):
                self.send_header(name, value)
            with _client_io():
                self.end_headers()
            self._event(_chunk(completion_id, created, model, {'role': 'assistant', 'content': ''}))
            while True:
                text, reason = item
                if text:
                    self._event(_chunk(completion_id, created, model, {'content': text}))
                if reason:
                    self._event(_chunk(completion_id, created, model, {}, reason))
                    break
                try:
                    item = next(pieces)
                except Exception as error:  # noqa: BLE001 - reported to the client as an SSE event
                    self._stream_failure(error)
                    break
            self._event('[DONE]')
        except _ClientGone:
            return  # client left; _chat closes the upstream generator
        finally:
            pieces.close()

    def _stream_failure(self, error):
        mapped = upstream_error(error)
        if mapped is None:
            _log_internal(error)
            mapped = _server_error()
        self._event(mapped.body())


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, token, default_model, router=None, host=HOST):
        if not token:
            raise ValueError('an API token is required')
        super().__init__((host, port), _Handler)
        self.token = token
        self.default_model = default_model
        self.router = router

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if not isinstance(error, (ConnectionError, TimeoutError)):
            _log_internal(error)


def create_server(port=DEFAULT_PORT, *, token, default_model=None, relays=None, host=HOST):
    """Return a server bound to host:port (127.0.0.1 unless told otherwise; port 0 picks a free
    port); call serve_forever().

    relays: None shares one router built from the saved relay pool (none when it is empty or
    off), False uses no relays, a RelayRouter is shared as given. Fixed at server start.
    """
    return _Server(port, token, resolve_model(default_model), shared_router(relays), host=_check_host(host))


def _check_host(host):
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        raise ValueError(f'--host must be an IP address, not {host!r}') from None


def _is_loopback(host):
    return ipaddress.ip_address(host).is_loopback


# ── command line ─────────────────────────────────────────────────────────

_make_stop_event = threading.Event


def _parser():
    parser = argparse.ArgumentParser(prog='bleo-api', description='bleo OpenAI-compatible API server')
    parser.add_argument('--host', default=os.environ.get('BLEO_API_HOST') or HOST,
                        help=f'listen address (default {HOST}, or BLEO_API_HOST); anything else is '
                             'reachable by other machines, e.g. 0.0.0.0 inside a container')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'listen port (default {DEFAULT_PORT})')
    parser.add_argument('--model', help='default model when a request names none')
    parser.add_argument('--tunnel', choices=('cloudflare', 'tailscale'), help='publish the API through a tunnel')
    parser.add_argument('--tunnel-name', help='named Cloudflare tunnel to run (streaming works)')
    parser.add_argument('--quick', action='store_true', help='Cloudflare quick tunnel (no streaming)')
    parser.add_argument('--tailscale-port', type=int, default=tunnel.DEFAULT_TAILSCALE_PORT,
                        help=f'Tailscale HTTPS port (default {tunnel.DEFAULT_TAILSCALE_PORT})')
    commands = parser.add_subparsers(dest='command')
    show = commands.add_parser('token', help='show where the API token lives')
    show.add_argument('--show', action='store_true', help='print the token (terminal only)')
    return parser


def _validate(parser, args):
    if not 0 <= args.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    if args.model and resolve_model(args.model) not in list_models():
        parser.error(f'unknown model: {args.model}')
    if args.tunnel != 'cloudflare' and (args.tunnel_name or args.quick):
        parser.error('--tunnel-name and --quick need --tunnel cloudflare')
    if args.tunnel != 'tailscale' and args.tailscale_port != tunnel.DEFAULT_TAILSCALE_PORT:
        parser.error('--tailscale-port needs --tunnel tailscale')


def _token_command(args):
    try:
        token = load_token()
    except (BleoError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    if not args.show:
        print(f'API token: {_token_source()}')
        return 0
    if not sys.stdout.isatty():
        print('error: refusing to print the token to a non-terminal; run this in a terminal '
              'or read the token file directly', file=sys.stderr)
        return 1
    print(token)
    return 0


def _start_tunnel(args, port):
    handle = tunnel.Tunnel(args.tunnel, port, name=args.tunnel_name, quick=args.quick,
                           https_port=args.tailscale_port,
                           log=lambda message: print(message, file=sys.stderr, flush=True))
    try:
        handle.start()
    except BaseException:
        handle.stop()
        raise
    if handle.url:
        print(f'public URL: {handle.url}', flush=True)
    else:
        print(f'named tunnel {args.tunnel_name} connected; public URL: the hostname routed to it '
              f'(cloudflared tunnel route dns {args.tunnel_name} <hostname>)', flush=True)
    return handle


def _serve(args):
    server = create_server(args.port, token=load_token(), default_model=args.model, host=args.host)
    host, port = server.server_address[:2]
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    print(f'bleo-api listening on http://{host}:{port}', flush=True)
    if not _is_loopback(host):
        print(f'warning: listening on {host}, not loopback: other machines can reach the API '
              '(the token is still required)', file=sys.stderr, flush=True)
    print(f'API token {_token_source()}', flush=True)
    stop = _make_stop_event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, lambda *_: stop.set())
    handle = None
    try:
        if args.tunnel:
            handle = _start_tunnel(args, port)
        stop.wait()
    finally:
        if handle is not None:
            handle.stop()
        server.shutdown()
        server.server_close()
        for number, old in previous.items():
            signal.signal(number, old)
    return 0


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    _validate(parser, args)
    if args.command == 'token':
        return _token_command(args)
    try:
        return _serve(args)
    except (BleoError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
