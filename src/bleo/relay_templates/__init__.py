"""Deployable relay templates for Cloudflare Workers, Deno Deploy and Vercel Edge.

Each platform folder holds files a user deploys by hand. Every JavaScript or TypeScript
wrapper inlines relay-core.js verbatim, so each one is a single deployable file. The relay
forwards only to Brave Leo's two hosts and reads its shared secret from BLEO_RELAY_SECRET
at runtime. No secret is written into any file.
"""

import errno
import importlib.resources
import pathlib
import secrets

PLATFORMS = ("cloudflare", "deno", "vercel")

# Files per platform, relative to that platform's folder in this package. They are written
# flat-relative into out_dir, so the output folder is the deployable unit.
_FILES = {
    "cloudflare": ("worker.js", "wrangler.toml", "README.md"),
    "deno": ("main.ts", "README.md"),
    "vercel": ("api/relay.js", "vercel.json", "package.json", "README.md"),
}


def new_secret() -> str:
    """Return a fresh shared secret for BLEO_RELAY_SECRET (64 hex characters)."""
    return secrets.token_hex(32)


def write_template(platform: str, out_dir) -> list[pathlib.Path]:
    """Write one platform's deployable files into out_dir and return the paths written.

    Refuses to overwrite: if any target file already exists, raises FileExistsError before
    writing anything.
    """
    if platform not in _FILES:
        raise ValueError(f"unknown relay platform {platform!r}; expected one of {PLATFORMS}")
    root = pathlib.Path(out_dir)
    targets = [(name, root / name) for name in _FILES[platform]]
    for _, target in targets:
        if target.exists() or target.is_symlink():
            raise FileExistsError(errno.EEXIST, "refusing to overwrite existing file", str(target))
    source_root = importlib.resources.files(__name__) / platform
    written = []
    for name, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source_root / name).read_bytes())
        written.append(target)
    return written
