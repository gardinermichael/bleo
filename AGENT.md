# Repository map

- `src/bleo/client.py`: signed chat-completions requests, response parsing, retries and history.
- `src/bleo/_http.py`: standard-library HTTP transport and compressed streaming responses.
- `src/bleo/models.py`: model aliases, tier filtering and explicit live refresh.
- `src/bleo/models.json`: normalized Brave remote catalog snapshot; source and date are documented in README.md.
- `src/bleo/cli.py`: one-shot and interactive CLI.
- `src/bleo/ui.py`: shared banner, prompt, table, picker, line-editor and spinner helpers.
- `src/bleo/relay_state.py`: relay pool in `relays.json` (0600), in-memory health book (cooldowns, escalation, EWMA) and candidate ordering.
- `src/bleo/relay.py`: relay router; opens one response per request, rolls past failing relays, hedges slow starts and falls back to direct.
- `src/bleo/relay_templates/`: Cloudflare, Deno and Vercel relay sources; `relay-core.js` is inlined into each wrapper.
- `src/bleo/api.py`: `bleo-api` OpenAI-compatible server on 127.0.0.1; token handling and request validation.
- `src/bleo/tunnel.py`: Cloudflare and Tailscale tunnels that publish the `bleo-api` port.
- `tests/`: offline unittest suite; transport tests start a local HTTP server.

Run `python -m pip install --no-deps -e .` followed by `python -m unittest discover -s tests -v`.

Keep GPT-6 Luna (`bedrock-openai.gpt-5.6-luna`) as the API and CLI default. Preserve the Claude Haiku compatibility ID. Preserve the matching signing key ID and PSK. A requested model can differ from the model reported by Brave; expose the latter through `last_model`. Do not commit live credentials, generated responses or local environments.
