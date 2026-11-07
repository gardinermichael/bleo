import base64
import errno
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPSConnection
from http.cookiejar import Cookie, CookieJar
import select
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zlib

HEADER_TIMEOUT = 300.0
BODY_STALL_TIMEOUT = 120.0
CREDENTIAL_HEADERS = ('authorization', 'proxy-authorization', 'cookie', 'x-relay-auth')


class Cookies(CookieJar):
    def set(self, name, value, domain):
        self.set_cookie(Cookie(0, name, value, None, False, domain, True,
                               domain.startswith('.'), '/', True, True, None,
                               True, None, None, {}, False))


class Response:
    def __init__(self, raw):
        self.raw = raw
        self.status_code = raw.getcode()
        self.headers = raw.headers
        self._body = None

    def chunks(self):
        encoding = self.headers.get('Content-Encoding', '').lower()
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == 'gzip' else zlib.decompressobj() if encoding == 'deflate' else None
        if encoding not in ('', 'identity', 'gzip', 'deflate'):
            raise OSError(f'Unsupported content encoding: {encoding}')
        read = getattr(self.raw, 'read1', self.raw.read)
        while True:
            chunk = read(8192)
            if not chunk:
                break
            try:
                yield decoder.decompress(chunk) if decoder else chunk
            except zlib.error as error:
                raise OSError('Invalid compressed response') from error
        if decoder:
            yield decoder.flush()
            if not decoder.eof:
                raise OSError('Truncated compressed response')

    def read(self):
        if self._body is None:
            self._body = b''.join(self.chunks())
        return self._body

    @property
    def text(self):
        return self.read().decode('utf-8', 'replace')

    def iter_lines(self):
        pending = b''
        for chunk in self.chunks():
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                yield line.rstrip(b'\r').decode('utf-8', 'replace')
        if pending:
            yield pending.decode('utf-8', 'replace')


def has_credentials(headers):
    return any(name.lower() in CREDENTIAL_HEADERS or 'digest' in name.lower() for name in headers)


class RefuseCredentialRedirect(HTTPRedirectHandler):
    """Never replay a request that carries credentials or a body to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method() != 'GET' or has_credentials(name for name, _ in req.header_items()):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Session:
    def __init__(self, *, headers=None, timeout=60, proxy=None):
        self.headers = headers or {}
        self.timeout = timeout
        self.cookies = Cookies()
        handlers = [HTTPCookieProcessor(self.cookies), RefuseCredentialRedirect()]
        if proxy:
            if urlsplit(proxy).scheme not in ('http', 'https'):
                raise ValueError('Use an HTTP or HTTPS proxy')
            handlers.append(ProxyHandler({'http': proxy, 'https': proxy}))
        self.opener = build_opener(*handlers)

    def open(self, method, url, *, headers=None, content=None, timeout=None):
        body = content.encode('utf-8') if isinstance(content, str) else content
        request = Request(url, data=body, headers={**self.headers, **(headers or {})}, method=method)
        try:
            raw = self.opener.open(request, timeout=self.timeout if timeout is None else timeout)
        except HTTPError as error:
            raw = error
        return Response(raw)

    def get(self, url, **kwargs):
        response = self.open('GET', url, **kwargs)
        try:
            response.read()
            return response
        finally:
            response.raw.close()

    @contextmanager
    def stream(self, method, url, **kwargs):
        response = self.open(method, url, **kwargs)
        try:
            yield response
        finally:
            response.raw.close()

    def close(self):
        self.cookies.clear()



class AttemptCancelled(Exception):
    pass


class _AttemptBody:
    """File-like view of the response for Response: reads are tracked and cancel-aware."""

    def __init__(self, attempt, response):
        self._attempt = attempt
        self._response = response
        self.headers = response.headers

    def getcode(self):
        return self._response.status

    def read1(self, size=-1):
        attempt = self._attempt
        attempt._enter()
        try:
            data = self._response.read1(size)
        except Exception as error:
            if attempt._dead():
                raise AttemptCancelled('Attempt cancelled') from error
            raise
        finally:
            attempt._leave()
        if not data and attempt._dead():
            raise AttemptCancelled('Attempt cancelled')
        return data

    read = read1

    def close(self):
        self._attempt.close()


class Attempt:
    """One HTTP request on one connection that any thread can cancel without blocking.

    cancel() only sets a flag and shuts the socket down, which wakes any thread blocked in
    connect, send, or read. The socket is closed by the thread using it, or by cancel()
    itself when no thread is inside the attempt, so a descriptor is never closed under a
    reader. DNS resolution (getaddrinfo) cannot be interrupted; cancel is honoured as soon
    as it returns.
    """

    def __init__(self, url, *, method='POST', headers=None, body=None, proxy=None,
                 header_timeout=HEADER_TIMEOUT, stall_timeout=BODY_STALL_TIMEOUT, context=None):
        target = urlsplit(url)
        if target.scheme not in ('http', 'https') or not target.hostname:
            raise ValueError('Use an HTTP or HTTPS URL')
        self.url = url
        self.method = method
        self._request_headers = dict(headers or {})
        self.body = body.encode('utf-8') if isinstance(body, str) else body
        self.header_timeout = header_timeout
        self.stall_timeout = stall_timeout
        self.status = None
        self.headers = None
        self.elapsed_headers = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._closed = False
        self._expired = False
        self._phase = 'new'
        self._busy = 0
        self._timer = None
        self._wake = None
        self._response = None
        self._body = None
        self._conn = self._build_connection(target, proxy, context)

    @property
    def cancelled(self):
        return self._cancelled

    def _build_connection(self, target, proxy, context):
        https = target.scheme == 'https'
        port = target.port or (443 if https else 80)
        self._path = (target.path or '/') + ('?' + target.query if target.query else '')
        if not proxy:
            host, conn_port, tunnel = target.hostname, port, False
        else:
            parts = urlsplit(proxy)
            if parts.scheme not in ('http', 'https') or not parts.hostname:
                raise ValueError('Use an HTTP or HTTPS proxy')
            host, conn_port, tunnel = parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80), True
            auth = {}
            if parts.username is not None:
                token = f'{unquote(parts.username)}:{unquote(parts.password or "")}'.encode()
                auth = {'Proxy-Authorization': 'Basic ' + base64.b64encode(token).decode()}
            if not https:
                self._path = target._replace(fragment='').geturl()
                self._request_headers = {**auth, **self._request_headers}
        if https:
            conn = HTTPSConnection(host, conn_port, timeout=self.header_timeout, context=context)
        else:
            conn = HTTPConnection(host, conn_port, timeout=self.header_timeout)
        if tunnel and https:
            conn.set_tunnel(target.hostname, port, headers=auth)
        conn._create_connection = self._connect
        return conn

    # Connecting: a polled non-blocking connect so cancel needs no help from the kernel.
    def _connect(self, address, timeout, source_address=None):
        deadline = time.monotonic() + (timeout if timeout else self.header_timeout)
        error = OSError('getaddrinfo returned no addresses')
        for family, kind, proto, _, sockaddr in socket.getaddrinfo(address[0], address[1], 0, socket.SOCK_STREAM):
            self._check_live()
            sock = socket.socket(family, kind, proto)
            try:
                sock.setblocking(False)
                if source_address:
                    sock.bind(source_address)
                code = sock.connect_ex(sockaddr)
                while code in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                    self._check_live()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('timed out connecting')
                    _, writable, _ = select.select([], [sock], [], min(0.02, remaining))
                    if writable:
                        code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        break
                if code:
                    raise OSError(code, 'connect failed')
                sock.settimeout(self.header_timeout)
                shutter = sock.dup()
            except AttemptCancelled:
                sock.close()
                raise
            except OSError as caught:
                sock.close()
                error = caught
                if isinstance(caught, TimeoutError):
                    raise
                continue
            with self._lock:
                if self._cancelled or self._closed:
                    shutter.close()
                    sock.close()
                    raise AttemptCancelled('Attempt cancelled')
                self._wake = shutter
            return sock
        raise error

    def _dead(self):
        return self._cancelled or self._closed

    def _check_live(self):
        if self._dead():
            raise AttemptCancelled('Attempt cancelled')
        if self._expired:
            raise TimeoutError('timed out waiting for response headers')

    def _shutdown_locked(self):
        if self._wake is not None:
            try:
                self._wake.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _enter(self):
        with self._lock:
            if self._dead():
                raise AttemptCancelled('Attempt cancelled')
            self._busy += 1

    def _leave(self):
        with self._lock:
            self._busy -= 1
            idle_dead = self._busy == 0 and self._dead()
        if idle_dead:
            self._release()

    def _release(self):
        with self._lock:
            wake, self._wake = self._wake, None
            timer, self._timer = self._timer, None
        if timer:
            timer.cancel()
        for resource in (self._response, self._conn):
            try:
                if resource is not None:
                    resource.close()
            except Exception:
                pass
        if wake:
            try:
                wake.close()
            except OSError:
                pass

    def _expire(self):
        with self._lock:
            if self._phase != 'headers':
                return
            self._expired = True
            self._shutdown_locked()

    def open(self):
        self._enter()
        started = time.monotonic()
        try:
            with self._lock:
                if self._phase != 'new':
                    raise RuntimeError('Attempt already opened')
                self._phase = 'headers'
                self._timer = threading.Timer(self.header_timeout, self._expire)
                self._timer.daemon = True
                self._timer.start()
            try:
                self._conn.request(self.method, self._path, body=self.body, headers=self._request_headers)
                response = self._conn.getresponse()
            except Exception as error:
                self._check_live()
                raise
            with self._lock:
                self._phase = 'body'
                self._timer.cancel()
                if self._dead():
                    response.close()
                    raise AttemptCancelled('Attempt cancelled')
                if self._expired:
                    response.close()
                    raise TimeoutError('timed out waiting for response headers')
                self._response = response
                if self._conn.sock is not None:
                    self._conn.sock.settimeout(self.stall_timeout)
            self.status = response.status
            self.headers = response.headers
            self.elapsed_headers = time.monotonic() - started
            self._body = Response(_AttemptBody(self, response))
            return self
        except BaseException:
            with self._lock:
                self._closed = True
            raise
        finally:
            self._leave()

    def iter_chunks(self):
        return self._body.chunks()

    def iter_lines(self):
        return self._body.iter_lines()

    def read_all(self):
        return self._body.read()

    def cancel(self):
        with self._lock:
            self._cancelled = True
            self._shutdown_locked()
            idle = self._busy == 0
        if idle:
            self._release()

    def close(self):
        with self._lock:
            self._closed = True
            self._shutdown_locked()
            idle = self._busy == 0
        if idle:
            self._release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
