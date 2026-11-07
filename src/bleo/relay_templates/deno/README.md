# bleo relay on Deno Deploy

Forwards bleo's requests to Brave Leo's two hosts (`ai-chat.bsg.brave.com` and
`ai-chat-premium.bsg.brave.com`) and nothing else. The secret is not in the code.

Deno Deploy Classic and `deployctl` were shut down on 2026-07-20. Use the current `deno deploy`
CLI from a Deno version that ships it (run `deno deploy --help` to confirm the options on your
install), or the Deno Deploy dashboard.

## Deploy

1. Create an app in the Deno Deploy dashboard, or let `deno deploy` create one, and deploy
   `main.ts` from this folder.
2. In the app's settings, add an environment variable `BLEO_RELAY_SECRET`. Its value is a long
   random string you choose, for example
   `python3 -c "import secrets; print(secrets.token_hex(32))"`. Keep it; bleo needs it too.
3. Redeploy if the dashboard asks, so the variable is live.

Until the variable is set, every request gets `500 {"error":"relay secret not configured"}`.

## Caveat: User-Agent

Upstream pi-freeflow observed Deno rewriting the `User-Agent` header on outbound requests.
bleo sends its own `User-Agent`, and Brave may reject requests whose agent was changed. Check
this before you rely on the relay: run the probe below. If it fails with a 4xx that the
Cloudflare or Vercel template does not produce, use one of those instead.

## Connect bleo

```sh
bleo relay add <deployed-url> --auth <secret> --label <name>
bleo relay probe <name> --no-direct
```
