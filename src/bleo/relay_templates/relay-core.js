// Shared relay core for bleo's deployable relays (Cloudflare Workers, Deno Deploy, Vercel Edge).
// This file is the source of truth: each platform wrapper inlines it verbatim and the test
// suite fails if a copy drifts. Plain JavaScript, no imports, no exports, no secret in code.
// The platform wrapper passes the secret (read from BLEO_RELAY_SECRET) into handleRelay.

// Upstreams bleo talks to. Anything else would make this relay an open proxy.
const ALLOWED_TARGETS = ["https://ai-chat.bsg.brave.com", "https://ai-chat-premium.bsg.brave.com"];
const MAX_BODY_BYTES = 32 * 1024 * 1024;
const RELAY_AUTH_HEADER = "x-relay-auth";
// x-relay-auth is read by handleRelay, so it is stripped explicitly instead of living here.
const DENY_HEADERS = ["host", "connection", "content-length", "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization", "transfer-encoding", "te", "trailer", "upgrade", "x-relay-target", "x-relay-path"];

const resolveRelayTarget = function(target, relayPath) {
  let targetUrl;
  try { targetUrl = new URL(target); } catch { return { ok: false, status: 400, reason: "invalid x-relay-target" }; }
  if (typeof relayPath !== "string" || relayPath.indexOf("@") !== -1 || relayPath.indexOf("\\") !== -1 || relayPath.charAt(0) !== "/") {
    return { ok: false, status: 403, reason: "forbidden x-relay-path" };
  }
  let finalUrl;
  try { finalUrl = new URL(relayPath, targetUrl); } catch { return { ok: false, status: 403, reason: "forbidden x-relay-path" }; }
  if (finalUrl.hostname !== targetUrl.hostname || finalUrl.protocol !== targetUrl.protocol || finalUrl.port !== targetUrl.port || finalUrl.username || finalUrl.password) {
    return { ok: false, status: 403, reason: "forbidden x-relay-path (host mismatch)" };
  }
  return { ok: true, url: finalUrl.toString() };
};

const isPrivateHostname = function(h) {
  if (!h) return true
  let host = String(h).trim().toLowerCase().replace(/^\[|\]$/g, "")
  if (host.length > 1 && host.endsWith(".")) host = host.slice(0, -1)
  if (!host) return true
  // Numeric IP literals outside dotted-decimal (decimal 2130706433, short 127.1,
  // hex 0x7f.0.0.1, octal 0177.0.0.1) skip the dotted-quad check below but
  // inet_aton still routes them. Normalize to dotted-decimal so the range
  // checks see the real address. DNS names fall through untouched.
  const segs = host.split(".");
  const numVal = function(p) {
    if (/^0x[0-9a-f]+$/i.test(p)) return parseInt(p, 16);
    if (/^0[0-7]+$/.test(p)) return parseInt(p, 8);
    if (/^[0-9]+$/.test(p)) return parseInt(p, 10);
    return NaN;
  };
  const allNumeric = segs.length >= 1 && segs.length <= 4 && segs.every(function(p) { return p.length > 0 && !isNaN(numVal(p)); });
  const canonicalV4 = segs.length === 4 && segs.every(function(p) { return /^[0-9]+$/.test(p) && (p.length === 1 || p.charAt(0) !== "0") && Number(p) <= 255; });
  if (allNumeric && !canonicalV4) {
    const vals = segs.map(numVal);
    let n = NaN;
    if (segs.length === 1 && vals[0] <= 4294967295) n = vals[0];
    else if (segs.length === 2 && vals[0] <= 255 && vals[1] <= 16777215) n = vals[0] * 16777216 + vals[1];
    else if (segs.length === 3 && vals[0] <= 255 && vals[1] <= 255 && vals[2] <= 65535) n = (vals[0] * 256 + vals[1]) * 65536 + vals[2];
    else if (vals.every(function(v) { return v <= 255; })) n = ((vals[0] * 256 + vals[1]) * 256 + vals[2]) * 256 + vals[3];
    if (!isNaN(n)) host = ((n >>> 24) & 255) + "." + ((n >>> 16) & 255) + "." + ((n >>> 8) & 255) + "." + (n & 255);
  }
  if (host === "localhost" || host === "0.0.0.0" || host === "127.0.0.1" || host.endsWith(".localhost") || host.endsWith(".local") || host.endsWith(".internal")) return true
  if (host.startsWith("::")) return true
  const v4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/)
  if (v4) {
    const a = Number(v4[1])
    const b = Number(v4[2])
    if (a === 0 || a === 10 || a === 127) return true
    if (a === 169 && b === 254) return true
    if (a === 192 && b === 168) return true
    if (a === 172 && b >= 16 && b <= 31) return true
    if (a === 100 && b >= 64 && b <= 127) return true
    return false
  }
  if (host.includes(":")) {
    if (host.startsWith("fc") || host.startsWith("fd")) return true
    if (/^fe[89ab]/.test(host)) return true
    return false
  }
  return false
};

// Constant-time string comparison (no timingSafeEqual in every edge runtime).
const timingSafeEqualStr = function(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  let diff = a.length ^ b.length;
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i++) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
};

const jsonResponse = function(status, body) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
};

// options is for tests only: wrappers never pass it.
//   options.allowlist            replaces ALLOWED_TARGETS (a local stand-in upstream)
//   options.allowPrivateTargets  skips the private/loopback host guard (a local stand-in is loopback)
async function handleRelay(request, secret, options) {
  const allowlist = (options && options.allowlist) || ALLOWED_TARGETS;
  const allowPrivateTargets = Boolean(options && options.allowPrivateTargets);
  if (typeof secret !== "string" || secret.length === 0) {
    return jsonResponse(500, { error: "relay secret not configured" });
  }
  if (!["GET", "POST", "HEAD", "OPTIONS"].includes(request.method)) return new Response("Method Not Allowed", { status: 405 });
  if (Number(request.headers.get("content-length") || 0) > MAX_BODY_BYTES) return new Response("Payload Too Large", { status: 413 });
  const target = request.headers.get("x-relay-target");
  if (!target) return jsonResponse(400, { error: "Missing x-relay-target header" });
  if (!timingSafeEqualStr(request.headers.get(RELAY_AUTH_HEADER) || "", secret)) {
    return jsonResponse(401, { error: "unauthorized" });
  }
  let targetUrl;
  try { targetUrl = new URL(target); } catch { return jsonResponse(400, { error: "invalid x-relay-target" }); }
  if (targetUrl.protocol !== "http:" && targetUrl.protocol !== "https:") return jsonResponse(403, { error: "forbidden x-relay-target protocol" });
  if (targetUrl.username || targetUrl.password) return jsonResponse(403, { error: "forbidden x-relay-target (embedded credentials)" });
  if (!allowPrivateTargets && isPrivateHostname(targetUrl.hostname)) return jsonResponse(403, { error: "forbidden x-relay-target (private/loopback host)" });
  const cleanTarget = target.replace(/\/$/, "");
  if (!allowlist.includes(cleanTarget)) return jsonResponse(403, { error: "Forbidden target" });
  const relayPath = request.headers.get("x-relay-path") || "/";
  const resolved = resolveRelayTarget(target, relayPath);
  if (!resolved.ok) return jsonResponse(resolved.status, { error: resolved.reason });
  const headers = new Headers(request.headers);
  DENY_HEADERS.forEach((h) => headers.delete(h));
  headers.delete(RELAY_AUTH_HEADER);
  try {
    // Never follow redirects: a 3xx goes back to the caller, so credentials never leave the allowlist.
    const init = { method: request.method, headers, signal: request.signal, redirect: "manual" };
    if (request.method !== "GET" && request.method !== "HEAD") { init.body = request.body; init.duplex = "half"; }
    const response = await fetch(resolved.url, init);
    return new Response(response.body, { status: response.status, headers: response.headers });
  } catch (error) {
    return jsonResponse(502, { error: String(error) });
  }
}
