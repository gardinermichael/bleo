from __future__ import annotations

import base64
import hashlib
import hmac
from http.client import HTTPException
import ipaddress
import json
import os
import re
import socket
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ._http import Session
from .exceptions import APIError, AuthError, BleoError, RatelimitError
from .models import DefaultModel, History, Message, Qwen38, Role, list_models, model_info, resolve_model

_BASE = 'https://ai-chat.bsg.brave.com'
_PREMIUM_BASE = 'https://ai-chat-premium.bsg.brave.com'
_CONVERSATION = '/v1/chat/completions'
_DEFAULT_MODEL = DefaultModel
_KNOWN_X_BRAVE_KEY = 'qztbjzBqJueQZLFkwTTJrieu8Vw3789u'
_KNOWN_PRE_SHARED_KEY = 'a1a5e78387143d981cc13d52445983b7b9884c2a46009122710b675dfe11e280'
_UA = 'bleo/0.2.0'
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_IMAGE_TYPES = {
    'image/jpeg': lambda data: data.startswith(b'\xff\xd8\xff'),
    'image/png': lambda data: data.startswith(b'\x89PNG\r\n\x1a\n'),
    'image/gif': lambda data: data.startswith((b'GIF87a', b'GIF89a')),
    'image/webp': lambda data: data.startswith(b'RIFF') and data[8:12] == b'WEBP',
}


def _validate_image_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Image URL must use public HTTPS')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError('Image host could not be resolved') from None
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('Image URL must point to a public host')


class _ImageRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _image_data_url(image):
    if image.startswith('data:'):
        match = re.fullmatch(r'data:(image/(?:jpeg|png|gif|webp));base64,([A-Za-z0-9+/]*={0,2})', image)
        if not match:
            raise ValueError('Image data must be a supported base64 image')
        mime, encoded = match.groups()
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ValueError('Image data must be valid base64') from None
    else:
        if not isinstance(image, str):
            raise ValueError('Image must be an HTTPS URL or image data URL')
        _validate_image_url(image)
        request = Request(image, headers={'User-Agent': _UA, 'Accept': 'image/avif,image/webp,image/png,image/jpeg,image/gif'})
        with build_opener(_ImageRedirect()).open(request, timeout=30) as response:
            mime = response.headers.get_content_type().lower()
            data = response.read(_MAX_IMAGE_BYTES + 1)
    if len(data) > _MAX_IMAGE_BYTES:
        raise ValueError('Image must be 20 MB or smaller')
    if mime == 'application/octet-stream':
        mime = next((kind for kind, matches in _IMAGE_TYPES.items() if matches(data)), '')
    if mime not in _IMAGE_TYPES or not _IMAGE_TYPES[mime](data):
        raise ValueError('Image must be a valid JPEG, PNG, GIF, or WebP file')
    return f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')


def _make_digest(body):
    return base64.b64encode(hashlib.sha256(body.encode('utf-8')).digest()).decode()


def _make_hmac_signature(body, psk):
    signed = f'digest: SHA-256={_make_digest(body)}'.encode()
    return base64.b64encode(hmac.new(psk.encode(), signed, hashlib.sha256).digest()).decode()


def _make_key_id():
    # This identifies the bundled signing key, independently of the browser version.
    return 'linux-148-release'


def _content(value):
    if isinstance(value, str):
        return [{'type': 'text', 'text': value}]
    if not isinstance(value, list) or not value:
        raise ValueError('Content must be text or a nonempty list of content blocks')
    blocks = []
    for block in value:
        if isinstance(block, str):
            blocks.append({'type': 'text', 'text': block})
        elif isinstance(block, dict) and isinstance(block.get('type'), str):
            blocks.append(dict(block))
        else:
            raise ValueError('Invalid content block')
    return blocks


class Bleo:
    """Brave Leo client using the current chat-completions protocol and Python stdlib.

    The default requested model is GPT-6 Luna. Premium credentials,
    service keys and signing key IDs can be supplied explicitly or via environment.
    max_retries counts total attempts. Long Retry-After delays are raised to the caller.
    """

    def __init__(self, model=_DEFAULT_MODEL, *, x_brave_key=None, pre_shared_key=None,
                 signing_key_id=None, premium_credential=None, timeout=60.0,
                 max_retries=3, max_retry_delay=60.0, history=False, proxy=None):
        if timeout <= 0 or max_retry_delay < 0:
            raise ValueError('timeout must be positive and max_retry_delay nonnegative')
        self.model = resolve_model(model)
        self.x_brave_key = x_brave_key if x_brave_key is not None else os.environ.get('BRAVE_X_BRAVE_KEY', _KNOWN_X_BRAVE_KEY)
        self.pre_shared_key = pre_shared_key if pre_shared_key is not None else os.environ.get('BRAVE_PRE_SHARED_KEY', _KNOWN_PRE_SHARED_KEY)
        self.signing_key_id = signing_key_id or os.environ.get('BRAVE_SIGNING_KEY_ID') or _make_key_id()
        self.premium_credential = premium_credential or os.environ.get('BRAVE_LEO_PREMIUM_CREDENTIAL')
        self.timeout = timeout
        self.proxy = proxy
        self.max_retries = max(1, int(max_retries))
        self.max_retry_delay = max_retry_delay
        self.history_enabled = bool(history)
        self.history = History(model=self.model)
        self.last_model = None
        self._client = Session(timeout=timeout, proxy=proxy)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._client.close()

    def reset(self):
        self.history.clear()

    def enable_history(self):
        self.history_enabled = True

    def disable_history(self):
        self.history_enabled = False
        self.history.clear()

    def _headers(self, body, *, stream=True):
        headers = {'User-Agent': _UA, 'Accept-Language': 'en-US,en;q=0.9',
                   'Content-Type': 'application/json', 'Accept-Encoding': 'gzip, deflate',
                   'Accept': 'text/event-stream' if stream else 'application/json',
                   'x-brave-key': self.x_brave_key}
        if self.pre_shared_key:
            headers['digest'] = f'SHA-256={_make_digest(body)}'
            signature = _make_hmac_signature(body, self.pre_shared_key)
            headers['Authorization'] = (f'Signature keyId="{self.signing_key_id}",algorithm="hs2019",'
                                        f'headers="digest",signature="{signature}"')
        if self.premium_credential:
            headers['Cookie'] = '__Secure-sku#brave-leo-premium=' + self.premium_credential
        return headers

    def _build_payload(self, messages, *, model=None, stream=True):
        selected = resolve_model(model or self.model)
        info = model_info(selected)
        if info and info['encrypted']:
            raise ValueError('This model requires Brave\'s encrypted NEAR transport; choose list_models()')
        if info and 'chat' not in info['capabilities']:
            raise ValueError('Select a chat model from list_models()')
        return {'messages': [{'role': m['role'], 'content': _content(m['content'])} for m in messages],
                'brave_capability': ['chat'], 'model': selected,
                'stream': stream, 'system_language': 'en_US'}

    def _chat_stream(self, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        base = _PREMIUM_BASE if self.premium_credential else _BASE
        return self._client.stream('POST', base + _CONVERSATION, content=body, headers=self._headers(body))

    @staticmethod
    def _raise_for_status(resp):
        body = resp.text
        if resp.status_code in (401, 403):
            raise AuthError(f'Brave authentication failed (HTTP {resp.status_code}); check service keys and signing_key_id')
        if resp.status_code == 429:
            delay = resp.headers.get('Retry-After')
            if delay is None:
                try:
                    delay = json.loads(body).get('error', {}).get('rate_limit_expires_at')
                except (ValueError, AttributeError):
                    pass
            raise RatelimitError('Brave rate limit reached', retry_after=delay)
        raise APIError(f'Brave HTTP {resp.status_code}', resp.status_code, body)

    @staticmethod
    def _iter_sse(resp):
        data = []
        for line in resp.iter_lines():
            if not line:
                if data:
                    yield '\n'.join(data)
                    data = []
            elif line.startswith('data:'):
                value = line[5:]
                data.append(value[1:] if value.startswith(' ') else value)
        if data:
            yield '\n'.join(data)

    @staticmethod
    def _extract_chunk(obj):
        choices = obj.get('choices', [])
        if not isinstance(choices, list):
            raise APIError('Invalid completion choices', 200)
        if not choices:
            return None
        choice = choices[0]
        if not isinstance(choice, dict):
            raise APIError('Invalid completion choice', 200)
        finish = choice.get('finish_reason')
        if finish not in (None, 'stop'):
            raise APIError(f'Completion did not finish normally: {finish}', 200)
        message = choice.get('delta', choice.get('message', {}))
        if not isinstance(message, dict):
            raise APIError('Invalid completion message', 200)
        content = message.get('content')
        if content is None:
            return None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return ''.join(part.get('text', '') for part in content if isinstance(part, dict) and isinstance(part.get('text', ''), str))
        raise APIError('Invalid completion content', 200)

    def _attempt_stream(self, payload):
        with self._chat_stream(payload) as resp:
            if resp.status_code != 200:
                self._raise_for_status(resp)
            saw_text = False
            is_json = resp.headers.get('Content-Type', '').split(';')[0] == 'application/json'
            events = [resp.text] if is_json else self._iter_sse(resp)
            for data in events:
                if data == '[DONE]':
                    if not saw_text:
                        raise APIError('Empty completion')
                    return
                try:
                    obj = json.loads(data)
                except ValueError:
                    raise APIError('Invalid JSON in completion stream') from None
                if not isinstance(obj, dict):
                    raise APIError('Invalid completion object')
                if obj.get('error'):
                    error = obj['error']
                    code = str(error.get('type', '')) if isinstance(error, dict) else ''
                    if code.startswith('429'):
                        raise RatelimitError('Brave rate limit reached')
                    raise APIError('Brave returned a completion error', body=data)
                if isinstance(obj.get('model'), str):
                    self.last_model = obj['model'].strip() or None
                chunk = self._extract_chunk(obj)
                if chunk:
                    saw_text = True
                    yield chunk
                choices = obj.get('choices', [])
                if choices and choices[0].get('finish_reason') == 'stop':
                    # Some service responses append continuation turns after completion.
                    if not saw_text:
                        raise APIError('Empty completion')
                    return
            if not saw_text:
                raise APIError('Empty completion')
            raise APIError('Completion stream ended before completion')

    def _stream_with_retry(self, payload):
        for attempt in range(self.max_retries):
            yielded = False
            try:
                for chunk in self._attempt_stream(payload):
                    yielded = True
                    yield chunk
                return
            except (APIError, RatelimitError, OSError, HTTPException) as error:
                retryable = not isinstance(error, APIError) or error.status_code is None or error.status_code >= 500
                if yielded or not retryable or attempt + 1 == self.max_retries:
                    raise
                delay = 1.5 * 2 ** attempt
                if isinstance(error, RatelimitError):
                    delay = max(delay, 60.0 if error.retry_after is None else error.retry_after)
                    if delay > self.max_retry_delay:
                        raise
                time.sleep(delay)
        raise BleoError('No completion attempts were made')

    def stream(self, prompt, *, remember=None, model=None):
        if isinstance(prompt, str) and not prompt.strip():
            raise ValueError('Prompt must not be empty')
        remember = self.history_enabled if remember is None else remember
        messages = self.history.to_messages() if remember else []
        messages.append(Message(role=Role.User, content=prompt).to_dict())
        payload = self._build_payload(messages, model=model)
        self.last_model = None
        collected = []
        for chunk in self._stream_with_retry(payload):
            collected.append(chunk)
            yield chunk
        # Commit a turn only when generation completes, including when consumed via stream().
        if remember:
            self.history.add_user(prompt)
            self.history.add_assistant(''.join(collected))

    def ask(self, prompt, *, remember=None, model=None):
        return ''.join(self.stream(prompt, remember=remember, model=model))

    def ask_with_image(self, prompt, image, *, remember=None, model=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('Prompt must not be empty')
        if not isinstance(image, str):
            raise ValueError('Image must be an HTTPS URL or image data URL')
        selected = resolve_model(model or self.model)
        info = model_info(selected)
        if not info or 'vision' not in info['capabilities']:
            selected = Qwen38 if model_info(Qwen38) and 'vision' in model_info(Qwen38)['capabilities'] else next(
                (name for name in list_models()
                 if (info := model_info(name)) and 'vision' in info['capabilities']), None)
        if not selected:
            raise ValueError('No vision-capable Brave model is available')
        content = [prompt, {'type': 'image_url', 'image_url': {'url': _image_data_url(image)}}]
        return self.ask(content, remember=remember, model=selected)

    def ask_stream(self, prompt, *, remember=None, model=None):
        return self.stream(prompt, remember=remember, model=model)
