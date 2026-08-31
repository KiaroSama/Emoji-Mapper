"""The canonical map phase: live custom-emoji ids back to tickers.

Runs after the packs are built. Resolution is by IMAGE, never by position:
a positional walk once wrote a scrambled ticker->id map, and
``MapIdentityUnproven`` exists so an unprovable match stops the run instead
of being guessed.
"""

from __future__ import annotations

# This module lives in coins/; allow importing the shared engine from the
# project root.
import os as _bootstrap_os
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import json
import tempfile
from collections import defaultdict
from pathlib import Path

from PIL import Image

from announce import announce_packs
from coins._inventory import refill_inventory
from emojikit.identity import _dhash, hamming
from packstate import (canonical_map_lock, exclusive_lock,
                       write_json_atomic)
from telegram_api import (Telegram)
from coins import _dedup_plan as cfg
from coins._dedup_plan import (load_plan, load_state, save_state)


BACKUP_IDS = cfg.ROOT / "ticker_to_id.bak.json"


def reapply_aliases(new_map: dict[str, str]) -> int:
    """Re-apply chain/name aliases that have no own PNG (e.g. bnbbsc->bnb).

    The old ticker_to_id (backup) recorded these aliases by pointing the alias
    ticker at the SAME cid as its base coin. After the rebuild the base coin gets
    a NEW cid; map every PNG-less alias onto a sibling that IS in the new map.
    """
    if not BACKUP_IDS.is_file():
        print("  no backup map; skipping alias re-apply.", flush=True)
        return 0
    old = json.loads(BACKUP_IDS.read_text(encoding="utf-8"))
    old_groups: dict[str, list[str]] = defaultdict(list)
    for t, c in old.items():
        old_groups[str(c)].append(t)
    added = 0
    for t, c in old.items():
        if t in new_map:
            continue
        for sib in old_groups.get(str(c), []):
            if sib in new_map:
                new_map[t] = new_map[sib]
                added += 1
                break
    print(f"  re-applied {added} aliases from backup.", flush=True)
    return added


def approved_shared_tickers() -> set[str]:
    """Tickers reviewed as legitimately sharing a logo (shared_logo_groups.json)."""
    if not cfg.GROUPS_REPORT.is_file():
        return set()
    try:
        groups = json.loads(cfg.GROUPS_REPORT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {t for members in groups.values() for t in members}


def unapproved_shared_groups(mapping: dict[str, str]) -> dict[str, list[str]]:
    """Emoji ids claimed by several tickers that were never reviewed as shared.

    Two coins may legitimately share one image (Tether on six chains), and those
    groups are recorded in shared_logo_groups.json. Anything else pointing many
    tickers at one id is the signature of a mis-aligned mapping, not a shared
    logo -- and it is silent, because the map stays structurally valid.
    """
    approved = approved_shared_tickers()
    by_id: dict[str, list[str]] = defaultdict(list)
    for ticker, cid in mapping.items():
        by_id[str(cid)].append(ticker)
    return {cid: tickers for cid, tickers in by_id.items()
            if len(tickers) > 1 and not set(tickers) <= approved}


class MapIdentityUnproven(RuntimeError):
    """A live sticker could not be tied to a plan entry by image content."""


def resolve_by_image(tg: Telegram, live: list[tuple[str, str]],
                     order: list[str]) -> dict[str, str]:
    """``rep -> custom_emoji_id``, proven per sticker by IMAGE identity.

    Position is NOT identity, and neither is an equal count. The recorded upload
    order says what we sent; it says nothing about what is in the pack now. A
    same-length reorder or an after-the-fact replacement leaves ``live[i]``
    holding a different coin's art, and zipping ``order`` against it is exactly
    how the Solama memecoin llama ended up published as `sol`.

    Every live sticker must match exactly one recorded upload and every recorded
    upload exactly one live sticker; anything else raises, because there is no
    safe guess between "the pack changed" and "the map is fine".
    """
    want: dict[str, int] = {}
    for rep in order:
        png = cfg.EMOJI / f"{rep}.png"
        if not png.is_file():
            raise MapIdentityUnproven(
                f"the source image for {rep!r} is gone, so no live sticker can "
                f"be proven to be it")
        want[rep] = _dhash(Image.open(png).convert("RGBA"))

    rep_to_cid: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "live.png"
        for cid, file_id in live:
            try:
                tg.download_file(str(file_id), dest)
                got = _dhash(Image.open(dest).convert("RGBA"))
            except Exception as exc:
                # Unreadable is not "not ours": guessing here is what writes a
                # wrong id into the canonical map.
                raise MapIdentityUnproven(
                    f"live sticker {cid} could not be read ({exc})") from exc
            # ponytail: O(live x plan) hamming scan, a few million cheap int ops
            # for this pack family; bucket by hash prefix if it ever gets big.
            hits = [rep for rep, w in want.items()
                    if hamming(w, got) <= cfg.SAME_IMAGE_MAX]
            if len(hits) != 1:
                raise MapIdentityUnproven(
                    f"live sticker {cid} matches {len(hits)} uploaded image(s) "
                    f"{sorted(hits)[:4]}; it cannot be attributed to a coin")
            if hits[0] in rep_to_cid:
                raise MapIdentityUnproven(
                    f"{hits[0]!r} is live twice ({rep_to_cid[hits[0]]} and "
                    f"{cid}); the pack contains a duplicate")
            rep_to_cid[hits[0]] = cid

    missing = [rep for rep in order if rep not in rep_to_cid]
    if missing:
        raise MapIdentityUnproven(
            f"{len(missing)} recorded upload(s) have no live sticker "
            f"(e.g. {missing[:5]})")
    return rep_to_cid


def map_and_fill(tg: Telegram) -> None:
    """Rebuild the canonical map from live state, holding BOTH locks.

    In the documented project order: pack-family lock FIRST, canonical map lock
    SECOND. The live sets, the resume state and the map are one snapshot, and
    the WHOLE map is written back from it -- so a provider that appends a
    sticker and its map entry after the snapshot but before the write has that
    entry erased by this write. Reading everything inside the locks is what
    makes the snapshot describe the state actually being replaced.
    """
    with exclusive_lock(cfg.LOCK), canonical_map_lock():
        _map_and_fill(tg)


def _map_and_fill(tg: Telegram) -> None:
    plan = load_plan()
    state = load_state(plan)
    sets = sorted(state["sets"], key=lambda x: x["index"])
    live: list[tuple[str, str]] = []
    for s in sets:
        for st in tg._call("getStickerSet",
                           data={"name": s["name"]}).get("stickers", []):
            live.append((str(st.get("custom_emoji_id", "")),
                         str(st.get("file_id", ""))))

    order = state.get("order") or []
    candidate = cfg.ROOT / "ticker_to_id.candidate.json"
    if not order:
        write_json_atomic(candidate, {
            "error": "no uploads are recorded", "live_stickers": len(live),
            "plan_entries": len(plan)})
        raise SystemExit(
            f"ERROR: no uploads are recorded but {len(live)} stickers are "
            f"live.\n"
            f"       Refusing to write {cfg.TICKER_IDS.name} from positional "
            f"guesswork -- that is what corrupts the map.\n"
            f"       Rebuild identities from image content with "
            f"coins/remap_ids.py. Details: {candidate.name}")

    try:
        rep_to_cid = resolve_by_image(tg, live, order)
    except MapIdentityUnproven as exc:
        write_json_atomic(candidate, {
            "error": str(exc), "recorded_uploads": len(order),
            "live_stickers": len(live), "plan_entries": len(plan)})
        raise SystemExit(
            f"ERROR: {exc}.\n"
            f"       Refusing to write {cfg.TICKER_IDS.name}: equal counts are not "
            f"identity, and mapping by position is what put an unrelated logo "
            f"on a real ticker.\n"
            f"       Rebuild identities from image content with "
            f"coins/remap_ids.py. Details: {candidate.name}") from exc

    by_rep = {g["rep"]: g for g in plan}
    ticker_to_id: dict[str, str] = {}
    for rep, cid in rep_to_cid.items():
        g = by_rep.get(rep)
        if not g:
            continue
        for t in g["tickers"]:
            ticker_to_id[t] = cid
    print(f"mapped by verified image identity ({len(live)} stickers)", flush=True)

    reapply_aliases(ticker_to_id)

    bad = unapproved_shared_groups(ticker_to_id)
    oversized = {cid: ts for cid, ts in bad.items() if len(ts) > cfg.SHARED_GROUP_LIMIT}
    if oversized:
        biggest = max(oversized.values(), key=len)
        write_json_atomic(candidate, ticker_to_id)
        raise SystemExit(
            f"ERROR: one emoji id would be shared by {len(biggest)} unreviewed "
            f"tickers (e.g. {sorted(biggest)[:6]}).\n"
            f"       A group that large is the signature of a mis-aligned "
            f"mapping, not a shared logo. Refusing to overwrite "
            f"{cfg.TICKER_IDS.name}; review {candidate.name} instead.")
    if bad:
        print(f"  note: {len(bad)} shared-logo group(s) are not listed in "
              f"{cfg.GROUPS_REPORT.name} (largest {max(len(t) for t in bad.values())}). "
              f"Cross-chain variants of one asset are expected here; add them to "
              f"that file to silence this.", flush=True)

    # Every other writer of the canonical map (alias_map, enhance_map,
    # remap_ids --apply, verify_logos, the providers) takes the same lock, so a
    # concurrent read-modify-write there cannot silently discard this rebuild.
    # map_and_fill() already holds it across the whole snapshot; exclusive_lock
    # is not reentrant, so claiming it again here would fail the run outright.
    write_json_atomic(cfg.TICKER_IDS, ticker_to_id)
    print(f"mapped {len(ticker_to_id)} tickers across {len(sets)} sets "
          f"({len(live)} live stickers)", flush=True)
    fill_inventory(ticker_to_id)


def fill_inventory(ticker_to_id: dict[str, str]) -> None:
    filled, total = refill_inventory(ticker_to_id, cfg.INV, cfg.OUT_INV)
    print(f"inventory filled: {filled}/{total} -> {cfg.OUT_INV.name}", flush=True)


def send_final_links(tg: Telegram) -> None:
    # Same lock as build(): this is a read-modify-write of the rebuild state, so
    # running it beside a build would save a stale snapshot back over the
    # upload order recorded in the meantime.
    with exclusive_lock(cfg.LOCK):
        state = load_state()
        sets = sorted(state["sets"], key=lambda x: x["index"])
        if not sets:
            print("no sets to send.", flush=True)
            return
        # One call carrying every pack. The Worker splits it across messages
        # when the family outgrows Telegram's 4096-character limit; the single
        # hand-built string could only be rejected whole, losing every link.
        dest = announce_packs(
            tg, cfg.USER_ID,
            [{"name": s["name"], "title": str(s["index"])} for s in sets],
            bot="coin", note="\U0001F4E6 @YourBrand Crypto Emoji \u2014 all packs:",
            # One line per pack: 29 of them as titled cards is three screens of
            # scrolling, and this message is a link index, not an announcement.
            style="list")
        state["final_sent"] = True
        save_state(state)
        print(f"sent final links for {len(sets)} packs to {dest} (preview off).",
              flush=True)
