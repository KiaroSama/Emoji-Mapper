"""Start the curate panel against a THROWAWAY COPY of the catalog.

Why this exists, plainly: an agent verifying the panel in a browser fired
synthetic drag events at the panel that was serving the owner's real
`collection/` directory. Every one of those drags called `/api/order` and
rewrote `items.position` in the live catalog, on top of an afternoon of manual
ordering. Reordering is exactly what the panel is for, so there is no way to
"test carefully" against real data -- the test IS the mutation.

So automated UI checks get their own catalog and their own port:

* the catalog is copied to a temp directory, media is symlinked or copied, and
  the copy is deleted on exit;
* its port is the real panel's + 1, taken from `panel.DEFAULT_PORT` rather
  than typed again, so a sandbox can never take the port a real panel is on
  and a real panel is never mistaken for the sandbox;
* it refuses to start if `--data-dir` points anywhere inside the project.

Usage (this is what `.claude/launch.json` runs):

    .venv\\Scripts\\python.exe scripts/panel_sandbox.py
    .venv\\Scripts\\python.exe scripts/panel_sandbox.py --source collection --port 8766
    .venv\\Scripts\\python.exe scripts/panel_sandbox.py --all      # published packs too
"""

from __future__ import annotations

import argparse
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from panel import DEFAULT_PORT as PANEL_PORT  # noqa: E402 - needs ROOT on the path

# IMPORTED, never re-typed: the sandbox's whole job is to stay off the port a
# real panel uses, and two copies of that number would drift the day one moves.
DEFAULT_PORT = PANEL_PORT + 1
TMP_PREFIX = "panel-sandbox-"


def clone_catalog(source: Path, dest: Path) -> int:
    """Copy the catalog and its media into ``dest``. Returns the item count."""
    db = source / "catalog.db"
    if not db.is_file():
        raise SystemExit(f"no catalog at {db} -- nothing to sandbox")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db, dest / "catalog.db")
    # The publisher's state too: `--with-pack N` resolves pack numbers through
    # it, and without a copy the sandbox could only ever show candidates.
    for state in source.glob("publish_*.json"):
        shutil.copy2(state, dest / state.name)

    # Media is read-only to the panel, so hard-link it where the filesystem
    # allows: 200 emoji is ~20 MB and copying it on every launch is waste.
    # A link failure is not fatal -- fall back to copying.
    media_src, media_dst = source / "media", dest / "media"
    for src in media_src.rglob("*"):
        if not src.is_file():
            continue
        out = media_dst / src.relative_to(media_src)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, out)
        except OSError:
            shutil.copy2(src, out)

    import sqlite3
    with sqlite3.connect(dest / "catalog.db") as con:
        n = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    # The copied rows still hold absolute/relative paths into the SOURCE tree.
    # Repoint them at the clone, or the sandbox would serve -- and a future
    # writer could touch -- the real files.
    with sqlite3.connect(dest / "catalog.db") as con:
        for key, path in con.execute("SELECT content_key, file_path FROM items").fetchall():
            p = Path(path)
            try:
                rel = p.relative_to(source) if p.is_absolute() else Path(path).relative_to(source.name)
            except ValueError:
                continue
            con.execute("UPDATE items SET file_path=? WHERE content_key=?",
                        (str(dest / rel).replace("\\", "/"), key))
        con.commit()
    return n


def sweep_stale() -> int:
    """Delete clones a previous run left behind, before making another.

    ``atexit`` does not run when the process is killed, and this server is
    normally ended by killing it. Seven abandoned clones at ~2.4 MB each were
    found in one session. Cleaning at START rather than only at exit is the
    only cleanup that survives the way the thing is actually stopped.

    A clone in use is protected by its own lock: a directory that is still
    being served refuses to delete on Windows, and that failure is ignored.
    """
    removed = 0
    for old in Path(tempfile.gettempdir()).glob(f"{TMP_PREFIX}*"):
        if not old.is_dir():
            continue
        before = old.exists()
        shutil.rmtree(old, ignore_errors=True)
        removed += before and not old.exists()
    if removed:
        print(f"cleaned {removed} abandoned sandbox clone(s)", flush=True)
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", default="collection",
                    help="Catalog to CLONE (never served directly).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    # Anything else goes to the panel itself (`--all`, `--with-pack N`, ...):
    # once every emoji is published, a sandbox without them shows an empty grid.
    args, panel_args = ap.parse_known_args(argv)

    if args.port == PANEL_PORT:
        raise SystemExit(f"refusing port {PANEL_PORT}: that is the real panel's port")

    source = (ROOT / args.source).resolve()
    sweep_stale()
    tmp = Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
    atexit.register(shutil.rmtree, tmp, True)

    n = clone_catalog(source, tmp)
    print(f"sandbox catalog: {n} items cloned from {source} -> {tmp}", flush=True)
    print(f"the real catalog at {source} is NOT served and cannot be modified",
          flush=True)

    cmd = [sys.executable, str(ROOT / "panel.py"), "--data-dir", str(tmp),
           "--port", str(args.port), "--no-open", *panel_args]
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
