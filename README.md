# bleo

<p align="center">
  <img src="logo.png" alt="Bleo logo" width="220">
</p>

Talk to Brave Leo from Python or the shell. Requires Python 3.10 or later and has no runtime dependencies. GPT-6 Luna (`bedrock-openai.gpt-5.6-luna`) is the default model.

```sh
pip install git+https://github.com/Ninnix/bleo.git
bleo "tell me a joke"          # GPT-6 Luna by default
bleo -p "tell me a joke"
bleo -m haiku "hello"          # Claude Haiku compatibility model
bleo --no-stream "write code"
bleo --models --access free
bleo --models-all
bleo --list-models --access free
bleo --refresh-models
bleo
```

The prompt has a red `🦁` banner, coloured input, streamed answers, and a model picker. Set `NO_COLOR=1` or pass `--no-color` to disable colour.

Use `/models` to pick a free model, or `/models-all` to include premium models. `/model [N|name]` switches directly. `/status`, `/history [on|off]`, `/image PATH [text]`, `/clear`, `/read FILE`, and `/quit` manage the session. Tab completes commands; arrow keys navigate history. Completed turns stay in the session. `--no-stream` prints the whole reply. A failed one-shot request exits nonzero; an interactive error leaves the prompt open.

## Python API

```python
from bleo import Bleo

with Bleo(history=True) as client:
    print(client.ask("My name is Alice."))
    for chunk in client.stream("What is my name?"):
        print(chunk, end="", flush=True)
    print()
    print("Requested:", client.model, "Served:", client.last_model)
```

`ask()`, `stream()`, and `ask_stream()` accept `model=` and `remember=` overrides. History is off by default. A user/assistant pair is saved only after a complete reply; failed or cancelled streams leave history intact. `reset()` clears history. `disable_history()` clears it and turns it off. Consume the full stream to complete a turn.

`ask_with_image(prompt, image)` accepts an HTTPS URL or image data URL. Remote images are fetched, size-checked, and inlined for Brave. If the selected model cannot read images, the request uses an available vision model.

## Models

The bundled snapshot was checked against [Brave's live catalog](https://ai-chat.bsg.brave.com/v1/models) on 2026-10-21. IDs are the names sent to the API; they may differ from the displayed model version.

```python
from bleo import list_models, model_info, refresh_models

print(list_models())          # direct chat models
print(list_models("free"))    # models available to basic users
print(list_models("premium")) # shared and premium models
print(model_info("luna"))     # name, maker, access, capabilities, encryption
refresh_models()             # explicit network call; updates this process only
```

Imports and `list_models()` work offline. Invalid refreshes keep the previous catalog. NEAR models need a different encrypted transport; summary-only models cannot chat. Both have metadata, but `list_models()` excludes them and requests reject them before sending.

| Alias or ID | Display name | Access |
| --- | --- | --- |
| `luna` / `bedrock-openai.gpt-5.6-luna` | GPT-6 Luna | Free and premium; default |
| `haiku` / `claude-3-haiku` | Claude Haiku | Compatibility ID |
| `auto` / `automatic` | Automatic routing | Compatibility ID |
| `qwen` / `qwen-14b-instruct` | Qwen 3.6 35B | Free and premium |
| `gpt-oss-20b` | GPT OSS 20B | Free and premium |
| `nemotron` | Nemotron Nano 3 30B | Free and premium |
| `qwen-3-235b` | Qwen 3.8 Flash Next | Free and premium |
| `glm` | GLM 4.7 Flash | Free and premium |
| `maverick` | Llama 4 Maverick | Free and premium |
| `automatic-bravebot` | Automatic bot routing | Free and premium |
| `sonnet` | Claude 5.5 Sonnet | Premium |
| `opus` | Claude Opus 5.5 | Premium |
| `gpt-oss-120b` | GPT OSS 120B | Premium |
| `bedrock-openai.gpt-5.5` | GPT-5.5 (Bedrock) | Premium |
| `terra` / `bedrock-openai.gpt-5.4` | GPT 5.6 Terra | Premium |
| `grok` | Grok 4.7 | Premium |
| `mistral` | Mistral Large | Premium |
| `qwen-3-235b-bedrock` | Qwen 3 235B | Premium |
| `deepseek` | Deepseek V3.2 | Premium |
| `kimi` | Kimi K2.5 | Premium |
| `glm5` | GLM 5.3 | Premium |

GPT-6 Luna is the default. It is vision-capable and available on both free and premium accounts. Claude Haiku remains a compatibility ID: it is absent from the current remote catalog, but [Brave's bundled model definitions](https://github.com/brave/brave-core/blob/master/components/ai_chat/core/browser/constants.h) specify `claude-3-haiku`. The old `claude-haiku` spelling remains an alias for that current wire ID. A live request using the corrected ID succeeds.

Brave controls routing, so the requested model may not serve the reply. `last_model` reports the server's model name, or `None` if Brave omits it. The CLI marks compatibility IDs. Retired constants and explicit IDs remain accepted for saved configurations; Brave may reject or reroute IDs it no longer advertises.

## Transport and credentials

Requests use Brave's [`/v1/chat/completions` protocol](https://github.com/brave/brave-core/blob/master/components/ai_chat/core/browser/engine/conversation_api_client.cc): OpenAI-style messages and SSE deltas. The old `/v1/conversation` endpoint returns a browser-update notice.

The standard library handles HTTP, TLS, cookies, proxies, compression, signing, and streaming. No browser or third-party package is needed. Bundled service keys are fallbacks. Explicit arguments take precedence over environment variables:

| Argument | Environment variable |
| --- | --- |
| `x_brave_key` | `BRAVE_X_BRAVE_KEY` |
| `pre_shared_key` | `BRAVE_PRE_SHARED_KEY` |
| `signing_key_id` | `BRAVE_SIGNING_KEY_ID` |
| `premium_credential` | `BRAVE_LEO_PREMIUM_CREDENTIAL` |

The signing key ID must match the pre-shared key. Changing it to match an installed browser version breaks authentication. Set `pre_shared_key=""` to disable signing. A premium credential selects Brave's premium endpoint. `proxy=` and `--proxy` accept HTTP or HTTPS proxies; standard proxy environment variables also work.

`max_retries=3` means three total attempts. Only transient failures before output are retried; authentication and other HTTP 4xx errors stop immediately. Rate limits honor `Retry-After` and JSON expiry timestamps. Longer waits than `max_retry_delay=60` raise `RatelimitError` with `retry_after` so callers can schedule another attempt. Streams stop at the first completed answer. Partial, empty, malformed, or truncated replies raise errors and are never replayed. Set `max_retries=1` to disable retries.

## Relays

A relay is a small forwarder you deploy yourself, on Cloudflare Workers, Deno Deploy or Vercel Edge. It forwards only to Brave Leo's two hosts (`ai-chat.bsg.brave.com` and `ai-chat-premium.bsg.brave.com`), and only for requests that carry your shared secret. bleo can spread its requests over several relays.

Create the deployable files for one platform. The command prints a new 64-character secret once; copy it.

```sh
bleo relay template cloudflare      # or: deno, vercel (writes ./bleo-relay-<platform>)
```

Then follow that platform's README: [Cloudflare](src/bleo/relay_templates/cloudflare/README.md), [Deno Deploy](src/bleo/relay_templates/deno/README.md) or [Vercel Edge](src/bleo/relay_templates/vercel/README.md). Each one stores the secret as `BLEO_RELAY_SECRET` on the platform. Until that variable is set, the relay answers `500`.

- Cloudflare: `npx wrangler deploy`, then `npx wrangler secret put BLEO_RELAY_SECRET`.
- Vercel: `vercel deploy --prod`, then `vercel env add BLEO_RELAY_SECRET` and redeploy.
- Deno Deploy: add `BLEO_RELAY_SECRET` in the app's settings and redeploy if asked. The Deno README warns that Deno may rewrite the `User-Agent` header, so check the relay with a probe before you rely on it.

Add the relay to bleo with the deployed URL and the same secret, then probe it:

```sh
bleo relay add <deployed-url> --auth <secret> --label home
bleo relay probe home --no-direct
bleo relay list
```

`probe` sends one signed streaming chat through that relay alone, with no direct fallback, and prints the route, HTTP status, seconds to headers, the number of content chunks, the time between the first and last chunk, and whether the UTF-8 reply decoded. It passes only on a 2xx reply served by that relay with at least one content chunk; otherwise it fails with a non-zero exit and makes no direct request. Platform support is confirmed only by a passing probe. If a premium credential is configured, the premium host is probed and reported separately.

Other commands:

- `bleo relay remove <label|url>` removes a relay.
- `bleo relay mode auto|spread|off` sets the mode. `sticky` is accepted and stored as `auto`. `off` sends every request direct.
- `bleo relay list` shows labels, URLs and the sticky relay. It never prints a secret.
- `bleo -v` prints `route: <label or direct>` after each reply.

How requests are routed:

- In `auto`, the sticky relay (the one that last answered successfully) is tried first. Another relay takes its place only after at least three measurements, and only if its average time to headers is more than the larger of 1.5 s and 25% of the sticky relay's average faster.
- In `spread`, healthy relays take turns on a rotating cursor instead of sticking to one.
- A relay is rolled past when it answers 408, 429, 502-504, 520-530, 403, 413 or a redirect, or with a relay-side refusal such as a wrong secret (401 `unauthorized`) or a disabled deployment (402). A connection error also counts as a failure. 413 is skipped without a cooldown.
- After a failure a relay cools down for 30 s by default, 45 s for other 5xx, 60 s for 504, 90 s for 429, and 6 h for a disabled deployment (402). Each consecutive failure multiplies the cooldown, up to four times. A success clears it.
- If the first relay has not answered within 8 s, the next healthy relay is started too, and the first usable answer wins.
- When every relay has failed or is cooling down, the request goes direct to Brave, as bleo did before relays existed. Only `probe --no-direct` runs without this fallback.
- Health is kept in memory for each process. The saved file holds only relays, the mode and the sticky relay.

The pool is stored in `~/.bleo/relays.json` (or under `BLEO_HOME`) with mode 0600. The relay secrets are saved in that file in plain text, so keep the file private.

## OpenAI-compatible API (bleo-api)

`bleo-api` serves bleo through an OpenAI-style chat-completions endpoint, so OpenAI-compatible clients can use it. It listens on `127.0.0.1` only, on port 8317 by default (`--port`).

```sh
bleo-api                             # http://127.0.0.1:8317
bleo-api --port 9000 --model luna    # other port, default model for requests that name none
```

Every route except `GET /health` needs `Authorization: Bearer <token>`. The token is `BLEO_API_TOKEN` if that is set. Otherwise it is read from `~/.bleo/api-token` (under `BLEO_HOME` if set), and created on first use with mode 0600. The server logs only where the token came from, never its value; `token --show` prints it, but only in a terminal.

```sh
bleo-api token              # shows where the token comes from
bleo-api token --show       # prints the token; only works in a terminal
```

To script with the token, read the file rather than capturing `token --show`, which refuses to print to a pipe:

```sh
TOKEN="$(cat ~/.bleo/api-token)"
curl http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"model": "luna", "messages": [{"role": "user", "content": "tell me a joke"}]}'
```

Add `"stream": true` to the body and use `curl -N` to receive server-sent events. Streaming ends with `data: [DONE]`.

From Python, install the `openai` package (it is not a bleo dependency) and point it at the local server:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8317/v1", api_key="<token>")
reply = client.chat.completions.create(
    model="luna",
    messages=[{"role": "user", "content": "hello"}],
)
print(reply.choices[0].message.content)
```

Routes: `GET /health` (no token), `GET /v1/models` and `POST /v1/chat/completions`. The paths without `/v1` also work.

Supported request fields:

- `model`: a model from bleo's catalog, by alias or ID. Without it, the `--model` default or GPT-6 Luna is used.
- `messages`: roles `system`, `user` and `assistant`. Content is a string or a list of `text` parts. `image_url` parts are allowed in user messages only.
- `stream`: boolean.
- `stop`: a string or up to four strings.
- `max_tokens` or `max_completion_tokens`: when both are given, the smaller applies. The limit is approximate, at four characters per token.
- `n`: only `1`.
- `response_format`: only `{"type": "text"}`.

Rejected with `400` and code `unsupported_parameter`: `tools`, `tool_choice`, `functions`, `function_call`, `parallel_tool_calls`, `top_logprobs`, `logprobs` when true, `n` other than 1, a `response_format` other than text, messages with role `tool` or `function` or with `tool_calls` or `function_call`, and content parts other than `text` or `image_url`.

Ignored: any other top-level field, such as `temperature`, `top_p`, `seed` or `user`. It is accepted and has no effect.

## Remote access

`bleo-api` always binds `127.0.0.1`. A tunnel publishes that port, and the public URL is printed when the tunnel starts. The token is still required. Copy it to a client with `bleo-api token --show`, run in a terminal, over a channel you trust.

- Named Cloudflare tunnel, with streaming: `bleo-api --tunnel cloudflare --tunnel-name NAME`. The tunnel must already exist in your `cloudflared` setup, and `cloudflared` must be on `PATH`.
- Cloudflare quick tunnel: `bleo-api --tunnel cloudflare --quick`. It prints a random `*.trycloudflare.com` URL. Quick tunnels refuse streaming with `400` and code `streaming_unsupported`, so send `"stream": false`.
- Tailscale: `bleo-api --tunnel tailscale [--tailscale-port 8443]`. It uses `tailscale serve`, so the API is reachable on your tailnet only. bleo refuses a port that already has a Serve mapping and, at exit, removes only the mapping it created.

`--tunnel-name` and `--quick` need `--tunnel cloudflare`. `--tailscale-port` needs `--tunnel tailscale`.

## Tests

```sh
python -m pip install --no-deps -e .
python -m unittest discover -s tests -v
```

Tests use only the standard library and require no service credentials. They cover signing, current payloads, model metadata and refresh validation, retries and rate limits, SSE framing and truncation, atomic conversation history, CLI failures and cleanup, and a local HTTP server for compression, cookies and transport errors. CI runs the suite on Python 3.10 and 3.13 and verifies empty runtime dependency metadata.

Thanks to [cnbeining](https://www.cnbeining.com/2024/01/reverse-engineering-brave-leos-api-key-from-brave-browser/) for the original signing-key research.
