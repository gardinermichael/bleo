"""Deploy bleo's relay to the user's own Cloudflare, Deno Deploy or Vercel account.

The platform token is used in memory only: it is never written, logged or printed, and every
message built here is scrubbed of it. The relay secret goes to the platform as the
environment variable (Cloudflare: a secret_text binding) BLEO_RELAY_SECRET and is never part
of the uploaded code. If the platform refuses to store it, the deploy stops and cleans up;
there is no fallback that embeds it.
"""

from __future__ import annotations

import getpass
import importlib.resources
import json
import os
import re
import secrets
import sys
import time
from urllib.parse import quote

from . import BleoError
from ._http import Session

PLATFORMS = ("cloudflare", "deno", "vercel")
SECRET_NAME = "BLEO_RELAY_SECRET"
TOKEN_ENV = {"cloudflare": "CLOUDFLARE_API_TOKEN", "deno": "DENO_DEPLOY_TOKEN", "vercel": "VERCEL_TOKEN"}
TOKEN_LABEL = {"cloudflare": "Cloudflare API token", "deno": "Deno Deploy access token",
               "vercel": "Vercel API token"}
API_BASES = {"cloudflare": "https://api.cloudflare.com/client/v4",
             "deno": "https://api.deno.com/v2",
             "vercel": "https://api.vercel.com"}
POLL_INTERVAL = {"deno": 2.0, "vercel": 3.0}
POLL_TIMEOUT = 120.0
HOSTNAME_WAIT = 30.0
REQUEST_TIMEOUT = 60
USER_AGENT = "bleo-relay-deploy"
COMPATIBILITY_DATE = "2024-03-20"
_NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"


class DeployError(BleoError):
    """A deploy step failed. The message never contains the token or the relay secret."""


# -- names, token, templates ---------------------------------------------------------------

def default_name() -> str:
    return "bleo-relay-" + "".join(secrets.choice(_NAME_CHARS) for _ in range(6))


def _base_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.lower())
    return re.sub(r"-{2,}", "-", cleaned).strip("-")


def platform_name(platform: str, name: str) -> str:
    """The name as the platform will accept it ([a-z0-9-]; Cloudflare 58 chars, Deno 3-32)."""
    clean = _base_name(name)
    if platform == "cloudflare":
        clean = clean[:58].rstrip("-")
    elif platform == "deno":
        clean = clean[:32].rstrip("-")
        if clean and len(clean) < 3:
            clean += "-relay"
    elif platform == "vercel":
        clean = clean[:100].rstrip("-")
    if not clean:
        raise ValueError(f"--name {name!r} has no usable characters (use a-z, 0-9 and -)")
    return clean


def _can_prompt() -> bool:
    return sys.stdin.isatty()


def read_token(platform: str, environ=None) -> str:
    """The platform token from its environment variable, else a hidden prompt."""
    environ = os.environ if environ is None else environ
    token = (environ.get(TOKEN_ENV[platform]) or "").strip()
    if not token and not _can_prompt():
        # getpass falls back to echoing input without a terminal; refuse instead.
        raise ValueError(f"no terminal to ask for the {TOKEN_LABEL[platform]}; run this in a terminal "
                         f"or set {TOKEN_ENV[platform]}")
    if not token:
        token = getpass.getpass(f"{TOKEN_LABEL[platform]} (input hidden; kept in memory only): ").strip()
    if not token:
        raise ValueError(f"no {TOKEN_LABEL[platform]} given (or set {TOKEN_ENV[platform]})")
    return token


def _template(platform: str, relpath: str) -> str:
    root = importlib.resources.files("bleo.relay_templates") / platform
    return (root / relpath).read_text(encoding="utf-8")


# -- HTTP ----------------------------------------------------------------------------------

class _Api:
    """JSON calls to one platform's REST API with the token in the Authorization header."""

    def __init__(self, platform, token, secret, base):
        self.platform = platform
        self.token = token
        self.secret = secret
        self.base = base.rstrip("/")
        self.session = Session(timeout=REQUEST_TIMEOUT)

    def close(self):
        self.session.close()

    def scrub(self, text):
        text = str(text)
        for hidden in (self.token, self.secret):
            if hidden:
                text = text.replace(hidden, "[hidden]")
        return text

    def fail(self, action, message=None):
        return DeployError(self.scrub(message or f"Failed to {action}"))

    def request(self, method, path, action, *, data=None, raw=None, content_type=None, ok=None):
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT,
                   "Accept": "application/json"}
        body = None
        if data is not None:
            body, content_type = json.dumps(data).encode(), "application/json"
        elif raw is not None:
            body = raw
        if content_type:
            headers["Content-Type"] = content_type
        try:
            response = self.session.open(method, self.base + path, headers=headers, content=body)
            try:
                text = response.text
            finally:
                response.raw.close()
        except (OSError, ValueError) as error:
            raise self.fail(action, f"Could not reach {self.platform} to {action}: {type(error).__name__}") from None
        status = response.status_code
        if status in (ok or range(200, 300)):
            return _parse(text)
        raise self.fail(action, self._explain(action, status, text))

    def _explain(self, action, status, text):
        detail = _detail(self.platform, text) or f"HTTP {status}"
        name = {"cloudflare": "Cloudflare", "deno": "Deno Deploy", "vercel": "Vercel"}[self.platform]
        if 300 <= status < 400:
            return f"Failed to {action}: {name} answered with a redirect (HTTP {status}), which bleo does not follow"
        if status in (401, 403):
            if re.search(r"quota|limit|exceeded", detail, re.I):
                return f"{name} plan or usage limit hit while trying to {action}: {detail}"
            return (f"{name} authentication failed while trying to {action}: {detail}. "
                    f"Check that your token is valid and has the permissions it needs.")
        return f"Failed to {action} (HTTP {status}): {detail}"

    def attempt(self, method, path, action, **kwargs):
        """A best-effort call (cleanup, optional steps): never raises."""
        try:
            return self.request(method, path, action, **kwargs)
        except DeployError:
            return None


def _parse(text):
    try:
        value = json.loads(text) if text.strip() else {}
    except ValueError:
        return {}
    return value


def _detail(platform, text):
    value = _parse(text)
    message = None
    if isinstance(value, dict):
        if platform == "cloudflare" and isinstance(value.get("errors"), list) and value["errors"]:
            first = value["errors"][0]
            message = first.get("message") if isinstance(first, dict) else None
        elif isinstance(value.get("error"), dict):
            message = value["error"].get("message")
    if not isinstance(message, str) or not message:
        message = text.strip() if platform == "deno" and not isinstance(value, dict) else None
    return (message or "")[:300]


def _field(value, *keys):
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _multipart(parts):
    boundary = "bleo" + secrets.token_hex(16)
    chunks = []
    for name, filename, ctype, content in parts:
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                      f'Content-Type: {ctype}\r\n\r\n'.encode() + content.encode() + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


# -- platforms -----------------------------------------------------------------------------

def _cloudflare(api, name, environ, progress, wait):
    progress("Resolving Cloudflare account")
    accounts = _field(api.request("GET", "/accounts", "resolve Cloudflare account"), "result") or []
    accounts = [a for a in accounts if isinstance(a, dict) and a.get("id")]
    if not accounts:
        raise api.fail("", "No Cloudflare account is accessible with this API token")
    if len(accounts) == 1:
        account = accounts[0]["id"]
    else:
        wanted = (environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
        chosen = [a for a in accounts if a["id"] == wanted]
        if not chosen:
            names = ", ".join(str(a.get("name") or "(unnamed)") for a in accounts)
            reason = "does not match any of them" if wanted else "is not set"
            raise api.fail("", f"This token can see {len(accounts)} Cloudflare accounts ({names}) and "
                               f"CLOUDFLARE_ACCOUNT_ID {reason}. Set it to the account id to use.")
        account = chosen[0]["id"]
    scripts = f"/accounts/{quote(account, safe='')}/workers/scripts/{quote(name, safe='')}"
    progress("Uploading relay worker to Cloudflare")
    metadata = {"main_module": "index.js", "compatibility_date": COMPATIBILITY_DATE,
                "bindings": [{"type": "secret_text", "name": SECRET_NAME, "text": api.secret}]}
    body, ctype = _multipart([("index.js", "index.js", "application/javascript+module", _template("cloudflare", "worker.js")),
                              ("metadata", "metadata.json", "application/json", json.dumps(metadata))])
    api.request("PUT", scripts, "upload Worker to Cloudflare", raw=body, content_type=ctype)
    api.attempt("POST", scripts + "/subdomain", "enable workers.dev routing", data={"enabled": True})
    progress("Reading workers.dev routing")
    try:
        subdomain = _field(api.request("GET", f"/accounts/{quote(account, safe='')}/workers/subdomain",
                                       "retrieve workers.dev subdomain"), "result", "subdomain")
        if not subdomain:
            raise api.fail("", "Worker deployed but workers.dev subdomain is unavailable. Enable a workers.dev "
                               "subdomain for your account in the Cloudflare dashboard.")
    except BaseException:
        api.attempt("DELETE", scripts, "remove the Worker")
        raise
    return f"https://{name}.{subdomain}.workers.dev"


def _vercel(api, name, environ, progress, wait):
    progress("Creating Vercel project")
    project = api.request("POST", "/v11/projects", f'create Vercel project "{name}"',
                          data={"name": name, "framework": None})
    project_id = _field(project, "id")
    if not project_id:
        raise api.fail("", "Vercel did not return a project id")
    pid = quote(project_id, safe="")
    deployment = None

    def undo():
        if deployment:
            api.attempt("DELETE", f"/v13/deployments/{quote(deployment, safe='')}", "remove the deployment")
        api.attempt("DELETE", f"/v9/projects/{pid}", "remove the project")

    try:
        progress("Storing the relay secret as a Vercel environment variable")
        api.request("POST", f"/v10/projects/{pid}/env", f"set {SECRET_NAME} on the Vercel project",
                    data={"key": SECRET_NAME, "value": api.secret, "type": "encrypted", "target": ["production"]})
        progress("Uploading relay files to Vercel")
        files = [{"file": path, "data": _template("vercel", path)}
                 for path in ("api/relay.js", "package.json", "vercel.json")]
        created = api.request("POST", "/v13/deployments", "create Vercel deployment",
                              data={"name": name, "project": project_id, "files": files,
                                    "projectSettings": {"framework": None}, "target": "production"})
        deployment = _field(created, "id") or _field(created, "uid")
        if not deployment:
            raise api.fail("", "Vercel did not return a deployment id")
        api.attempt("PATCH", f"/v9/projects/{pid}", "make the deployment public", data={"ssoProtection": None})
        progress("Waiting for Edge deployment to go live")
        return wait("vercel", lambda: _vercel_ready(api, deployment))
    except BaseException:
        undo()
        raise


def _vercel_ready(api, deployment):
    info = api.request("GET", f"/v13/deployments/{quote(deployment, safe='')}", "poll Vercel deployment status")
    state = info.get("readyState") if isinstance(info, dict) else None
    if state == "READY" and info.get("url"):
        return f"https://{info['url']}"
    if state in ("ERROR", "CANCELED"):
        raise api.fail("", f"Deployment failed with state: {state}")
    return None


def _deno(api, name, environ, progress, wait):
    progress("Creating Deno Deploy app")
    try:
        app = api.request("POST", "/apps", f'create Deno Deploy app "{name}"',
                          data={"slug": name, "labels": {"custom.kind": "relay"},
                                "config": {"install": "deno install",
                                           "runtime": {"type": "dynamic", "entrypoint": "main.ts"}}})
    except DeployError as error:
        if "HTTP 409" in str(error):
            raise api.fail("", f'An app named "{name}" already exists on Deno Deploy; choose a different --name.') from None
        raise
    app_id = _field(app, "id")
    if not app_id:
        raise api.fail("", "Deno Deploy did not return an app id")
    aid = quote(app_id, safe="")
    try:
        progress("Storing the relay secret as a Deno Deploy environment variable")
        api.request("PATCH", f"/apps/{aid}", f"set {SECRET_NAME} on the Deno Deploy app",
                    data={"env_vars": [{"key": SECRET_NAME, "value": api.secret, "secret": True}]})
        progress("Uploading relay script to Deno Deploy")
        revision = api.request("POST", f"/apps/{aid}/deploy", "upload relay script",
                               data={"assets": {"main.ts": {"kind": "file", "encoding": "utf-8",
                                                            "content": _template("deno", "main.ts")}}})
        revision_id = _field(revision, "id")
        if not revision_id:
            raise api.fail("", "Deno Deploy did not return a revision id")
        progress("Waiting for Deno Deploy build to finish")
        info = wait("deno", lambda: _deno_revision(api, revision_id))
        seen = {"revision": info}
        host = _deno_host(info)
        if not host:
            # A revision can report "succeeded" before a hostname is routed to it.
            progress("Waiting for Deno Deploy to route a hostname")
            host = _deno_wait_for_host(api, revision_id, wait, seen)
        if not host:
            host = _deno_managed_domain(api)
            host = f"{name}.{host}" if host else None
        if not host:
            raise api.fail("", f'Deployed but could not determine the public URL for "{name}" '
                               f"(Deno returned {_deno_summary(seen)}). "
                               f"Check the app's domain in the Deno Deploy dashboard.")
        return f"https://{host}"
    except BaseException:
        api.attempt("DELETE", f"/apps/{aid}", "remove the app")
        raise


def _deno_revision(api, revision_id):
    info = api.attempt("GET", f"/revisions/{quote(revision_id, safe='')}", "poll Deno Deploy revision")
    status = info.get("status") if isinstance(info, dict) else None
    if status == "succeeded":
        return info
    if status in ("failed", "skipped"):
        reason = f" ({info['failure_reason']})" if info.get("failure_reason") else ""
        raise api.fail("", f"Deno Deploy build failed{reason}")
    return None


def _deno_host(revision):
    timelines = [t for t in (revision.get("timelines") or []) if isinstance(t, dict)]
    def production(t):
        return any(isinstance(t.get(k), str) and t[k].strip().lower() == "production" for k in ("name", "context", "slug"))
    chosen = next((t for t in timelines if production(t)), timelines[0] if timelines else None)
    if not chosen:
        return None
    hosts = list(chosen.get("hostnames") or []) + [d.get("domain", "") for d in chosen.get("domains") or []
                                                    if isinstance(d, dict)]
    return next((h for h in hosts if isinstance(h, str) and h), None)


def _deno_wait_for_host(api, revision_id, wait, seen):
    rid = quote(revision_id, safe="")

    def check():
        revision = api.attempt("GET", f"/revisions/{rid}", "poll Deno Deploy revision")
        timelines = api.attempt("GET", f"/revisions/{rid}/timelines", "read Deno Deploy timelines")
        seen["revision"] = revision if isinstance(revision, dict) else seen["revision"]
        seen["timelines"] = timelines if isinstance(timelines, list) else seen.get("timelines")
        return _deno_host(seen["revision"] or {}) or _deno_host({"timelines": seen.get("timelines") or []})

    try:
        return wait("deno", check, timeout=HOSTNAME_WAIT)
    except DeployError:
        return None


def _deno_summary(seen):
    """What Deno reported about routing, without values that could be sensitive."""
    def describe(timelines):
        items = [t for t in timelines or [] if isinstance(t, dict)]
        return ", ".join(f"{t.get('name') or t.get('slug') or '?'}: "
                         f"{len(t.get('hostnames') or []) + len(t.get('domains') or [])} hostname(s)"
                         for t in items) or "none"
    revision = seen.get("revision") or {}
    return (f"status {revision.get('status')!r}; revision timelines: {describe(revision.get('timelines'))}; "
            f"timelines endpoint: {describe(seen.get('timelines'))}")


def _deno_managed_domain(api):
    domains = api.attempt("GET", "/domains", "list Deno Deploy domains")
    for item in domains if isinstance(domains, list) else []:
        host = item.get("domain", "") if isinstance(item, dict) else ""
        if host.endswith(".deno.net"):
            return host.removeprefix("*.")
    return None


_DEPLOYERS = {"cloudflare": _cloudflare, "deno": _deno, "vercel": _vercel}


def deploy(platform, token, name, secret, *, environ=None, progress=None, sleep=time.sleep,
           monotonic=time.monotonic):
    """Upload the relay for platform under name and return its public URL.

    name must already be platform-safe (see platform_name). secret is stored on the platform as
    BLEO_RELAY_SECRET only; it is checked not to be in the uploaded code.
    """
    if platform not in _DEPLOYERS:
        raise ValueError(f"unknown relay platform {platform!r}; expected one of {PLATFORMS}")
    if not token or not secret:
        raise ValueError("a token and a relay secret are required")
    environ = os.environ if environ is None else environ
    progress = progress or (lambda message: None)
    for relpath in {"cloudflare": ("worker.js",), "deno": ("main.ts",),
                    "vercel": ("api/relay.js", "package.json", "vercel.json")}[platform]:
        if secret in _template(platform, relpath):
            raise DeployError("refusing to deploy: the relay secret appears in the relay code")

    def wait(kind, check, timeout=None):
        timeout = POLL_TIMEOUT if timeout is None else timeout
        deadline = monotonic() + timeout
        while True:
            result = check()
            if result:
                return result
            if monotonic() >= deadline:
                raise DeployError(f"Deployment timed out ({timeout:g}s)")
            sleep(POLL_INTERVAL[kind])

    api = _Api(platform, token, secret, API_BASES[platform])
    try:
        return _DEPLOYERS[platform](api, name, environ, progress, wait)
    finally:
        api.close()
