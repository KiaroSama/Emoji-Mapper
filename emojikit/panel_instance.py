"""Identify and reopen an existing loopback curation session without replacing it."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import webbrowser
from pathlib import Path

APP = "emoji-mapper-panel"
PROBE_TIMEOUT = 1
MAX_REPLY = 4 * 1024 * 1024
log = logging.getLogger("panel")


def session_identity(db_path: Path, show_all: bool, packs) -> dict:
    # Compare canonical collections without exposing local paths or credentials.
    canonical = os.path.normcase(str(db_path.resolve()))
    return {"application": APP,
            "catalog": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "all": bool(show_all), "packs": sorted(set(packs or []))}


def _get(port: int, path: str) -> tuple[int, bytes]:
    # Direct HTTP avoids system proxies and redirects; only loopback is probed.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=PROBE_TIMEOUT)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read(MAX_REPLY + 1)
        if len(body) > MAX_REPLY:
            return 0, b""
        return response.status, body
    except (OSError, http.client.HTTPException):
        return 0, b""
    finally:
        conn.close()


def probe_session(port: int) -> dict | None:
    status, body = _get(port, "/api/session")
    if status == 200:
        try:
            info = json.loads(body)
        except (ValueError, UnicodeError):
            return None
        fields = {"application", "catalog", "all", "packs"}
        return info if (isinstance(info, dict) and set(info) == fields
                        and info.get("application") == APP) else None
    if status != 404:
        return None

    # Older panels have no session endpoint. Recognize their public page, but
    # do not pretend its collection/view identity has been verified.
    status, body = _get(port, "/api/ping")
    if status != 200 or body.strip() != b'{"ok":true}':
        return None
    status, body = _get(port, "/")
    markers = ('<title>Emoji Mapper — Curate</title>', 'id="items-data"',
               '/static/panel-grid.js', 'const TOKEN = ')
    if status == 200 and all(marker.encode("utf-8") in body for marker in markers):
        return {"application": APP, "legacy": True}
    return None


def reopen_existing(port: int, expected: dict, *, no_open: bool) -> int | None:
    """Return a launch exit code when a panel exists, otherwise let binding decide."""
    info = probe_session(port)
    if info is None:
        return None
    url = f"http://127.0.0.1:{port}/"
    if info.get("legacy") is not True and info != expected:
        log.error("A panel for another collection or view is already running at %s. "
                  "Open that session or choose another --port.", url)
        return 2
    if info.get("legacy") is True:
        log.warning("Reusing an older panel; its existing collection and view remain active.")
    log.info("Panel already running; reopening %s", url)
    print(f"Panel already running: {url}", flush=True)
    if not no_open and not webbrowser.open(url):
        log.warning("The browser did not open automatically. Open %s", url)
    return 0
