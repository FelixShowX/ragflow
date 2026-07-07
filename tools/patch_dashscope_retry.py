#!/usr/bin/env python3
"""
Patch the DashScope SDK's sync HTTP handler to retry on transient
connection/read errors (SSLEOFError, RemoteDisconnected, ReadTimeout, etc.).

RAGFlow calls DashScope through the Tongyi-Qianwen provider. When the Docker
host routes traffic through a proxy (e.g. Surge -> overseas relay), the TLS
connection to dashscope.aliyuncs.com is intermittently closed by the remote end.
The SDK's default behaviour is to create a fresh requests.Session with no retry
adapter, so a single connection failure aborts the whole embedding request and
the parsing task fails.

This patch mounts an HTTPAdapter with urllib3 Retry(total=5, backoff_factor=1)
onto every temporary HTTPS session created by dashscope's HttpRequest. It is
idempotent and safe to run multiple times.

Usage:
    python tools/patch_dashscope_retry.py
"""

import sys
from pathlib import Path

# Find the dashscope http_request module inside the active venv.
# Prefer the venv that RAGFlow itself uses; fall back to system site-packages.
CANDIDATES = [
    Path("/ragflow/.venv/lib/python3.13/site-packages/dashscope/api_entities/http_request.py"),
]

try:
    import dashscope.api_entities.http_request as _ht
    CANDIDATES.insert(0, Path(_ht.__file__))
except Exception:
    pass

TARGET = next((p for p in CANDIDATES if p.exists()), None)
if TARGET is None:
    print("[patch_dashscope_retry] Could not find dashscope http_request.py; nothing to patch.")
    sys.exit(0)


def patch():
    content = TARGET.read_text(encoding="utf-8")

    # 1. Ensure imports are present.
    imports = """from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
"""
    if "from requests.adapters import HTTPAdapter" not in content:
        marker = "import requests\n"
        if marker in content:
            content = content.replace(marker, marker + imports)
        else:
            print("[patch_dashscope_retry] Warning: could not locate import block.")
            return False

    # 2. Patch _handle_request to mount a retry adapter on temporary sessions.
    old_block = """            else:
                session = requests.Session()
                should_close = True

            try:"""

    new_block = """            else:
                session = requests.Session()
                # Retry transient connection/read errors (SSLEOFError,
                # RemoteDisconnected, ReadTimeout) and HTTP 5xx/429. These are
                # common when traffic to dashscope.aliyuncs.com crosses proxies.
                retries = Retry(
                    total=5,
                    backoff_factor=1,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["POST", "GET"],
                )
                session.mount("https://", HTTPAdapter(max_retries=retries))
                should_close = True

            try:"""

    if old_block in content:
        content = content.replace(old_block, new_block)
    else:
        print("[patch_dashscope_retry] Warning: _handle_request block already patched or changed.")
        return False

    TARGET.write_text(content, encoding="utf-8")
    print(f"[patch_dashscope_retry] Patched {TARGET}")
    return True


if __name__ == "__main__":
    ok = patch()
    sys.exit(0 if ok else 1)
