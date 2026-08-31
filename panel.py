"""Local web panel to review/curate downloaded emoji before publishing.

Opens a dark neon-blue panel in the browser showing every emoji in the catalog
as a large card with a label. All are selected (included) by default; click a
card to toggle it (deselected = excluded from the next publish). Visually
similar emoji are ordered next to each other (greedy nearest-neighbour on the
perceptual hash) so you can deselect look-alikes quickly. "Save" writes the
selection back to the catalog; build_collection then only publishes included
items.

Run:  python panel.py            (serves http://127.0.0.1:9450 and opens it)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from collection_state import (BRAND_LOGO_BOTS, BRAND_LOGO_DEFAULT,
                              PER_SET)
from emojikit.catalog import PHASH_BITS, Catalog
from emojikit.logsetup import record_exit_code, setup_logging
from emojikit.media import (PREVIEW_FPS, lottie_preview_webp,
                            lottie_still_webp)

ROOT = Path(__file__).resolve().parent
ASSET_DIR = ROOT / "assets"
log = logging.getLogger("panel")

_MIME = {".webp": "image/webp", ".png": "image/png", ".gif": "image/gif",
         ".webm": "video/webm", ".tgs": "application/gzip"}
FMT_ORDER = {"static": 0, "video": 1, "animated": 2}
DEFAULT_PORT = 9450   # the panel's home port; panel_sandbox imports it
LOGO_KEY = "__brand_logo__"  # pseudo content_key: preview-only, never saved/counted

MAX_BODY = 4 * 1024 * 1024  # generous for an order list, small enough to bound

# Media is content-addressed (the key IS the content hash), so a served file can
# never change under a key -- immutable caching is safe and stops the browser
# re-fetching every thumbnail while you scroll or re-sort.
_IMMUTABLE = "public, max-age=31536000, immutable"


def _json_for_script(value) -> str:
    """JSON safe to embed in an inert <script type=application/json> block.

    ``</script>`` inside a catalog label would otherwise close the block and
    everything after it becomes markup. U+2028/U+2029 are escaped because they
    are literal line terminators in JS string context.
    """
    return (json.dumps(value)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def order_by_similarity(items: list) -> list:
    """Greedy nearest-neighbour ordering by perceptual hash, grouped by format.

    Items without a perceptual hash (e.g. animated .tgs) keep content order and
    follow the hashed ones within their format group.
    """
    out: list = []
    for fmt in sorted({it.fmt for it in items}, key=lambda f: FMT_ORDER.get(f, 9)):
        group = [it for it in items if it.fmt == fmt]
        hashed = [it for it in group if it.phash is not None]
        plain = [it for it in group if it.phash is None]
        if hashed:
            # The walk stays quadratic on purpose: the greedy nearest-neighbour
            # chain IS the look-alike grouping the panel is for, and every index
            # that would make it sub-quadratic (LSH buckets, BK-tree pruning)
            # changes which near-twin ends up next to which. Only the constant
            # is negotiable, so the distance is inlined rather than called:
            # ``(a ^ b).bit_count()`` is identity.hamming's exact result, and at
            # n=3 600 dropping the per-pair call costs 0.48 s instead of 0.92 s
            # for a byte-identical order. (Against the older string-building
            # hamming it was 3.7 s.)
            # ponytail: O(n^2) scan; revisit only if a catalog grows past ~10k
            # items AND a different grouping is acceptable.
            remaining = hashed[:]
            ordered = [remaining.pop(0)]
            hashes = [it.phash for it in remaining]
            last = ordered[0].phash
            while remaining:
                best, best_d = 0, PHASH_BITS + 1
                for i, h in enumerate(hashes):
                    d = (h ^ last).bit_count()
                    if d < best_d:
                        best, best_d = i, d
                        if d == 0:
                            break   # nothing beats 0, and min() takes the first
                ordered.append(remaining.pop(best))
                last = hashes.pop(best)
            out.extend(ordered)
        out.extend(plain)
    return out


# The collector labels an ingested emoji "premium-id:<id>", and that id is the
# one thing anyone wants off this page. Decided here rather than by a regex
# inside the page's JavaScript, so it can actually be tested.
_PREMIUM_ID = re.compile(r"^premium-id:(\d+)$")


def copy_id_for(label: str) -> str:
    """The id a label offers for copying, or "" when it offers none.

    Anchored on purpose: "xpremium-id:12" and "premium-id:12x" are not ids, and
    a label that merely CONTAINS digits is not one either.
    """
    m = _PREMIUM_ID.match(label or "")
    return m.group(1) if m else ""


def packs_named(data_dir: Path, wanted: set[int]) -> set[str]:
    """Set names for the given pack indices, across every published family.

    Read from the publishers' own state files: the index is theirs, and
    deriving a name from the base plus a number would guess at a convention
    the state file already records exactly.
    """
    names: set[str] = set()
    for state in sorted(data_dir.glob("publish_*.json")):
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.debug("could not read %s: %s", state.name, exc)
            continue
        for rec in data.get("sets") or []:
            if rec.get("index") in wanted and rec.get("name"):
                names.add(rec["name"])
    return names


def build_view(cat: Catalog, bot_username: str = "",
               show_published: bool = False,
               keep_sets: set[str] | None = None) -> tuple[list[dict], dict, int]:
    """The cards to render, and where each one's file lives.

    An emoji already live in a pack is hidden by default: the grid is what the
    NEXT pack gets made of, and `is_published` skips those items at publish
    time however they are ticked here, so showing them only invites pruning
    work that changes nothing.

    This was "hide only a FULL set" for one round, on the theory that a set
    still being filled is still the pack being built. `--new-set` ended that --
    a pack can now be left half-empty on purpose, so "full" stopped meaning
    "finished" and the owner kept meeting an abandoned pack's emoji in the grid
    for the next one. Being published is the property that actually settles it,
    and it needs no state file and no capacity arithmetic.

    Hidden, never deleted -- those rows are what dedup recognises a re-download
    by, what maps a source premium id to ours, and what `sync_order` reads to
    re-sort a live set. ``show_published`` (``panel.py --all``) brings them
    back, which is how you reorder a pack that is already published.

    ``keep_sets`` (``panel.py --with-pack N``) is the narrow version of that:
    it un-hides ONE published set so its emoji can be arranged beside the new
    candidates going into it. `--all` is the wrong tool for that -- it also
    brings back every finished pack, which here is hundreds of cards you cannot
    act on.
    """
    # First time only: seed the manual order with the look-alike-grouped
    # similarity order (a nice starting point). After that, always use the saved
    # position order so the user's drag-drop arrangement is what shows/publishes.
    if cat.get_meta("order_seeded") != "1":
        seeded = order_by_similarity(cat.all_items())
        cat.set_order([it.content_key for it in seeded])
        cat.set_meta("order_seeded", "1")
    items = cat.all_items()  # saved manual/seeded order (by position)
    hidden = 0
    if not show_published:
        live = cat.published_keys()
        if keep_sets:
            # published_set_names() is the right lookup HERE and not in the
            # plain filter above: this asks "which set", where a row with no
            # recorded set name is genuinely unanswerable, so it stays hidden.
            where = cat.published_set_names()
            live = {k for k in live if where.get(k) not in keep_sets}
        keep = [it for it in items if it.content_key not in live]
        hidden = len(items) - len(keep)
        items = keep

    view = []
    by_key: dict[str, Path] = {}

    logo_path = Path(BRAND_LOGO_DEFAULT)
    if bot_username.lower() in BRAND_LOGO_BOTS and logo_path.is_file():
        # Preview-only: shows where the brand logo will be inserted on publish.
        # It is NOT part of the catalog, is never counted in the totals, is not
        # clickable/toggleable, and is never sent to /api/save.
        view.append({
            "key": LOGO_KEY, "fmt": "static", "label": "Brand logo (auto-added on publish)",
            "emoji": "", "included": True, "isLogo": True,
        })
        by_key[LOGO_KEY] = logo_path

    for it in items:
        label = (it.keywords[0] if it.keywords else
                 (it.emojis[0] if it.emojis else it.content_key[2:10]))
        view.append({
            "key": it.content_key,
            "fmt": it.fmt,
            "label": label,
            "copyId": copy_id_for(label),
            "emoji": it.emojis[0] if it.emojis else "",
            "included": it.included,
        })
        by_key[it.content_key] = Path(it.file_path)
    return view, by_key, hidden


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _is_loopback(netloc: str) -> bool:
    """True if a Host/Origin authority points at this machine's loopback."""
    host = netloc.rsplit("://", 1)[-1]
    if host.startswith("["):                    # [::1]:9450
        host = host[:host.index("]") + 1] if "]" in host else host
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host in LOOPBACK_HOSTS


# Animated previews are rendered once and kept on disk. The work is ~300-500 ms
# per animation, and the server is threaded, so a scrolling browser will ask for
# the same key from several connections at once -- one lock per key collapses
# that to a single render instead of N identical ones fighting for the CPU.
_preview_locks: dict[str, threading.Lock] = {}
_preview_locks_guard = threading.Lock()


def _preview_bytes(key: str, src: Path, db_path: Path, fps: int,
                   still: bool = False) -> bytes:
    """Preview for a .tgs, rendered once and cached on disk.

    ``still`` gives frame 0 as a single-frame WebP, which is what off-screen
    cards show -- see lottie_still_webp for why that matters.
    """
    cache_dir = db_path.parent / "preview"
    # content_key is a hash of the media, so the name can never go stale; ':'
    # is not legal in a Windows filename. The rate is part of the name because
    # it changes the bytes -- otherwise --preview-fps would silently serve
    # whatever the last run happened to render.
    tag = "still" if still else str(fps)
    dest = cache_dir / f"{key.replace(':', '_')}@{tag}.webp"
    if dest.is_file():
        return dest.read_bytes()
    with _preview_locks_guard:
        per_key = _preview_locks.setdefault(dest.name, threading.Lock())
    with per_key:
        if not dest.is_file():          # another thread may have won the race
            if still:
                lottie_still_webp(src, dest)
            else:
                lottie_preview_webp(src, dest, fps=fps)
        return dest.read_bytes()


def make_handler(view: list[dict], by_key: dict, db_path: Path, token: str,
                 preview_fps: int = PREVIEW_FPS, bot_username: str = "",
                 show_published: bool = False, hidden: int = 0,
                 keep_sets: set[str] | None = None):
    lock = threading.Lock()
    # A one-element list, not an int: `_reload_view` has to update it and the
    # page handler has to read the update, and rebinding a closed-over int
    # would leave the handler reading the value from start-up forever -- the
    # same trap `view`/`by_key` are rebuilt in place to avoid.
    hidden_now = [hidden]

    def _reload_view() -> None:
        """Refresh ``view``/``by_key`` from the catalog, IN PLACE.

        In place, not rebound: the handler and every route close over these two
        objects, so replacing them would leave the routes serving the old ones.

        Call under ``lock`` -- /api/order sorts ``view`` and /api/save writes
        ``included`` into it, and a rebuild racing either of those would drop a
        change that was already accepted.
        """
        try:
            cat = Catalog(db_path)
            try:
                fresh, fresh_by_key, fresh_hidden = build_view(
                    cat, bot_username, show_published, keep_sets)
            finally:
                cat.close()
        except Exception as exc:  # noqa: BLE001 - a page load must not 500
            log.warning("could not refresh from the catalog: %s", exc)
            return
        view[:] = fresh
        by_key.clear()
        by_key.update(fresh_by_key)
        hidden_now[0] = fresh_hidden

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet default logging
            pass

        def _send(self, code, body: bytes, ctype="application/json", *,
                  cache: str = ""):
            # Swallow benign disconnects (browser navigated away / cancelled a
            # media request): these raise ConnectionAbortedError/BrokenPipeError
            # on Windows and only spam the log with harmless tracebacks.
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if cache:
                    self.send_header("Cache-Control", cache)
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

        def _mutation_allowed(self) -> str:
            """Guard state-changing requests. Returns "" when allowed.

            The panel listens on localhost, so any page in the user's browser
            can POST to it. A per-run token in a custom header cannot be sent
            by a cross-origin ``no-cors`` request and cannot be read by one, so
            it is what actually stops a hostile page from re-ordering or
            de-selecting the catalog.
            """
            if not secrets.compare_digest(
                    self.headers.get("X-Panel-Token", ""), token):
                return "bad or missing panel token"
            if not _is_loopback(self.headers.get("Host", "")):
                return "unexpected Host"
            origin = self.headers.get("Origin")
            if origin is not None and not _is_loopback(origin):
                return "unexpected Origin"
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip()
            if ctype != "application/json":
                return "Content-Type must be application/json"
            return ""

        def handle_one_request(self):
            # Same for header/parse-level disconnects.
            try:
                super().handle_one_request()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def do_GET(self):
            # Reads need the same Host check as writes. Binding to 127.0.0.1
            # keeps remote sockets out, but any page whose hostname resolves to
            # loopback reaches this server same-origin and can read every
            # response -- and "/" carries the per-run mutation token in its
            # body, which is the one secret the POST guard rests on.
            if not _is_loopback(self.headers.get("Host", "")):
                self._send(403, b"unexpected Host", "text/plain")
                return
            if self.path == "/api/ping":
                # The page polls this so it can TELL YOU when this process is
                # gone. Deliberately touches neither the lock nor the catalog:
                # a liveness probe that can block behind a publish would report
                # a healthy server as dead. No token: it reveals nothing.
                self._send(200, b'{"ok":true}', cache="no-store")
                return
            if self.path == "/" or self.path.startswith("/index"):
                # /api/order sorts ``view`` in place, and CPython empties a list
                # for the duration of list.sort(); serialising it unlocked
                # rendered an empty grid.
                with lock:
                    # Re-read the catalog on every page load. ``view`` used to be
                    # a snapshot taken once at start-up, so anything that changed
                    # the catalog afterwards -- fetch_emoji_ids.py adding an
                    # emoji, add_media.py ingesting a folder -- was invisible
                    # until the panel was restarted, and a refresh looked like it
                    # did nothing. Reading 200 rows costs milliseconds.
                    _reload_view()
                    items = _json_for_script(view)
                page = (PAGE.replace("__ITEMS__", items).replace("__TOKEN__", token)
                            .replace("__PREVIEW_FPS__", str(preview_fps))
                            .replace("__PER_SET__", str(PER_SET))
                            .replace("__HIDDEN__", str(hidden_now[0])))
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8",
                           cache="no-store")
                return
            if self.path.startswith("/img/"):
                key = unquote(self.path[len("/img/"):])
                it = by_key.get(key)
                if not it or not it.is_file():
                    self._send(404, b"not found", "text/plain")
                    return
                data = it.read_bytes()
                self._send(200, data,
                           _MIME.get(it.suffix.lower(), "application/octet-stream"),
                           cache=_IMMUTABLE)
                return
            if self.path.startswith("/preview/"):
                # The frame rate is in the URL, not just the disk filename:
                # these are served immutable, so a browser that cached the old
                # rate would keep using it and --preview-fps would look inert.
                path, _, query = self.path.partition("?")
                still = "still=1" in query
                key = unquote(path[len("/preview/"):])
                it = by_key.get(key)
                if not it or not it.is_file():
                    self._send(404, b"not found", "text/plain")
                    return
                try:
                    body = _preview_bytes(key, it, db_path, preview_fps, still)
                except Exception as exc:  # noqa: BLE001 - one bad item must not 500 the grid
                    log.warning("preview failed for %s: %s", key, exc)
                    self._send(404, b"no preview", "text/plain")
                    return
                # Keyed by content_key, so the bytes can never change under it.
                self._send(200, body, "image/webp", cache=_IMMUTABLE)
                return
            if self.path.startswith("/static/"):
                name = unquote(self.path[len("/static/"):])
                f = (ASSET_DIR / name).resolve()
                # Resolve first, then require physical containment: comparing
                # parents would reject a legitimate subdirectory and would not
                # stop a symlink pointing outside the tree.
                if f.is_file() and f.is_relative_to(ASSET_DIR.resolve()):
                    ctype = ("application/javascript" if f.suffix == ".js"
                             else _MIME.get(f.suffix.lower(),
                                            "application/octet-stream"))
                    self._send(200, f.read_bytes(), ctype, cache=_IMMUTABLE)
                else:
                    self._send(404, b"not found", "text/plain")
                return
            self._send(404, b"not found", "text/plain")

        def _body_length(self):
            """Declared body size, or None after sending the right 4xx."""
            try:
                n = int(self.headers.get("Content-Length"))
            except (TypeError, ValueError):
                self._send(411, b'{"error":"Content-Length required"}')
                return None
            if n < 0 or n > MAX_BODY:
                self._send(413, b'{"error":"body too large"}')
                return None
            return n

        def _read_body(self, n: int) -> bytes | None:
            try:
                return self.rfile.read(n)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                return None

        def do_POST(self):
            try:
                self._route_post()
            except sqlite3.Error as exc:
                # build_collection.py reads the same database file and
                # sqlite3.connect only waits 5 s, so "database is locked" is
                # routine here, not freak. Uncaught it escaped the handler and
                # closed the socket with no HTTP response at all, so the panel
                # could only say "Save failed" with no reason.
                self._send(503, json.dumps(
                    {"error": f"catalog unavailable: {exc}"}).encode())

        def _route_post(self):
            n = self._body_length()
            if n is None:
                return
            # Read the body BEFORE answering, even when the request is going to
            # be rejected: replying to a request whose body is still in flight
            # resets the connection, so the client sees an abort instead of the
            # 403 explaining what was wrong.
            raw = self._read_body(n)
            if raw is None:
                return

            why = self._mutation_allowed()
            if why:
                self._send(403, json.dumps({"error": why}).encode())
                return

            try:
                payload = json.loads(raw or b"{}")
            except (ValueError, UnicodeDecodeError):
                self._send(400, b'{"error":"malformed JSON"}')
                return
            if not isinstance(payload, dict):
                self._send(400, b'{"error":"expected a JSON object"}')
                return

            if self.path == "/api/save":
                if set(payload) - {"excluded"}:
                    self._send(400, b'{"error":"unknown keys"}')
                    return
                raw = payload.get("excluded", [])
                if not isinstance(raw, list) or not all(isinstance(k, str) for k in raw):
                    self._send(400, b'{"error":"excluded must be a list of keys"}')
                    return
                # ``known`` MUST be read under the lock: /api/order sorts
                # ``view`` in place and CPython empties a list for the duration
                # of list.sort(), so a save landing in that window saw no known
                # keys, intersected the request down to nothing, and
                # set_inclusion(set()) re-included every row -- discarding the
                # whole de-selection while still answering {"ok": true}.
                with lock:
                    known = {v["key"] for v in view if not v.get("isLogo")}
                    excluded = set(raw) & known
                    cat = Catalog(db_path)
                    try:
                        # A FILTERED view can only speak for what it shows.
                        # set_inclusion re-includes every key it is not given,
                        # so saving from a grid that hides finished packs would
                        # silently re-include every hidden item that had been
                        # deselected. Carry their current state through.
                        hidden_excluded = {
                            it.content_key for it in cat.all_items()
                            if not it.included and it.content_key not in known}
                        inc, exc = cat.set_inclusion(excluded | hidden_excluded)
                    finally:
                        cat.close()
                    for v in view:
                        v["included"] = v["key"] not in excluded
                self._send(200, json.dumps({"ok": True, "included": inc, "excluded": exc}).encode())
                return

            if self.path == "/api/order":
                # Persist the manual drag-drop order. The logo preview key is
                # ignored (it's not a catalog item). Anything other than an
                # exact permutation of the current keys is rejected: a partial
                # or padded list would silently drop items from the publish
                # order.
                if set(payload) - {"order"}:
                    self._send(400, b'{"error":"unknown keys"}')
                    return
                raw = payload.get("order", [])
                if not isinstance(raw, list) or not all(isinstance(k, str) for k in raw):
                    self._send(400, b'{"error":"order must be a list of keys"}')
                    return
                keys = [k for k in raw if k != LOGO_KEY]
                with lock:
                    # Under the lock for the same reason as /api/save, and
                    # because this is check-then-act: validating against a
                    # ``view`` that a concurrent sort has emptied rejects a
                    # perfectly good order as "not a permutation".
                    expected = sorted(v["key"] for v in view if not v.get("isLogo"))
                    ok = sorted(keys) == expected
                    if ok:
                        cat = Catalog(db_path)
                        try:
                            cat.set_order(keys)
                            cat.set_meta("order_seeded", "1")
                        finally:
                            cat.close()
                        # Reorder the in-memory view to match (logo stays first).
                        pos = {k: i for i, k in enumerate(keys)}
                        view.sort(key=lambda v: (not v.get("isLogo"), pos.get(v["key"], 1 << 30)))
                if not ok:
                    self._send(400, b'{"error":"order must be a permutation of current keys"}')
                    return
                self._send(200, json.dumps({"ok": True, "count": len(keys)}).encode())
                return

            self._send(404, b"{}")

    return Handler


# The page is an ASSET, not source: 902 of this module's lines were one
# HTML/CSS/JS string literal, leaving 720 lines of actual Python. Kept out
# of the .py so ruff stops linting a JS blob as opaque, editors highlight it
# properly, and a UI diff stops churning this file.
#
# ROOT-relative like BRAND_LOGO_DEFAULT, never absolute: an absolute asset
# path is exactly how the "mandatory" brand logo silently vanished on every
# other machine. ThePanelPageActuallyShips pins that this file is present.
PAGE = (ASSET_DIR / "panel.html").read_text(encoding="utf-8")


def _detect_bot_username() -> str:
    """Best-effort: which bot's token is configured, so the panel can preview
    the brand logo only when it would actually be added on publish (i.e. the
    Emoji Mapper bot, never the coin bot). Never raises -- on any error
    (missing .env, no network, bad token) the logo preview is simply skipped.
    """
    try:
        from build_pack import (load_env)
        from telegram_api import (Telegram)
        load_env()
        token = os.environ.get("GENERAL_BOT_TOKEN", "")
        if not token:
            return ""
        return Telegram(token).get_me().get("username", "")
    except Exception as exc:  # noqa: BLE001 - preview-only, never fatal
        log.debug("bot username detection failed: %s", exc)
        return ""


def _port_holder(port: int) -> int | None:
    """The pid listening on ``port``, or None when it cannot be determined.

    Best-effort and never fatal: it runs only to improve an error message, so a
    missing tool, a parse surprise or a slow call must not turn "the panel is
    already open" into a traceback.
    """
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()          # proto  local  foreign  state  pid
        if (len(parts) >= 5 and parts[3].upper() == "LISTENING"
                and parts[1].endswith(f":{port}")):
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def main() -> int:
    # BEFORE setup_logging: the logger registers the literal values of
    # SECRET_ENV_KEYS so they can be masked wherever they appear, and it can only
    # register what is already in the environment. Loading .env afterwards -- as
    # this did, via _detect_bot_username() further down -- left any .env-only
    # credential unregistered for literal masking in the one process that serves
    # a browser UI. It also decides the log retention window.
    from build_pack import load_env
    load_env()
    setup_logging("panel")
    ap = argparse.ArgumentParser(description="Curate downloaded emoji before publishing.")
    ap.add_argument("--data-dir", default="collection")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--preview-fps", type=int, default=PREVIEW_FPS,
                    help="Frame rate for animated previews. The grid can show "
                         "60+ cards at once and the browser decodes every frame "
                         "of each, so this is the main lever on how heavy the "
                         "panel feels (default: %(default)s).")
    ap.add_argument("--no-open", action="store_true", help="Don't auto-open the browser.")
    ap.add_argument("--all", action="store_true",
                    help="Also show emoji already live in a pack. They are hidden by "
                         "default so the grid is the pack being built.")
    ap.add_argument("--with-pack", type=int, action="append", metavar="N",
                    help="Also show the emoji already live in pack N, so a "
                         "half-full pack can be arranged beside the new "
                         "candidates going into it. Repeatable.")
    args = ap.parse_args()

    data_dir = (ROOT / args.data_dir) if not os.path.isabs(args.data_dir) else Path(args.data_dir)
    db_path = data_dir / "catalog.db"
    if not db_path.is_file():
        log.error("no catalog at %s (run fetch_pack.py / add_media.py first).", db_path)
        return 2

    bot_username = _detect_bot_username()

    keep_sets = packs_named(data_dir, set(args.with_pack)) if args.with_pack else set()
    if args.with_pack and not keep_sets:
        # Silence here would look identical to "that pack holds nothing".
        log.error("no published pack matches %s in %s",
                  sorted(set(args.with_pack)), data_dir)
        return 2
    if keep_sets:
        log.info("also showing already-live emoji from: %s",
                 ", ".join(sorted(keep_sets)))

    cat = Catalog(db_path)
    try:
        view, by_key, hidden = build_view(cat, bot_username, args.all, keep_sets)
    finally:
        cat.close()
    log.info("loaded %d emoji from %s", len(view), db_path)

    # Per-run mutation token: a page on another origin can neither read it nor
    # attach it to a no-cors POST, so it cannot re-order or de-select the
    # catalog behind the user's back.
    token = secrets.token_urlsafe(24)
    handler = make_handler(view, by_key, db_path, token, args.preview_fps,
                           bot_username, args.all, hidden, keep_sets)

    class QuietServer(ThreadingHTTPServer):
        # SO_REUSEADDR OFF. socketserver turns it on by default, and on Windows
        # that does NOT mean what it means on Linux: a second bind to a port
        # that already has a live listener SUCCEEDS. Two panels then run, both
        # logging "Panel at ...", the browser reaches whichever socket the OS
        # picks, and the older process keeps serving its own start-up snapshot
        # of the page and the catalog. That is why refreshing appeared to do
        # nothing and only closing the launcher -- which kills every instance --
        # made a change show up.
        allow_reuse_address = False

        # Don't dump a traceback when a browser simply drops a connection
        # (very common while scrolling a media-heavy grid on Windows).
        def handle_error(self, request, client_address):
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
                return
            super().handle_error(request, client_address)

    try:
        httpd = QuietServer(("127.0.0.1", args.port), handler)
    except OSError as exc:
        # Say what to do about it. A traceback here reads as "the panel is
        # broken" when the real state is "the panel is already open".
        log.error("cannot listen on port %d: %s", args.port, exc)
        pid = _port_holder(args.port)
        # NAME the process. "Press Ctrl+C in the window running it" is useless
        # when the holder was started detached and has no window -- which is how
        # every stray one so far got there.
        stop = (f"  Or stop it:   taskkill /PID {pid} /F" if pid
                else "  Or stop it:   press Ctrl+C in the window running it")
        who = f" (pid {pid})" if pid else ""
        print(
            f"\nA panel is already running on port {args.port}{who}." + "\n"
            f"  Open it:      http://127.0.0.1:{args.port}/" + "\n"
            + stop + "\n"
            f"  Or use another port:  panel.py --port {args.port + 1}" + "\n",
            flush=True)
        return 2
    url = f"http://127.0.0.1:{args.port}/"
    log.info("Panel at %s  (Ctrl+C to stop)", url)
    print(f"Emoji curate panel: {url}", flush=True)
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(record_exit_code(main()))
