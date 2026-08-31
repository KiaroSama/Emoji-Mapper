"""Review (and precisely fix) coin logos against official CoinGecko art.

Coin tickers are shared by many tokens, so a logo fetched by symbol can be the
wrong coin (e.g. a green "SOL" token instead of Solana). This tool pulls the
top-N coins by market cap, compares each one's official logo to the local source
logo (``<symbol>.png``) with a perceptual hash, and lists candidates that differ.

IMPORTANT: a large perceptual distance does NOT prove a logo is wrong. Many
correct logos come from different icon sets (different art) than CoinGecko, so
they differ too. Treat the report as a REVIEW list for a human, not ground
truth. Fixing is therefore restricted to an explicit, user-confirmed ticker list
(``--fix --only sym1,sym2``) so correct logos are never clobbered.

Usage:
  python coins/verify_logos.py --emoji-dir "PATH/emoji" [--top 100]
  python coins/verify_logos.py --emoji-dir "PATH/emoji" --fix --only sol,xrp
"""

from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import contextlib
import io
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageOps

from build_pack import (EXIT_FAILED, EXIT_OK, EXIT_USAGE, ingest_exit_code, load_env, safe_int_env)
from packstate import (LockBusy, canonical_map_lock, exclusive_lock, make_intent, pack_family_lock_path, write_json_atomic)
from telegram_api import (AmbiguousUploadError, SetState, Telegram, _input_sticker, _mime_for_path)
from coins import _http
from emojikit import media
from emojikit.identity import _dhash, hamming
from emojikit.logsetup import setup_logging

ROOT = Path(__file__).resolve().parent
# The coin pack family, matching coins/fetch_paprika.py and coins/rebuild_dedup.py.
SET_BASE = "gvcryptoemoji"
# Keyed on the BASE NAME, exactly like the fetchers and the rebuild tool:
# --fix replaces stickers in the very sets they append to. A lock named after
# this script's own file (coin_pack.lock) was a DIFFERENT name from theirs, so
# the exclusion it advertised did not exist and a --fix could run concurrently
# with a top-up against the same live sets.
PACK_LOCK = pack_family_lock_path(SET_BASE)
log = logging.getLogger("verify_logos")
UA = {"User-Agent": "Mozilla/5.0 (logo-verify; local tool)"}
# A live sticker within this perceptual distance of the PNG we sent IS that
# upload: Telegram re-encodes PNG to WEBP, so identical content still differs by
# a bit or two. Same budget as the rebuild tool and the fetchers.
SAME_IMAGE_MAX = 8


def dh(img: Image.Image) -> int:
    return _dhash(img.convert("RGBA"))


def _intent_path() -> Path:
    """Where a replacement records itself BEFORE it is attempted.

    Resolved from ROOT per call, not at import, so a test or an alternate
    checkout redirects it with the rest of this module's paths.
    """
    return ROOT / "verify_logos_intent.json"


def _run_targets(map_path, state_path=None) -> dict:
    """The files this run is bound to, normalized for comparison.

    The intent file has ONE fixed location but --map and --state are chosen per
    run. A crash under ``--map A.json`` followed by a restart under
    ``--map B.json`` used to reconcile the pending replacement into B: a ticker
    is repointed inside a file that never held the old id, while A keeps naming
    a sticker that no longer exists. Recording the resolved targets is what lets
    the recovery refuse.

    normcase + resolve so the same file reached by a different spelling (case,
    a relative path, a symlink) still compares equal on Windows and POSIX.
    """
    def norm(p) -> str:
        return os.path.normcase(str(Path(p).resolve())) if p else ""

    return {"map_target": norm(map_path), "state_target": norm(state_path)}


def fetch_markets(top: int) -> list[dict]:
    out: list[dict] = []
    per = 250
    for page in range(1, (top + per - 1) // per + 1):
        url = (f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
               f"&order=market_cap_desc&per_page={per}&page={page}&sparkline=false")
        # Through the shared pooled client: this had no retry at all, so a
        # single 429 on page 2 of 4 ended the whole review with a traceback.
        resp = _http.get(url, headers=UA, timeout=60)
        if resp is None:
            raise RuntimeError(f"CoinGecko markets page {page} is unavailable")
        out.extend(resp.json())
        time.sleep(3)
    return out[:top]


def fetch_image(url: str) -> bytes | None:
    resp = _http.get(url, headers=UA, retries=3, backoff=2.0, max_backoff=6.0)
    if resp is None:
        log.debug("image fetch failed %s", url)
        return None
    return resp.content


def logo_distance(ours: Image.Image, theirs: Image.Image) -> int:
    """How far OUR logo is from the reference, ignoring deliberate restyling.

    A plain dHash comparison answers the wrong question here. This project's
    emoji are drawn light-on-transparent so they read on Telegram's dark chat
    background, while the reference images are dark-on-light. dHash is
    inversion-sensitive, so the SAME mark scores as maximally different: a
    review sweep of the top 1000 coins flagged 237 of them, and every one of the
    twelve worst collapsed from ~50 to ~12 once the comparison accounted for it
    (xrp 52 -> 12, bora 50 -> 10, xdai 47 -> 9). A check that reports a
    deliberate style choice as a wrong logo is a check nobody can act on.

    Comparing all three renderings and keeping the closest leaves the flag
    meaning what it says: the artwork is of something else.
    """
    variants = [ours]
    with contextlib.suppress(Exception):
        variants.append(ImageOps.invert(ours.convert("RGB")))
    with contextlib.suppress(Exception):
        # A transparent mark flattened onto white, as the reference renders it.
        rgba = ours.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[-1])
        variants.append(flat)
    want = dh(theirs)
    return min(hamming(dh(v), want) for v in variants)


def report(emoji_dir: Path, top: int, threshold: int) -> list[tuple[int, str, str, str]]:
    coins = fetch_markets(top)
    log.info("fetched %d market coins", len(coins))
    flagged: list[tuple[int, str, str, str]] = []
    for c in coins:
        sym = str(c.get("symbol", "")).lower()
        local = emoji_dir / f"{sym}.png"
        if not sym or not local.is_file():
            continue
        data = fetch_image(c.get("image", ""))
        if not data:
            continue
        try:
            d = logo_distance(Image.open(local), Image.open(io.BytesIO(data)))
        except Exception:  # noqa: BLE001
            continue
        if d > threshold:
            flagged.append((d, sym, str(c.get("name", "")), str(c.get("image", ""))))
        time.sleep(0.05)
    flagged.sort(reverse=True)
    log.info("flagged %d / %d coins (distance > %d) -- REVIEW ONLY, not proof of error:",
             len(flagged), len(coins), threshold)
    for d, sym, name, _ in flagged:
        log.info("  %-8s d=%-4d %s", sym, d, name)
    return flagged


def _cids(sset: dict) -> list[str]:
    """The custom_emoji_id of every sticker in a set, in order."""
    return [str(st.get("custom_emoji_id")) for st in sset.get("stickers", [])]


def cid_location(tg: Telegram, sets: list[dict],
                 cid: str) -> tuple[str, int, str, list[str]] | None:
    """Return (set_name, position, file_id, cids_before) of a custom_emoji_id.

    The full BEFORE snapshot comes back with it because it is the only way to
    prove afterwards which sticker the replacement actually became.
    """
    for s in sets:
        sset = tg.get_sticker_set(s["name"])
        cids = _cids(sset)
        for pos, st in enumerate(sset.get("stickers", [])):
            if cids[pos] == str(cid):
                return s["name"], pos, st["file_id"], cids
    return None


def _replaced_check(tg: Telegram, sname: str, old_cid: str):
    """applied_check for replaceStickerInSet: has the old sticker gone away?

    replaceStickerInSet is not safe to blind-retry. A response lost after
    Telegram applied the change leaves an ``old_sticker`` that no longer
    exists, so the retry fails against a set that is in fact already correct --
    and with a file handle as the body it would upload nothing anyway.
    """
    def check():
        state, sset = tg.probe_set_state(sname)
        if state is not SetState.EXISTS:
            return None                  # unknown or vanished: reconcile, don't guess
        return old_cid not in _cids(sset)

    return check


def verified_new_cid(before: list[str], after: list[str], pos: int) -> str | None:
    """The replacement's custom_emoji_id, or None when identity is unprovable.

    ``after[pos]`` is our replacement only if the set is otherwise untouched.
    If anything else was added or removed meanwhile, position ``pos`` now holds
    an unrelated emoji -- and every map entry that shared the old id would be
    repointed at the wrong picture, permanently and silently.
    """
    if len(after) != len(before) or not 0 <= pos < len(after):
        return None
    if any(a != b for i, (a, b) in enumerate(zip(before, after, strict=True))
           if i != pos):
        return None
    new_cid = after[pos]
    # A genuine replacement carries a NEW id: unchanged means nothing happened,
    # and an id already present elsewhere means we are reading someone else's.
    if not new_cid or new_cid == "None" or new_cid in before:
        return None
    return new_cid


def is_our_image(tg: Telegram, sticker: dict, want: int) -> bool:
    """Does this live sticker actually carry the image we prepared?

    The cid list can look exactly right while position ``pos`` holds someone
    else's art -- structure is not identity. Trusting it repoints every ticker
    that shared the old id at that stranger, permanently and silently.
    """
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "live.png"
        try:
            tg.download_file(str(sticker.get("file_id")), dest)
            return hamming(dh(Image.open(dest)), int(want)) <= SAME_IMAGE_MAX
        except Exception as exc:  # noqa: BLE001
            log.error("cannot read the replacement sticker to prove it: %s", exc)
            return False


def _repoint_locked(map_path: Path, old_cid: str, new_cid: str) -> int:
    """The read-modify-write itself. The CALLER must hold the map lock.

    Split out because ``exclusive_lock`` is not reentrant: fix_one holds
    canonical_map_lock() across its whole read-choose-replace-repoint span, so
    calling the wrapper below from inside it would raise LockBusy against a
    lock this very run already owns.
    """
    mp = json.loads(map_path.read_text(encoding="utf-8"))
    changed = 0
    for t, c in list(mp.items()):
        if str(c) == old_cid:
            mp[t] = new_cid
            changed += 1
    if changed:
        write_json_atomic(map_path, mp)
    return changed


def repoint(map_path: Path, old_cid: str, new_cid: str) -> int:
    """Move every ticker on ``old_cid`` to ``new_cid``, as ONE locked
    read-modify-write of the canonical map.

    Re-reading under the lock is what makes this idempotent: a reconcile that
    runs after a crash finds nothing left on the old id and changes nothing,
    and a concurrent writer of the map cannot lose this edit (or have its own
    lost).

    For callers that do NOT already hold the lock -- reconcile_intent chooses
    its id from the intent file and the live pack, never from the map, so a
    single locked write is all it needs.
    """
    with canonical_map_lock():
        return _repoint_locked(map_path, old_cid, new_cid)


def reconcile_intent(tg: Telegram, map_path: Path, state_path=None) -> bool:
    """Resolve a replacement recorded before the last mutation. True when clear.

    Telegram can apply the replacement and still leave us without the
    confirmation read (a dropped connection, a killed process). The canonical
    map then still names the OLD cid -- which no longer exists -- so no later
    run can locate it and the ticker is stranded on a dead sticker forever.
    The intent written before the mutation is what makes that recoverable.

    Recoverable INTO THE FILE IT WAS RECORDED AGAINST, and no other: an intent
    whose target is different from -- or missing for -- this run's --map/--state
    is refused rather than applied to the wrong map.
    """
    path = _intent_path()
    if not path.is_file():
        return True
    try:
        intent = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("replacement intent %s is unreadable (%s); resolve it by hand "
                  "before mutating the packs again.", path.name, exc)
        return False

    want = _run_targets(map_path, state_path)
    got = {key: str((intent or {}).get(key) or "") for key in want}
    if got != want:
        log.error("the pending replacement in %s was recorded against map %s / "
                  "state %s, not this run's map %s / state %s. Refusing to "
                  "reconcile it into a different map -- re-run with the "
                  "original --map/--state, or resolve %s by hand.", path.name,
                  got["map_target"] or "<unrecorded>",
                  got["state_target"] or "<unrecorded>",
                  want["map_target"], want["state_target"] or "<none>",
                  path.name)
        return False

    try:
        sname = str(intent["set_name"])
        pos = int(intent["set_index"])
        old_cid = str(intent["old_cid"])
        before = [str(c) for c in intent["before"]]
        want_hash = int(intent["source_dhash"])
    except (ValueError, KeyError, TypeError) as exc:
        log.error("replacement intent %s is unreadable (%s); resolve it by hand "
                  "before mutating the packs again.", path.name, exc)
        return False

    try:
        sset = tg.get_sticker_set(sname)
    except RuntimeError as exc:
        log.error("cannot read %s to resolve the pending replacement of %s: %s",
                  sname, old_cid, exc)
        return False
    after = _cids(sset)
    if old_cid in after:
        # The mutation never applied: nothing is pending and nothing is stale.
        log.info("pending replacement of %s never applied; nothing to recover.",
                 old_cid)
        path.unlink(missing_ok=True)
        return True

    new_cid = verified_new_cid(before, after, pos)
    if new_cid is None or not is_our_image(tg, sset["stickers"][pos], want_hash):
        log.error("pending replacement of %s in %s cannot be proven, and %s "
                  "still names an id that is gone. Review the pack, repair the "
                  "map, then delete %s.", old_cid, sname, map_path.name,
                  path.name)
        return False
    changed = repoint(map_path, old_cid, new_cid)
    path.unlink(missing_ok=True)
    log.info("recovered pending replacement %s -> %s (%d map entries)",
             old_cid, new_cid, changed)
    return True


def fix_one(tg: Telegram, uid: int, sets: list[dict], map_path: Path,
            emoji_dir: Path, sym: str, state_path=None) -> bool:
    """Replace one ticker's sticker with the official CoinGecko logo."""
    # Resolve official image for this exact symbol via market lookup.
    coins = fetch_markets(250)
    coin = next((c for c in coins if str(c.get("symbol", "")).lower() == sym), None)
    if not coin:
        log.warning("%s: not found in top markets; skip", sym)
        return False
    data = fetch_image(coin["image"])
    if not data:
        log.warning("%s: official image fetch failed; skip", sym)
        return False

    # ONE acquisition around choosing the id and repointing it. The map read
    # used to be unlocked: a map-only writer (alias_map, enhance_map,
    # remap_ids --apply) could repoint the ticker in the window, so this run
    # replaced -- destroyed -- the live sticker for the id it had read while
    # the map already named another one, and the repoint below then found no
    # entry on the old id at all. The whole span has to be inside the lock
    # because it is the span in which the id must not change.
    #
    # Yes, that holds the map lock across a Telegram round trip. Acceptable
    # HERE: --fix is an operator-driven repair of an explicit short ticker
    # list, not a batch job, and PACK_LOCK is already held for its whole
    # duration anyway -- so the only tools this can delay are the other
    # hand-run map editors, which fail fast with LockBusy and are re-run.
    # PACK_LOCK first, canonical_map_lock() second: the project-wide order.
    with canonical_map_lock():
        try:
            old_cid = str(json.loads(map_path.read_text(encoding="utf-8")).get(sym, ""))
        except (OSError, ValueError) as exc:
            log.error("%s: cannot read %s: %s", sym, map_path.name, exc)
            return False
        loc = cid_location(tg, sets, old_cid)
        if not loc:
            log.warning("%s: current cid %s not found live; skip", sym, old_cid)
            return False
        sname, pos, old_fid, before = loc

        # Update local source files (full-res + 100x100) and upload the replacement.
        png_dir = emoji_dir.parent / "png"
        png_dir.mkdir(exist_ok=True)
        (png_dir / f"{sym}.png").write_bytes(data)
        tmp = ROOT / "_vtmp.png"
        tmp.write_bytes(data)
        src = emoji_dir / f"{sym}.png"
        media.to_static_png(tmp, src)
        tmp.unlink(missing_ok=True)

        # Record WHAT is about to change, and everything needed to prove
        # afterwards what it became, BEFORE the mutation. Written first because
        # the dangerous window opens the moment the request leaves. A
        # replacement targets a POSITION inside one set rather than a set
        # number, so set_index carries that position; reconcile_intent reads it
        # back the same way.
        intent = make_intent(key=sym, operation="replace", set_name=sname,
                             set_index=pos, expected_before=len(before))
        intent.update({"old_cid": old_cid, "before": before,
                       "source": str(src), "source_dhash": dh(Image.open(src)),
                       # Which map/state this replacement belongs to. The intent
                       # file's own path is fixed; its target is not.
                       **_run_targets(map_path, state_path)})
        write_json_atomic(_intent_path(), intent)

        # Immutable bytes, not an open handle: _call retries the POST, and a
        # file object is exhausted after the first attempt -- every retry
        # silently uploaded an empty body. The applied_check makes those retries
        # safe at all.
        try:
            tg._call("replaceStickerInSet", data={
                "user_id": uid, "name": sname, "old_sticker": old_fid,
                "sticker": json.dumps(_input_sticker("static", ["\U0001FA99"],
                                                     [sym, str(coin.get("name", "")).lower()])),
            }, files={"file0": (src.name, src.read_bytes(), _mime_for_path(src))},
                applied_check=_replaced_check(tg, sname, old_cid))
        except AmbiguousUploadError as exc:
            # May or may not be live; the postcondition read below is the
            # decider, and the intent survives if that read never comes back.
            log.warning("%s: %s", sym, exc)
        except RuntimeError as exc:
            # _call raises RuntimeError only once the change is verified NOT
            # applied.
            _intent_path().unlink(missing_ok=True)
            log.error("%s: replaceStickerInSet failed: %s", sym, exc)
            return False

        # Postcondition: prove WHICH sticker is the replacement before trusting it.
        try:
            sset = tg.get_sticker_set(sname)
        except RuntimeError as exc:
            log.error("%s: cannot re-read %s to confirm the replacement: %s. The "
                      "intent is kept; the next run resolves it.", sym, sname, exc)
            return False
        after = _cids(sset)
        if old_cid in after:
            _intent_path().unlink(missing_ok=True)
            log.error("%s: %s is still live in %s; the replacement did not apply.",
                      sym, old_cid, sname)
            return False
        new_cid = verified_new_cid(before, after, pos)
        if new_cid is None or not is_our_image(tg, sset["stickers"][pos],
                                               intent["source_dhash"]):
            log.error("%s: cannot prove what replaced %s in %s (the set changed "
                      "underneath us, or position %d does not hold our image). Map "
                      "left untouched -- check the pack, then re-run.",
                      sym, old_cid, sname, pos)
            return False
        # Repoint first, clear second: a crash in between leaves an intent whose
        # reconcile finds nothing left to move and simply clears it.
        changed = _repoint_locked(map_path, old_cid, new_cid)
    _intent_path().unlink(missing_ok=True)
    log.info("fixed %s: %s -> %s (%d map entries)", sym, old_cid, new_cid, changed)
    return True


def main() -> int:
    load_env()
    setup_logging("verify_logos")
    ap = argparse.ArgumentParser()
    ap.add_argument("--emoji-dir", required=True)
    ap.add_argument("--token-env", default="TELEGRAM_BOT_TOKEN")
    ap.add_argument("--map", default=str(ROOT / "ticker_to_id.json"))
    ap.add_argument("--state", default=str(ROOT / "rebuild_dedup_state.json"))
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--threshold", type=int, default=20, help="Review-flag distance.")
    ap.add_argument("--fix", action="store_true", help="Fix the --only tickers.")
    ap.add_argument("--only", default="", help="Comma-separated tickers to fix (required with --fix).")
    args = ap.parse_args()

    emoji_dir = Path(args.emoji_dir)
    if not emoji_dir.is_dir():
        log.error("emoji dir not found: %s", emoji_dir)
        return EXIT_USAGE

    if not args.fix:
        report(emoji_dir, args.top, args.threshold)
        log.info("Review only. To repair confirmed-wrong logos: --fix --only sol,xrp")
        return EXIT_OK

    syms = [s.strip().lower() for s in args.only.split(",") if s.strip()]
    if not syms:
        log.error("--fix requires --only with an explicit ticker list "
                  "(automated detection is not reliable enough to mass-fix).")
        return EXIT_USAGE

    # Raw os.environ[...] / int(...) turned an unset or mistyped variable into a
    # KeyError/ValueError traceback -- after argparse had already accepted the
    # run -- instead of the usage error every other bad input produces here.
    token = os.environ.get(args.token_env, "")
    if not token:
        log.error("%s is not set (env or .env).", args.token_env)
        return EXIT_USAGE
    uid = safe_int_env("PACK_OWNER_USER_ID", 0, minimum=0)
    if not uid:
        log.error("PACK_OWNER_USER_ID must be set to your numeric Telegram "
                  "user id.")
        return EXIT_USAGE

    tg = Telegram(token)
    map_path = Path(args.map)
    state_path = Path(args.state)
    def read_sets() -> list[dict]:
        """The pack family's current set list, and the map is parseable."""
        state = json.loads(state_path.read_text(encoding="utf-8"))
        json.loads(map_path.read_text(encoding="utf-8"))
        return sorted(state["sets"], key=lambda s: s["index"])

    try:
        read_sets()                 # fail on bad input BEFORE waiting for a lock
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.error("cannot read --state %s / --map %s: %s",
                  args.state, args.map, exc)
        return EXIT_USAGE

    fixed = 0
    try:
        # Replacing a sticker mutates the same pack family the fetchers append
        # to; two runs at once corrupt both the pack and the map.
        with exclusive_lock(PACK_LOCK):
            # RE-READ the state now the lock is held. The check above ran before
            # the wait, and a rebuild that finished during it deletes the old
            # packs and writes a new set list -- so the pre-lock snapshot names
            # sets that no longer exist while the map already points at the new
            # family. cid_location() would then search deleted packs and report
            # the ticker as missing. Lock order is unchanged: PACK_LOCK here,
            # canonical_map_lock inside fix_one/repoint.
            try:
                sets = read_sets()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.error("--state %s became unreadable while waiting for the "
                          "pack lock: %s. Nothing was changed.", args.state, exc)
                return EXIT_FAILED
            # An unresolved replacement must be settled before anything else is
            # mutated: the next fix would overwrite the only record of it.
            if not reconcile_intent(tg, map_path, state_path):
                log.error("refusing to replace anything while a previous "
                          "replacement is unresolved.")
                return EXIT_FAILED
            for sym in syms:
                if fix_one(tg, uid, sets, map_path, emoji_dir, sym, state_path):
                    fixed += 1
                time.sleep(0.3)
    except LockBusy as exc:
        log.error("%s", exc)
        return EXIT_FAILED
    log.info("fixed %d/%d requested logos; map saved.", fixed, len(syms))
    # An explicitly requested fix that did not happen is not a success: exiting
    # 0 told the launcher and CI that every listed logo had been repaired.
    unfixed = len(syms) - fixed
    if unfixed:
        log.error("%d requested logo(s) were NOT fixed; see the warnings above.",
                  unfixed)
    return ingest_exit_code(fixed, unfixed)


if __name__ == "__main__":
    raise SystemExit(main())
