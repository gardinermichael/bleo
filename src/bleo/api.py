"""OpenAI-compatible HTTP API server for bleo.

Provides ``GET /v1/models``, ``POST /v1/chat/completions`` (streaming and
non-streaming), and ``GET /health`` using only the Python standard library.
Zero third-party dependencies, matching bleo's dependency-free design.

Example::

    bleo-api --gateways "https://ai-chat.bsg.brave.com,https://custom.example.com/v1"

    curl http://127.0.0.1:8317/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d '{"model": "luna", "messages": [{"role": "user", "content": "hello"}]}'
"""

from __future__ import annotations

import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from . import (
    APIError,
    AuthError,
    Bleo,
    BleoError,
    GatewayExhaustedError,
    RatelimitError,
    list_models,
    model_info,
    resolve_model,
)
from .cli import _parse_gateways

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8317
_REQUEST_ID_PREFIX = "chatcmpl-"


def _now() -> int:
    return int(time.time())


def _chat_completion_id() -> str:
    return f"{_REQUEST_ID_PREFIX}{uuid.uuid4().hex[:24]}"


def _error_response(message: str, status_code: int, type_: str = "api_error") -> bytes:
    body = json.dumps(
        {
            "error": {
                "message": message,
                "type": type_,
                "param": None,
                "code": status_code,
            }
        }
    ).encode("utf-8")
    return body


def _models_list() -> list[dict]:
    """Return OpenAI-style model objects from bleo's catalog."""
    out: list[dict] = []
    for name in list_models():
        info = model_info(name) or {}
        out.append(
            {
                "id": name,
                "object": "model",
                "created": _now(),
                "owned_by": "brave",
                "info": info,
            }
        )
    return out


class _Handler(BaseHTTPRequestHandler):
    server_version = "bleo-api/0.2.0"

    def log_message(self, format, *args):  # noqa: A002 - shadows builtin, stdlib signature
        # Suppress default stderr logging; keep it quiet for embedded use.
        return

    # ── helpers ──────────────────────────────────────────────────────────

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: int, message: str, type_: str = "api_error") -> None:
        body = _error_response(message, status, type_)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    # ── routes ───────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 - stdlib interface requires do_GET
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {"status": "ok", "service": "bleo-api"})
            return
        if path in ("/v1/models", "/models"):
            self._send_json(200, {"object": "list", "data": _models_list()})
            return
        self._send_error(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib interface requires do_POST
        path = self.path.split("?", 1)[0]
        if path != "/v1/chat/completions" and path != "/chat/completions":
            self._send_error(404, "Not found")
            return

        try:
            raw = self._read_body()
            if not raw:
                self._send_error(400, "Empty request body")
                return
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._send_error(400, "Invalid JSON body")
            return

        if not isinstance(payload, dict):
            self._send_error(400, "Request body must be a JSON object")
            return

        model = payload.get("model", resolve_model(None))
        messages = payload.get("messages", [])
        stream = bool(payload.get("stream", False))

        # Validate messages
        if not isinstance(messages, list) or not messages:
            self._send_error(400, "'messages' must be a nonempty array")
            return

        # Extract the last user message content as the prompt.
        # bleo's ask()/stream() accept a string prompt or content blocks;
        # conversation history is managed per-client via the history flag.
        user_contents = [
            m.get("content", "") for m in reversed(messages)
            if isinstance(m, dict) and m.get("role") == "user"
        ]
        if not user_contents:
            self._send_error(400, "No user message found in 'messages'")
            return
        prompt = user_contents[0]
        if isinstance(prompt, list):
            # Content blocks (e.g. image + text) — pass through as-is.
            prompt = prompt
        elif not isinstance(prompt, str):
            self._send_error(400, "User message content must be a string or content blocks")
            return

        # Build bleo kwargs from OpenAI-compatible fields
        bleo_kwargs: dict = {}
        for key in ("x_brave_key", "pre_shared_key", "signing_key_id",
                    "premium_credential", "proxy"):
            if key in payload and payload[key] is not None:
                bleo_kwargs[key] = payload[key]

        gateways_raw = payload.get("gateways")
        if gateways_raw:
            bleo_kwargs["gateways"] = _parse_gateways(gateways_raw) if isinstance(gateways_raw, str) else list(gateways_raw)
        for key in ("gateway_cooldown", "gateway_max_cooldown", "gateway_max_failures"):
            if key in payload and payload[key] is not None:
                bleo_kwargs[key] = payload[key]

        try:
            with Bleo(model=model, **bleo_kwargs) as client:
                if stream:
                    self._stream_response(client, prompt, model)
                else:
                    self._nonstream_response(client, prompt, model)
        except GatewayExhaustedError as error:
            self._send_error(503, str(error), "gateway_exhausted")
        except AuthError as error:
            self._send_error(401, str(error), "auth_error")
        except RatelimitError as error:
            self._send_error(429, str(error), "rate_limit_error")
        except APIError as error:
            status = error.status_code or 502
            self._send_error(status, str(error), "api_error")
        except (BleoError, ValueError, OSError) as error:
            self._send_error(502, str(error), "api_error")

    # ── response handlers ───────────────────────────────────────────────

    def _nonstream_response(self, client: Bleo, prompt: str, model: str) -> None:
        try:
            text = client.ask(prompt)
        except Exception:
            # Re-raise so the outer try/except in do_POST handles it.
            raise

        completion_id = _chat_completion_id()
        payload = {
            "id": completion_id,
            "object": "chat.completion",
            "created": _now(),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
        }
        self._send_json(200, payload)

    def _stream_response(self, client: Bleo, prompt: str, model: str) -> None:
        completion_id = _chat_completion_id()

        def sse_event(data: dict) -> str:
            return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"

        # Open SSE stream
        first = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": _now(),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }
            ],
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.write(sse_event(first).encode("utf-8"))

        try:
            for chunk in client.stream(prompt):
                event = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": _now(),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": chunk},
                            "finish_reason": None,
                        }
                    ],
                }
                self.wfile.write(sse_event(event).encode("utf-8"))
        except GeneratorExit:
            # Client disconnected mid-stream; history is intentionally not committed.
            return
        except Exception:
            # Propagate so the outer handler can log / respond if still connected.
            raise

        # Terminal [DONE]


def run(
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    *,
    gateways: Optional[list[str]] = None,
    gateway_cooldown: float = 5.0,
    gateway_max_cooldown: float = 120.0,
    gateway_max_failures: int = 5,
) -> None:
    """Start the blocking HTTP API server.

    Parameters mirror ``Bleo`` constructor args so each request can use the
    same gateway pool configuration.
    """
    server = ThreadingHTTPServer((host, port), _Handler)
    print(f"bleo-api listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="bleo-api", description="bleo OpenAI-compatible API server"
    )
    parser.add_argument("--host", default=_DEFAULT_HOST, help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT, help="Bind port (default: 8317)")
    parser.add_argument("--gateways", help="Comma-separated gateway URLs for rotation/failover")
    parser.add_argument("--gateway-cooldown", type=float, default=5.0,
                        help="Base cooldown seconds after a gateway failure (default: 5)")
    parser.add_argument("--gateway-max-cooldown", type=float, default=120.0,
                        help="Maximum cooldown seconds (default: 120)")
    parser.add_argument("--gateway-max-failures", type=int, default=5,
                        help="Max consecutive failures before a gateway is skipped (default: 5)")
    args = parser.parse_args()

    gw = _parse_gateways(args.gateways) if args.gateways else None
    run(
        host=args.host,
        port=args.port,
        gateways=gw,
        gateway_cooldown=args.gateway_cooldown,
        gateway_max_cooldown=args.gateway_max_cooldown,
        gateway_max_failures=args.gateway_max_failures,
    )


if __name__ == "__main__":
    main()