"""Move a FINISHED pack's media into the owner's archive, and keep it true.

The owner keeps one folder per published pack outside the repository, and the
project directory must not accumulate media it has already shipped. That was a
standing rule carried only in someone's head: nothing enforced it, so a publish
round left 347 files behind and the archive silently drifted from the packs.

Two halves, and the second is the one that was missing:

* **ONLY A FULL PACK IS ARCHIVED.** The filename carries the emoji's SLOT, and a
  slot is only settled once the pack is closed -- a half-filled pack can still be
  reordered, and then every name in its folder is a lie. A pack under
  ``per_set`` therefore owns no archive folder at all.
* **THE ARCHIVE FOLLOWS THE PACK.** A recolour mints a new ``custom_emoji_id``
  and a reorder moves slots, so a folder written once and never revisited stops
  describing the pack it names. ``--sync`` rewrites the names and the metadata
  from the LIVE set every time.

Layout of ``<archive>/<pack title>/``::

    001_logo_yourbrand.png                 the brand logo, byte-identical to the repo asset
    <slot:03d>_<format>_<key[:12]>.<ext>    one per emoji, slot is 1-based (the logo is 1)
    _history.json                           machine: every position, id, content key and glyph
    _history.md                             the same, for a human
    _manifest.md                            name -> current id, the quick lookup

``items.file_path`` is rewritten as each file moves: it is an absolute path, and
the roster gallery reads it to draw the artwork. Dedup is unaffected -- a
``content_key`` hashes normalised pixels held in the database, never the file.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from emojikit.collection_state import BRAND_LOGO_DEFAULT, PER_SET
from emojikit import logsetup
from emojikit.packstate import LockBusy, write_json_atomic
from emojikit.maintenance import writer

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "collection"
CATALOG = DATA_DIR / "catalog.db"
BASE = "YourBrand_Emoji_Packs"
STATE = DATA_DIR / f"publish_{BASE}.json"

# The archive lives on the owner's media drive, not in the repository. An env
# var so the path is not baked in: an absolute default is exactly how the brand
# logo once vanished on every other machine.
ARCHIVE_ENV = "EMOJI_ARCHIVE_DIR"
ARCHIVE_FALLBACK = Path(r"F:\Stickers and Emojis\Emojis")

LOGO_NAME = "001_logo_yourbrand.png"
META = ("_history.json", "_history.md", "_manifest.md")
NAME_RE = re.compile(r"^(\d{3})_(static|animated|video)_([0-9a-f]{12})\.")

EXIT_OK, EXIT_FAILED, EXIT_STALE = 0, 1, 3
log = logging.getLogger("pack_archive")


def archive_root() -> Path:
    raw = os.environ.get(ARCHIVE_ENV, "").strip()
    return Path(raw) if raw else ARCHIVE_FALLBACK


def _sets() -> list[dict]:
    if not STATE.is_file():
        return []
    try:
        return [s for s in json.loads(STATE.read_text(encoding="utf-8")).get("sets") or []
                if s.get("name") and s.get("title")]
    except (OSError, ValueError) as exc:
        log.error("cannot read %s: %s", STATE.name, exc)
        return []


def _catalog() -> tuple[dict[str, dict], dict[str, list[str]]]:
    """``content_key -> row`` and ``set_name -> [content_key, ...]``."""
    items: dict[str, dict] = {}
    by_set: dict[str, list[str]] = {}
    if not CATALOG.is_file():
        return items, by_set
    db = sqlite3.connect(CATALOG)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT i.content_key ck, i.file_path fp, i.format fmt, i.keywords kw, "
            "       p.set_name sn, p.custom_emoji_id cid "
            "FROM items i JOIN publications p ON p.content_key = i.content_key "
            "WHERE p.base = ?", (BASE,)).fetchall()
    finally:
        db.close()
    for r in rows:
        items[r["ck"]] = {"path": Path(r["fp"]), "fmt": r["fmt"], "cid": str(r["cid"] or ""),
                          "set": r["sn"], "keywords": json.loads(r["kw"] or "[]")}
        by_set.setdefault(r["sn"], []).append(r["ck"])
    return items, by_set


def archive_name(slot: int, fmt: str, key: str, suffix: str) -> str:
    return f"{slot:03d}_{fmt}_{key.split(':', 1)[1][:12]}{suffix}"


# --------------------------------------------------------------------------- #
# Freshness -- local only, so the Stop hook costs no network call
# --------------------------------------------------------------------------- #
def check() -> tuple[bool, list[str]]:
    """``(stale, reasons)`` from the catalog and the state file alone.

    Deliberately never reads Telegram: this runs on every Stop, and a gate that
    costs five API calls each time is a gate that gets switched off. `--sync`
    does read live, because that is where the slot has to be authoritative.
    """
    items, by_set = _catalog()
    root = archive_root()
    why: list[str] = []
    for rec in _sets():
        folder = root / rec["title"]
        full = int(rec.get("live") or 0) >= PER_SET
        keys = by_set.get(rec["name"], [])
        if not full:
            # Its slots can still move, so a folder here would name them wrongly.
            if folder.is_dir():
                why.append(f"{rec['title']}: not full ({rec.get('live')}/{PER_SET}) "
                           f"but already has an archive folder")
            continue
        if not folder.is_dir():
            why.append(f"{rec['title']}: full but never archived")
            continue
        inside = [k for k in keys if not str(items[k]["path"]).startswith(str(root))]
        if inside:
            why.append(f"{rec['title']}: {len(inside)} published file(s) still in the project")
        for m in (LOGO_NAME, *META):
            if not (folder / m).is_file():
                why.append(f"{rec['title']}: missing {m}")
        hist = folder / "_history.json"
        if hist.is_file():
            try:
                doc = json.loads(hist.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                why.append(f"{rec['title']}: _history.json is unreadable")
                continue
            # An id changes only when a sticker is REPLACED, and that is exactly
            # what the archive must not keep quiet about.
            recorded = {str(e.get("premium_id")) for e in doc.get("emoji", []) if e.get("premium_id")}
            current = {items[k]["cid"] for k in keys if items[k]["cid"]}
            if recorded != current:
                why.append(f"{rec['title']}: {len(current - recorded)} id(s) added/changed, "
                           f"{len(recorded - current)} retired since the archive was written")
    return bool(why), why


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _rows(rec: dict, live: list[dict], items: dict[str, dict],
          ck_of: dict[str, str]) -> list[dict]:
    """One row per LIVE position, slot order, logo first."""
    out = []
    for slot, st in enumerate(live, start=1):
        cid = str(st.get("custom_emoji_id") or "")
        ck = ck_of.get(cid)
        if ck is None:
            # The brand logo is inserted at publish time and has no catalog row,
            # so it can only ever be identified by sitting at slot 1.
            out.append({"position": slot, "file": LOGO_NAME, "format": "static",
                        "premium_id": None, "content_key": None,
                        "note": "brand logo, copied from the repo"})
            continue
        it = items[ck]
        out.append({"position": slot,
                    "file": archive_name(slot, it["fmt"], ck, it["path"].suffix),
                    "format": it["fmt"], "premium_id": cid, "content_key": ck,
                    "emoji": st.get("emoji")})
    return out


def render_history_md(rec: dict, rows: list[dict]) -> str:
    out = [f"# {rec['name']}", "",
           f"Pack: https://t.me/addemoji/{rec['name']}",
           f"Emoji in the pack: **{len(rows)}**", "",
           "`premium_id` is the id THIS pack's emoji has now -- the one to use in a",
           "`tg-emoji` tag. `content key` is the catalog's hash of the artwork, which",
           "survives a recolour and is how a file is matched back to its emoji.", "",
           "| # | file | format | premium_id | content key |",
           "|---|------|--------|------------|-------------|"]
    for r in rows:
        pid = f"`{r['premium_id']}`" if r["premium_id"] else "`-`"
        ck = f"`{r['content_key']}`" if r["content_key"] else "`brand logo`"
        out.append(f"| {r['position']} | `{r['file']}` | {r['format']} | {pid} | {ck} |")
    return "\n".join(out) + "\n"


def render_manifest_md(rec: dict, rows: list[dict], items: dict[str, dict]) -> str:
    real = [r for r in rows if r["content_key"]]
    out = [f"# {rec['title']}", "",
           f"Pack: https://t.me/addemoji/{rec['name']}  |  format: {rec.get('fmt', 'mixed')}  "
           f"|  {len(real)} emoji", "",
           "| # | Name | Emoji ID |", "|---|------|----------|"]
    for i, r in enumerate(real, start=1):
        name = ", ".join(items[r["content_key"]]["keywords"]) or r["content_key"]
        name = name.replace("|", "\\|")          # 3.11 f-strings reject a backslash inside {}
        out.append(f"| {i} | {name} | {r['premium_id']} |")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #
def sync(tg) -> int:
    with writer(CATALOG.parent):
        return _sync(tg)


def _sync(tg) -> int:
    from emojikit.telegram_api import Telegram  # noqa: F401  (typing only; tg is injected)

    items, by_set = _catalog()
    ck_of = {v["cid"]: k for k, v in items.items() if v["cid"]}
    root = archive_root()
    db = sqlite3.connect(CATALOG)
    touched = moved = renamed = 0
    try:
        for rec in _sets():
            folder = root / rec["title"]
            if int(rec.get("live") or 0) < PER_SET:
                if folder.is_dir():
                    log.warning("%s is not full; leaving its folder alone", rec["title"])
                continue
            live = tg.get_sticker_set(rec["name"])["stickers"]
            rows = _rows(rec, live, items, ck_of)
            folder.mkdir(parents=True, exist_ok=True)

            want = {r["file"]: r["content_key"] for r in rows if r["content_key"]}
            # Rename before moving: a file already here under a stale slot is the
            # same artwork, and re-deriving it from the project would fail -- the
            # project copy is exactly what a previous archive run took away.
            have = {}
            for p in folder.iterdir():
                m = NAME_RE.match(p.name) if p.is_file() else None
                if m:
                    have[m.group(3)] = p
            for name, ck in want.items():
                dest = folder / name
                cur = have.get(ck.split(":", 1)[1][:12])
                if cur is not None and cur != dest:
                    cur.replace(dest)
                    renamed += 1
                elif cur is None:
                    src = items[ck]["path"]
                    if not src.is_file():
                        log.error("%s: source missing for %s (%s)", rec["title"], ck, src)
                        return EXIT_FAILED
                    shutil.move(str(src), str(dest))
                    moved += 1
                db.execute("UPDATE items SET file_path=? WHERE content_key=?", (str(dest), ck))

            logo = folder / LOGO_NAME
            if not logo.is_file():
                shutil.copy2(BRAND_LOGO_DEFAULT, logo)   # copied, never moved: the repo keeps its asset
            write_json_atomic(folder / "_history.json",
                              {"set_name": rec["name"],
                               "link": f"https://t.me/addemoji/{rec['name']}",
                               "total": len(rows), "emoji": rows})
            (folder / "_history.md").write_text(render_history_md(rec, rows), encoding="utf-8")
            (folder / "_manifest.md").write_text(render_manifest_md(rec, rows, items), encoding="utf-8")

            strays = [p.name for p in folder.iterdir()
                      if p.is_file() and p.name not in want and p.name not in (LOGO_NAME, *META)]
            if strays:
                # Never deleted here: a file the live pack no longer knows may be
                # the only copy of art the owner still wants. Reported instead.
                log.warning("%s: %d file(s) no longer in the pack: %s",
                            rec["title"], len(strays), ", ".join(sorted(strays)[:5]))
            touched += 1
            log.info("%s: %d emoji archived", rec["title"], len(want))
        db.commit()
    finally:
        db.close()
    print(f"archive: {touched} full pack(s) synced, {moved} file(s) moved, "
          f"{renamed} renamed to a new slot")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="report whether the archive still describes the packs (exit 3 = stale)")
    ap.add_argument("--sync", action="store_true",
                    help="archive every FULL pack and refresh its metadata")
    args = ap.parse_args(argv)
    if not (args.check or args.sync):
        ap.error("give --check or --sync")

    if args.check:
        stale, why = check()
        print(("STALE: " + "; ".join(why)) if stale else "fresh: the archive matches the packs")
        return EXIT_STALE if stale else EXIT_OK

    logsetup.setup_logging("pack_archive")
    from emojikit.build_pack import load_env
    from emojikit.telegram_api import Telegram
    load_env()
    token = os.environ.get("GENERAL_BOT_TOKEN")
    if not token:
        log.error("GENERAL_BOT_TOKEN is not set; cannot read the live packs.")
        return EXIT_FAILED
    try:
        return sync(Telegram(token))
    except LockBusy as exc:
        log.error("%s", exc)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
