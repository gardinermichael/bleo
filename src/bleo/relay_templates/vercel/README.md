# bleo relay on Vercel Edge

Forwards bleo's requests to Brave Leo's two hosts (`ai-chat.bsg.brave.com` and
`ai-chat-premium.bsg.brave.com`) and nothing else. The secret is not in the code.

The relay runs as an Edge Function at `api/relay.js`. `vercel.json` rewrites every path to it,
so bleo can use the deployment's root URL.

## Deploy

Run these in this folder, with the Vercel CLI signed in:

```sh
vercel deploy --prod
vercel env add BLEO_RELAY_SECRET
```

`vercel env add` asks for the value and for the environments to use (choose Production). Use a
long random string, for example `python3 -c "import secrets; print(secrets.token_hex(32))"`.
Keep it; bleo needs it too. Then redeploy with `vercel deploy --prod` so the variable is live.

Until the variable is set, every request gets `500 {"error":"relay secret not configured"}`.

## Connect bleo

Use the production URL that `vercel deploy --prod` printed, and the same secret:

```sh
bleo relay add <deployed-url> --auth <secret> --label <name>
bleo relay probe <name> --no-direct
```
