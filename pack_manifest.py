"""Write the published packs' emoji rosters to ``packs/``, for humans and machines.

One file pair per live set -- ``packs/<set>.md`` to read and ``packs/<set>.json``
to parse -- plus ``packs/index.json`` and ``packs/README.md`` over the whole
estate. Two families are covered: the general packs built by
``build_collection.py`` and the 29 crypto packs built by ``coins/``.

**The order and the ids come from TELEGRAM, never from a state file.** A pack can
be reordered live (``sync_order.py``), an emoji can be replaced in place, and a
replacement MINTS A NEW custom_emoji_id -- so a roster rebuilt from the recorded
plan would drift the moment any of that happens, silently, which is exactly the
kind of wrong-but-plausible file this is meant to prevent. ``getStickerSet``
answers what is actually there, in the actual order.

Provenance is the one thing Telegram cannot tell us, so it is joined in:
* general -- ``premium-id:<n>`` keywords, recorded by ``fetch_emoji_ids.py``,
  give the id the emoji had in the pack we took it from;
* coins -- ``coins/ticker_to_id.json`` gives the ticker each logo stands for.

Slot 1 of every general pack is the brand logo. It has no catalog row (it is not
an ingested item), which is why the older ``collection/manifests`` left its id
blank -- reading the live set fixes that for free, and the owner asked for it
explicitly.

Usage::

    python pack_manifest.py --refresh              # read live, rewrite packs/
    python pack_manifest.py --refresh --family coins
    python pack_manifest.py --check                # stale? exit 3. No network.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pack_gallery
from build_pack import EXIT_FAILED, EXIT_OK, Telegram, load_env
from collection_state import BRAND_LOGO_DEFAULT
from emojikit import media
from emojikit.logsetup import record_exit_code, setup_logging
from packstate import write_json_atomic

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "packs"
DATA_DIR = ROOT / "collection"
COINS_DIR = ROOT / "coins"
GENERAL_STATE = DATA_DIR / "publish_GodVerify_Emoji_Packs.json"
COINS_STATE = COINS_DIR / "rebuild_dedup_state.json"
TICKER_MAP = COINS_DIR / "ticker_to_id.json"
CATALOG = DATA_DIR / "catalog.db"
THUMBS = OUT_DIR / ".thumbs"
# The coin logos are a 5877-file corpus kept OUTSIDE the repo. Never hard-code
# one machine's drive letter: the brand logo once did and vanished everywhere
# else. Env var first, then the documented location, then no pictures.
COIN_ART_ENV = "COIN_EMOJI_DIR"
COIN_ART_FALLBACK = Path(r"F:\Stickers and Emojis\Emojis\@GodVerify Crypto Emoji\emoji")

EXIT_STALE = 3          # --check only: the roster no longer describes the inputs
log = logging.getLogger("pack_manifest")


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def _sets(state: Path) -> list[dict]:
    """The ``sets`` list of a publisher state file, or [] when it has none."""
    if not state.is_file():
        return []
    try:
        return list(json.loads(state.read_text(encoding="utf-8")).get("sets") or [])
    except (OSError, ValueError) as exc:
        log.warning("cannot read %s: %s", state.name, exc)
        return []


def general_provenance(live_ids: set[str]) -> dict[str, dict]:
    """``custom_emoji_id -> {name, source_emoji_ids}`` for the general family.

    An item's keywords can hold SEVERAL ``premium-id:`` values: the id it was
    downloaded from, and -- for anything the bot inventories were repointed
    at -- one of OUR own ids too. Only the ones that are not live in our packs
    are genuinely foreign, so that is the test. Cheaper and more honest than
    guessing by position in the list.
    """
    out: dict[str, dict] = {}
    if not CATALOG.is_file():
        return out
    db = sqlite3.connect(CATALOG)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT p.custom_emoji_id AS cid, i.keywords, i.emojis, i.format "
            "FROM publications p JOIN items i ON i.content_key = p.content_key").fetchall()
    finally:
        db.close()
    for r in rows:
        kws = json.loads(r["keywords"])
        srcs = [k.split(":", 1)[1] for k in kws if k.startswith("premium-id:")]
        labels = [k for k in kws if not k.startswith("premium-id:")]
        out[str(r["cid"])] = {
            "name": ", ".join(labels) or None,
            "source_emoji_ids": [s for s in srcs if s not in live_ids],
        }
    return out


def coin_provenance() -> dict[str, list[str]]:
    """``custom_emoji_id -> [ticker, ...]``.

    A list, not a string: shared-logo coins legitimately point several tickers
    at one sticker, and collapsing them would drop real information.
    """
    if not TICKER_MAP.is_file():
        return {}
    try:
        pairs = json.loads(TICKER_MAP.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("cannot read %s: %s", TICKER_MAP.name, exc)
        return {}
    out: dict[str, list[str]] = defaultdict(list)
    for ticker, cid in pairs.items():
        out[str(cid)].append(ticker)
    return {k: sorted(v) for k, v in out.items()}


def coin_art_dir() -> Path | None:
    """Where the coin logo PNGs live, or None when this machine cannot see them."""
    env = os.environ.get(COIN_ART_ENV)
    for cand in ([Path(env)] if env else []) + [COIN_ART_FALLBACK]:
        if cand.is_dir():
            return cand
    log.warning("coin art not found (set %s); coin pages will have no pictures.",
                COIN_ART_ENV)
    return None


def general_art() -> dict[str, Path]:
    """``custom_emoji_id -> the file we uploaded``, straight from the catalog."""
    if not CATALOG.is_file():
        return {}
    db = sqlite3.connect(CATALOG)
    try:
        return {str(c): Path(f) for c, f in db.execute(
            "SELECT p.custom_emoji_id, i.file_path FROM publications p "
            "JOIN items i ON i.content_key = p.content_key")}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _row(pos: int, st: dict, *, is_logo: bool, name, sources) -> dict:
    """One roster entry. ``index`` is 0-based (the owner counts the logo as 0),
    ``slot`` is what Telegram shows."""
    return {
        "index": pos,
        "slot": pos + 1,
        "custom_emoji_id": str(st.get("custom_emoji_id") or ""),
        "glyph": st.get("emoji"),
        "format": media.telegram_sticker_format(st),
        "role": "brand-logo" if is_logo else "emoji",
        "name": name,
        "source_emoji_ids": list(sources or []),
    }


def build_pack(tg: Telegram, rec: dict, family: str, prov, live_ids: set[str]) -> dict:
    """Read one live set and shape it into a roster document."""
    name = rec["name"]
    stickers = tg.get_sticker_set(name)["stickers"]
    rows = []
    for i, st in enumerate(stickers):
        cid = str(st.get("custom_emoji_id") or "")
        if family == "coins":
            tickers = prov.get(cid, [])
            rows.append(_row(i, st, is_logo=False,
                             name=", ".join(tickers) or None, sources=[]))
        else:
            info = prov.get(cid, {})
            # Slot 1 is the brand logo: no catalog row, so nothing to join.
            is_logo = i == 0 and bool(rec.get("logo", True)) and not info
            rows.append(_row(i, st, is_logo=is_logo,
                             name="brand logo" if is_logo else info.get("name"),
                             sources=info.get("source_emoji_ids")))
    return {
        "set_name": name,
        "title": rec.get("title"),
        "family": family,
        "pack_index": rec.get("index"),
        "link": f"https://t.me/addemoji/{name}",
        "count": len(rows),
        "captured_utc": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "emoji": rows,
    }


def _zero_note(doc: dict) -> str:
    """What `#` 0 means here. The coin bot is exempt from the brand logo, so
    saying "emoji 0 is the logo" would be false for 29 of the 34 packs."""
    if doc["family"] == "general":
        return ("`#` counts from 0 and emoji 0 is the brand logo; `slot` is the "
                "position Telegram shows.")
    return ("`#` counts from 0; `slot` is the position Telegram shows. This "
            "family carries no brand logo, so emoji 0 is a real coin.")


def render_markdown(doc: dict) -> str:
    """The same rows a human can scan. Kept beside the JSON deliberately: one
    reader edits neither, and two files that disagree are worse than none."""
    head = [
        f"# {doc.get('title') or doc['set_name']}",
        "",
        f"- **Set** `{doc['set_name']}`",
        f"- **Link** {doc['link']}",
        f"- **Family** {doc['family']}  |  **Pack** {doc.get('pack_index')}  "
        f"|  **Emoji** {doc['count']}",
        f"- **Captured** {doc['captured_utc']} (live from Telegram)",
        "",
        _zero_note(doc) + " *Was* is the id this emoji had in the pack it was "
        "taken from, empty when we made it ourselves.",
        "",
        "| # | slot | custom_emoji_id | glyph | format | name | was |",
        "|--:|-----:|-----------------|-------|--------|------|-----|",
    ]
    for e in doc["emoji"]:
        was = ", ".join(e["source_emoji_ids"])
        nm = (e["name"] or "").replace("|", "\\|")
        head.append(f"| {e['index']} | {e['slot']} | `{e['custom_emoji_id']}` | "
                    f"{e['glyph'] or ''} | {e['format']} | {nm} | "
                    f"{('`' + was + '`') if was else ''} |")
    return "\n".join(head) + "\n"


def render_index_markdown(index: dict) -> str:
    lines = [
        "# Published emoji packs",
        "",
        f"{index['pack_count']} packs, {index['emoji_count']} emoji. "
        f"Captured {index['captured_utc']} live from Telegram.",
        "",
        "Each pack has three files named after the set: `.md` to read, `.json` to "
        "parse, and `.html` to LOOK at -- one self-contained page with every "
        "thumbnail inline (animation included) and the same roster repeated in a "
        "`<script type=\"application/json\">` block. "
        "`index.json` also carries two flat lookups: `by_current_id` (id -> where it "
        "lives now) and `by_source_id` (the id an emoji had in its original pack -> "
        "ours).",
        "",
        "| Pack | Set | Emoji | Link |",
        "|------|-----|------:|------|",
    ]
    for p in index["packs"]:
        lines.append(f"| {p['family']} {p.get('pack_index')} | `{p['set_name']}` | "
                     f"{p['count']} | {p['link']} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Freshness
# --------------------------------------------------------------------------- #
def fingerprint() -> dict:
    """Cheap signature of every input that can change a roster.

    Size+mtime, not a content hash: this runs from a Stop hook, where a hash of
    a 600 KB catalog on every turn is a cost with no matching benefit -- any
    edit to these files moves both numbers.
    """
    out = {}
    for p in (CATALOG, GENERAL_STATE, COINS_STATE, TICKER_MAP):
        try:
            s = p.stat()
            out[p.name] = [s.st_size, int(s.st_mtime)]
        except OSError:
            out[p.name] = None
    return out


def check_stale() -> tuple[bool, str]:
    """(stale, why). Never touches the network -- the hook calls this."""
    idx = OUT_DIR / "index.json"
    if not idx.is_file():
        return True, "packs/index.json does not exist yet"
    try:
        recorded = json.loads(idx.read_text(encoding="utf-8")).get("inputs")
    except (OSError, ValueError) as exc:
        return True, f"packs/index.json is unreadable ({exc})"
    now = fingerprint()
    if recorded != now:
        changed = [k for k in now if recorded is None or recorded.get(k) != now[k]]
        return True, "changed since the roster was written: " + ", ".join(changed)
    return False, "roster matches its inputs"


# --------------------------------------------------------------------------- #
def refresh(family: str) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, dict, Telegram]] = []

    if family in ("all", "general"):
        token = os.environ.get("GENERAL_BOT_TOKEN")
        if not token:
            log.error("GENERAL_BOT_TOKEN is not set; cannot read the general packs.")
            return EXIT_FAILED
        tg = Telegram(token)
        jobs += [("general", r, tg) for r in _sets(GENERAL_STATE) if r.get("name")]
    if family in ("all", "coins"):
        # The coin packs belong to a DIFFERENT bot. A set belongs to the bot that
        # created it, so the general token answers STICKERSET_INVALID here.
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            log.error("TELEGRAM_BOT_TOKEN is not set; cannot read the coin packs.")
            return EXIT_FAILED
        tg = Telegram(token)
        jobs += [("coins", r, tg) for r in _sets(COINS_STATE) if r.get("name")]
    if not jobs:
        log.error("no published sets found for family %r", family)
        return EXIT_FAILED

    live_ids: set[str] = set()
    fetched: list[tuple[str, dict, dict]] = []
    for fam, rec, tg in jobs:
        try:
            ss = tg.get_sticker_set(rec["name"])
        except Exception as exc:                      # noqa: BLE001 - one dead set must not lose the rest
            log.error("%s: cannot read the live set: %s", rec["name"], exc)
            return EXIT_FAILED
        fetched.append((fam, rec, ss))
        live_ids.update(str(s.get("custom_emoji_id")) for s in ss["stickers"])

    gen_prov = general_provenance(live_ids)
    coin_prov = coin_provenance()
    gen_art = general_art()
    coin_dir = coin_art_dir()

    def art_for(fam):
        if fam == "general":
            def gen_path(e):
                # The brand logo has no catalog row -- it is inserted at publish
                # time, never ingested -- so the catalog cannot supply its art
                # either, and the page showed "no preview" on the one row the
                # owner cares most about. It ships in the repo; use it.
                if e["role"] == "brand-logo":
                    return Path(BRAND_LOGO_DEFAULT)
                return gen_art.get(e["custom_emoji_id"])
            return gen_path
        def coin_path(e):
            # The roster's name for a coin IS its ticker, and the corpus is
            # <ticker>.png. A shared logo lists several tickers; any of them
            # resolves to the same picture, so the first is enough.
            if not coin_dir or not e["name"]:
                return None
            return coin_dir / f"{e['name'].split(',')[0].strip()}.png"
        return coin_path

    packs, by_current, by_source = [], {}, {}
    for fam, rec, ss in fetched:
        prov = coin_prov if fam == "coins" else gen_prov
        doc = build_pack(_Prefetched(ss), rec, fam, prov, live_ids)
        write_json_atomic(OUT_DIR / f"{doc['set_name']}.json", doc)
        (OUT_DIR / f"{doc['set_name']}.md").write_text(render_markdown(doc), encoding="utf-8")
        (OUT_DIR / f"{doc['set_name']}.html").write_text(
            pack_gallery.render(doc, art_for(fam), THUMBS), encoding="utf-8")
        packs.append({k: doc[k] for k in ("set_name", "title", "family", "pack_index",
                                          "link", "count")})
        for e in doc["emoji"]:
            by_current[e["custom_emoji_id"]] = {"set": doc["set_name"], "slot": e["slot"],
                                                "index": e["index"]}
            for s in e["source_emoji_ids"]:
                by_source[s] = e["custom_emoji_id"]
        log.info("%s: %d emoji", doc["set_name"], doc["count"])

    index = {
        "captured_utc": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "pack_count": len(packs),
        "emoji_count": sum(p["count"] for p in packs),
        "packs": packs,
        "by_current_id": by_current,
        "by_source_id": by_source,
        # Written LAST and read by --check: the roster is only as fresh as the
        # inputs it was built from.
        "inputs": fingerprint(),
    }
    write_json_atomic(OUT_DIR / "index.json", index)
    (OUT_DIR / "README.md").write_text(render_index_markdown(index), encoding="utf-8")
    print(f"packs/: {index['pack_count']} packs, {index['emoji_count']} emoji, "
          f"{len(by_source)} source-id mappings, "
          f"{len(list(THUMBS.glob('*'))) if THUMBS.is_dir() else 0} cached thumbnails")
    return EXIT_OK


class _Prefetched:
    """Hands ``build_pack`` the set we already downloaded, so the estate is read
    exactly once even though the roster is built per pack."""

    def __init__(self, sset: dict):
        self._s = sset

    def get_sticker_set(self, _name: str) -> dict:
        return self._s


def main(argv: list[str] | None = None) -> int:
    load_env()
    setup_logging("pack_manifest")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh", action="store_true", help="Read the live packs and rewrite packs/.")
    ap.add_argument("--check", action="store_true",
                    help="Report whether packs/ still matches its inputs. No network.")
    ap.add_argument("--family", choices=("all", "general", "coins"), default="all")
    args = ap.parse_args(argv)

    if args.check == args.refresh:
        ap.error("pass exactly one of --refresh or --check")
    if args.check:
        stale, why = check_stale()
        print(("STALE: " if stale else "fresh: ") + why)
        return EXIT_STALE if stale else EXIT_OK
    return refresh(args.family)


if __name__ == "__main__":
    raise SystemExit(record_exit_code(main()))
