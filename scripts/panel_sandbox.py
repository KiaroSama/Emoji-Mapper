"""Start the curate panel against a THROWAWAY COPY of the catalog.

Why this exists, plainly: an agent verifying the panel in a browser fired
synthetic drag events at the panel that was serving the owner's real
`collection/` directory. Every one of those drags called `/api/order` and
rewrote `items.position` in the live catalog, on top of an afternoon of manual
ordering. Reordering is exactly what the panel is for, so there is no way to
"test carefully" against real data -- the test IS the mutation.

So automated UI checks get their own catalog, their own port, and their own
credentials-free environment:

* the catalog is CLONED by `emojikit.sandbox_clone` -- its own database bytes,
  its own media bytes, every path repointed -- and the clone is deleted on exit;
* its port is the real panel's + 1, taken from `panel.DEFAULT_PORT` rather than
  typed again, so a sandbox can never take the port a real panel is on and a
  real panel is never mistaken for the sandbox;
* the arguments are an ALLOWLIST. This used to forward unknown options straight
  to the panel, after its own `--data-dir`, so `panel_sandbox.py --data-dir
  collection` served the owner's live catalog while this file printed that the
  live catalog was not served. Nothing is forwarded now; the panel's argument
  list is built here, explicitly;
* the panel runs IN THIS PROCESS with session reuse off, so killing the wrapper
  stops the server and drops its lease together, and no unidentified listener is
  ever adopted as the sandbox.

Usage (this is what `.claude/launch.json` runs):

    .venv\\Scripts\\python.exe scripts/panel_sandbox.py
    .venv\\Scripts\\python.exe scripts/panel_sandbox.py --source collection --port 8766
    .venv\\Scripts\\python.exe scripts/panel_sandbox.py --all      # published packs too
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from emojikit import panel  # noqa: E402 - needs ROOT on the path
from emojikit.sandbox_clone import (  # noqa: E402
    TMP_PREFIX, sandbox_session, sweep_stale)

# IMPORTED, never re-typed: the sandbox's whole job is to stay off the port a
# real panel uses, and two copies of that number would drift the day one moves.
PANEL_PORT = panel.DEFAULT_PORT
DEFAULT_PORT = PANEL_PORT + 1

# Names that never belong in a sandbox child, beyond whatever `.env.example`
# lists: an owner may export a credential by hand, and an exported value needs
# no dotenv file to reach the child.
_SECRETISH = re.compile(r"TOKEN|SECRET|PASSWORD|_KEY$|^OWNER_ID$", re.IGNORECASE)


def scrubbed_environment() -> dict[str, str]:
    """The panel's environment, with this project's credentials removed.

    The sandbox isolated the CATALOG and not the ACCOUNT. `emojikit.panel`
    calls `_detect_bot_username()`, which calls `load_env()` and then `getMe` --
    so a sandbox started precisely to avoid touching the owner's data still
    reached live Telegram as the real bot, with the real token.

    Two leaks, two plugs. `NUMERA_EMOJI_MAPPER_NO_DOTENV` is the flag `load_env()`
    already honours for the test suite, so `.env` is never read; the inherited
    copies have to go with it, because an already-exported token does not need
    the file. `.env.example` is the authoritative key list and holds no values,
    so this stays correct as the project's credentials change.
    """
    template = ROOT / ".env.example"
    listed: set[str] = set()
    if template.is_file():
        listed = {line.split("=", 1)[0].strip()
                  for line in template.read_text(encoding="utf-8").splitlines()
                  if "=" in line and not line.lstrip().startswith("#")}
    child = {k: v for k, v in os.environ.items()
             if k not in listed and not _SECRETISH.search(k)}
    child["NUMERA_EMOJI_MAPPER_NO_DOTENV"] = "1"
    return child


@contextlib.contextmanager
def scrubbed_process_environment():
    """Apply the scrub to THIS process, then put the environment back.

    In-process is what makes the wrapper's death stop the server, so the scrub
    has to be applied here rather than handed to a child. Restoring matters
    because a caller that imported this module keeps running afterwards.
    """
    # Computed BEFORE the clear, and that order is the whole point: reading
    # `os.environ` after clearing it returns an EMPTY mapping, so the panel ran
    # with nothing but the dotenv flag -- no SystemRoot, no PATH -- and Winsock
    # could not even create a socket (`WinError 10106`). A scrub that removes
    # everything is not a scrub.
    child = scrubbed_environment()
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(child)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def build_parser() -> argparse.ArgumentParser:
    """An allowlist, deliberately.

    `allow_abbrev=False` because an abbreviation that is unambiguous today
    becomes a different option the day another is added, and this parser's whole
    job is that no argument can change what gets served. There is no
    `parse_known_args` and no forwarding: `--data-dir` reaches the panel from
    exactly one place, below.
    """
    ap = argparse.ArgumentParser(
        prog="panel_sandbox.py", allow_abbrev=False,
        description="Serve a THROWAWAY clone of the catalog. Never the real one.")
    ap.add_argument("--source", default="collection",
                    help="Catalog to CLONE (never served directly).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--all", action="store_true",
                    help="Show already-published emoji too.")
    ap.add_argument("--with-pack", type=int, action="append", default=[],
                    metavar="N", help="Include published pack N (repeatable).")
    ap.add_argument("--bot-username", default="",
                    help="Branding only; supplied so nothing contacts Telegram.")
    return ap


def panel_arguments(args: argparse.Namespace, data_dir: Path) -> list[str]:
    """The panel's argument list, built here rather than forwarded."""
    argv = ["--data-dir", str(data_dir), "--port", str(args.port), "--no-open"]
    if args.all:
        argv.append("--all")
    for number in args.with_pack:
        argv += ["--with-pack", str(number)]
    if args.bot_username:
        argv += ["--bot-username", args.bot_username]
    return argv


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.port == PANEL_PORT:
        raise SystemExit(f"refusing port {PANEL_PORT}: that is the real panel's port")
    if not 1 <= args.port <= 65535:
        raise SystemExit(f"refusing port {args.port}: not a port")

    source = (ROOT / args.source).resolve()
    sweep_stale()
    tmp = Path(tempfile.gettempdir()) / f"{TMP_PREFIX}{uuid.uuid4().hex[:8]}"

    # Acquire lifetime ownership BEFORE creating the published clone marker.
    # The same lease spans serving and cleanup, including exceptional exits.
    with sandbox_session(source, tmp) as n, scrubbed_process_environment():
        print(f"sandbox catalog: {n} items cloned from {source} -> {tmp}", flush=True)
        print(f"the source catalog at {source} is not served", flush=True)
        return panel.main(panel_arguments(args, tmp), reuse_existing=False)


if __name__ == "__main__":
    raise SystemExit(main())
