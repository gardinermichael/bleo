# bleo relay on Cloudflare Workers

Forwards bleo's requests to Brave Leo's two hosts (`ai-chat.bsg.brave.com` and
`ai-chat-premium.bsg.brave.com`) and nothing else. The secret is not in the code: you choose
it and store it as a Worker secret.

## Deploy

Run these in this folder, with a Cloudflare account and `wrangler` available through `npx`:

```sh
npx wrangler deploy
npx wrangler secret put BLEO_RELAY_SECRET
```

`wrangler secret put` asks for the value. Use a long random string, for example
`python3 -c "import secrets; print(secrets.token_hex(32))"`. Keep it; bleo needs it too.

Deploying first and then adding the secret is fine: until the secret is set, every request
gets `500 {"error":"relay secret not configured"}`.

## Connect bleo

Use the URL that `wrangler deploy` printed, and the same secret:

```sh
bleo relay add <deployed-url> --auth <secret> --label <name>
bleo relay probe <name> --no-direct
```
