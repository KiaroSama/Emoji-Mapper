"""Rebuild ticker_to_id.json by matching live pack images to source logos.

Why this exists: the original mapping was built from the dedup *plan order*, but
the live packs contain images in a slightly different order (some plan items were
skipped/failed during upload, shifting every later position). As a result the
position-based ticker->custom_emoji_id table is misaligned from the first gap
onward, so many tickers point at the wrong sticker.

This tool ignores position entirely and re-derives the mapping from image
content: it downloads every live sticker once, computes a small perceptual
signature, and matches each local source logo (``<ticker>.png``) to the live
sticker whose image is most similar. The result is a corrected, content-based
ticker -> custom_emoji_id map. It NEVER modifies the Telegram packs.

Usage:
  # 1. dry run: prints the distance/margin distribution used to calibrate the cutoff
  python coins/remap_ids.py --emoji-dir "PATH/TO/emoji"
  # 2. apply, with a calibrated cutoff (required)
  python coins/remap_ids.py --emoji-dir "PATH/TO/emoji" --max-distance 150 --apply

``--apply`` refuses to overwrite the canonical map from incomplete or ambiguous
data and writes a reviewable candidate file instead.

Both phases run under the coin pack-family lock -- the same one the fetchers and
rebuild_dedup take before touching those sets -- because the live read has to
still describe the live packs at the moment the map is replaced. If a provider
run holds it this exits EXIT_FAILED without writing anything; re-run afterwards.

Resumable: live signatures are cached in remap_live_cache.json keyed by set name
plus a digest of that set's live sticker manifest, so an interrupted run
continues without re-downloading while an edited pack is still re-read.
"""

from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import base64
import hashlib
import io
import json
import logging
import os
import shutil
import time
from collections import Counter
from pathlib import Path

import requests
from PIL import Image

# numpy is used ONLY here, by nearest()'s blocked distance matrix -- nothing
# else in the first-party tree imports it. It is therefore an extra for the coin
# tools rather than a requirement of the emoji-pack builder, and someone who
# never runs this reconciliation should not be made to install the wheel.
try:
    import numpy as np
except ImportError as exc:
    raise SystemExit("coins/remap_ids.py needs numpy: "
                     "pip install -r requirements-coins.txt") from exc

from emojikit.build_pack import (EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, load_env)
from emojikit.packstate import (LockBusy, canonical_map_lock, exclusive_lock, pack_family_lock_path, write_json_atomic)
from emojikit.telegram_api import (Telegram, api_base)
from emojikit.logsetup import setup_logging

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("remap_ids")
SIG_PX = 16  # signature is a SIG_PX x SIG_PX RGB thumbnail (robust to re-encode)
# The pack family this tool reads. Keyed on the same base name as fetch_paprika,
# fetch_cmc, verify_logos --fix and rebuild_dedup, so it resolves to the SAME
# lock file those take before mutating these sets -- a lock of our own would
# exclude nobody.
SET_BASE = "gvcryptoemoji"
PACK_LOCK = pack_family_lock_path(SET_BASE)


def signature(img: Image.Image) -> np.ndarray:
    """Small thumbnail signature: RGB on black, PLUS the alpha silhouette.

    The alpha plane is not decoration. Compositing on black and keeping only RGB
    made this function BLIND to any mark drawn in black on transparency: it
    flattens to a uniformly black square, so its signature is all zeros and
    every such logo lands on the same point. 135 of the 5875 coin logos are
    exactly that -- Aptos, Arkham, NEAR, Worldcoin, Bittensor and friends all
    ship a black wordmark -- and they measured pairwise distance 0.0 while being
    six visibly different pictures.

    Carrying the silhouette makes the shape survive whatever the colour does.
    `verify_logos.logo_distance` learned the same lesson from the other
    direction (it also tries inverted and flattened-on-white); this is the
    matcher's half of it.
    """
    im = img.convert("RGBA")
    bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
    flat = Image.alpha_composite(bg, im).convert("RGB").resize(
        (SIG_PX, SIG_PX), Image.LANCZOS)
    alpha = im.getchannel("A").resize((SIG_PX, SIG_PX), Image.LANCZOS)
    return np.concatenate((
        np.frombuffer(flat.tobytes(), dtype=np.uint8),
        np.frombuffer(alpha.tobytes(), dtype=np.uint8),
    )).astype(np.float32)


# Length of one signature; also the cache's compatibility key. A cache written
# by an older, shorter signature cannot be compared with a new one -- and
# silently mixing the two would match coins against noise.
SIG_LEN = SIG_PX * SIG_PX * 4


def manifest_digest(stickers: list[dict]) -> str:
    """Identity of a live set: its ordered (custom_emoji_id, file_unique_id) pairs.

    The cache used to be keyed by the set INDEX alone, which marked a set "done"
    forever: a sticker replaced, appended or deleted afterwards was silently
    skipped and the run matched against a stale manifest.
    """
    ident = [[str(st.get("custom_emoji_id")), str(st.get("file_unique_id"))]
             for st in stickers]
    return hashlib.sha256(json.dumps(ident).encode("utf-8")).hexdigest()[:32]


def load_cache(path: Path) -> dict:
    """Cache layout: {"sets": {name: manifest digest}, "sigs": {cid: b64},
    "errors": {cid: reason}}.

    A legacy cache (``done_sets`` keyed by set index) keeps its signatures --
    they are content-derived and still valid -- but loses its completeness
    marks, so every set is re-verified against the live manifest once.
    """
    raw = {}
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
    sigs = raw.get("sigs", {})
    sets = raw.get("sets", {})
    # Signatures of a different length came from a different signature()
    # definition, so they describe the images by a different rule. Comparing
    # them against current ones is not "slightly stale", it is meaningless --
    # and it would fail as a silent mismatch, not an error. Drop them, and drop
    # the completeness marks with them so every set is re-read.
    stale = [c for c, b in sigs.items()
             if len(base64.b64decode(b)) != SIG_LEN]
    if stale:
        log.info("discarding %d cached signature(s) written by an older "
                 "signature format; those sets will be re-read", len(stale))
        for c in stale:
            sigs.pop(c, None)
        sets = {}
    return {"sets": sets, "sigs": sigs, "errors": raw.get("errors", {})}


def save_cache(path: Path, cache: dict) -> None:
    write_json_atomic(path, cache)


def download_live(tg: Telegram, token: str, sets: list[dict], cache: dict,
                  cache_path: Path) -> list[str]:
    """Cache one signature per live sticker (resumable).

    Returns the names of sets that are still INCOMPLETE, i.e. at least one
    sticker has no signature. A set is only recorded as complete -- and thereby
    skipped on the next run -- when every sticker in its current manifest was
    downloaded and analysed; marking it done regardless let a set be cached as
    finished with zero usable signatures.
    """
    sess = requests.Session()
    live_cids: set[str] = set()
    incomplete: list[str] = []
    for s in sets:
        sticks = tg.get_sticker_set(s["name"]).get("stickers", [])
        cids = [str(st.get("custom_emoji_id")) for st in sticks]
        live_cids.update(cids)
        digest = manifest_digest(sticks)
        if cache["sets"].get(s["name"]) == digest:
            continue
        # strict: this loop's whole job is to bind an id to the sticker at that
        # position, so a length disagreement must raise rather than silently
        # drop the tail into a shorter, plausible-looking cache.
        for pos, (cid, st) in enumerate(zip(cids, sticks, strict=True)):
            if cid in cache["sigs"]:
                continue
            try:
                fp = tg._call("getFile", data={"file_id": st["file_id"]})["file_path"]
                r = sess.get(f"{api_base()}/file/bot{token}/{fp}", timeout=40)
                r.raise_for_status()  # an error page is not an image
                sig = signature(Image.open(io.BytesIO(r.content)))
                cache["sigs"][cid] = base64.b64encode(sig.astype(np.uint8).tobytes()).decode()
                cache["errors"].pop(cid, None)
            except Exception as exc:  # noqa: BLE001
                # The file URL embeds the bot token, and requests puts the URL
                # in its exception text -- never store that raw.
                cache["errors"][cid] = tg._safe(exc)
                log.warning("set %s pos %d cid %s failed: %s",
                            s["name"], pos, cid, cache["errors"][cid])
            time.sleep(0.02)
        missing = [c for c in cids if c not in cache["sigs"]]
        if missing:
            incomplete.append(s["name"])
            log.warning("set %s: %d/%d stickers still have no signature (not cached "
                        "as complete; re-run to retry)", s["name"], len(missing), len(cids))
        else:
            cache["sets"][s["name"]] = digest
        save_cache(cache_path, cache)
        log.info("set %s: cached %d live signatures total", s["name"], len(cache["sigs"]))

    # Ids that are no longer live (deleted or replaced stickers) must stop being
    # match candidates, otherwise a ticker is mapped to a sticker that is gone.
    stale = set(cache["sigs"]) - live_cids
    if stale:
        for cid in stale:
            cache["sigs"].pop(cid, None)
            cache["errors"].pop(cid, None)
        save_cache(cache_path, cache)
        log.info("pruned %d cached ids that are no longer live", len(stale))
    return incomplete


def build_local(emoji_dir: Path) -> tuple[list[str], np.ndarray]:
    """Compute signatures for every local <ticker>.png source logo."""
    tickers, sigs = [], []
    for p in sorted(emoji_dir.glob("*.png")):
        try:
            sigs.append(signature(Image.open(p)))
            tickers.append(p.stem.lower())
        except Exception as exc:  # noqa: BLE001
            log.warning("local %s failed: %s", p.name, exc)
    return tickers, np.array(sigs, dtype=np.float32)


def nearest(local: np.ndarray, live: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For each local row: nearest live index, its squared L2 distance, and the
    runner-up squared distance (inf when there is only one candidate).

    The runner-up is what makes a match checkable: a logo that is nearly as
    close to a second live sticker is ambiguous, not a match.
    """
    ln2 = (local * local).sum(1)
    vn2 = (live * live).sum(1)
    out_idx = np.empty(local.shape[0], dtype=np.int64)
    out_d2 = np.empty(local.shape[0], dtype=np.float64)
    out_second = np.empty(local.shape[0], dtype=np.float64)
    # Chunk over local rows to keep the Gram matrix memory bounded.
    step = 512
    for i in range(0, local.shape[0], step):
        block = local[i:i + step]
        g = block @ live.T
        # |a-b|^2 expanded this way can go slightly negative on identical rows
        # (float cancellation); sqrt() of that is NaN, which silently poisons
        # every distance comparison below.
        d2 = np.maximum(ln2[i:i + step, None] - 2 * g + vn2[None, :], 0.0)
        rows = np.arange(block.shape[0])
        best = d2.argmin(1)
        out_idx[i:i + step] = best
        out_d2[i:i + step] = d2[rows, best]
        d2[rows, best] = np.inf  # mask the winner, then the min is the runner-up
        out_second[i:i + step] = d2.min(1)
    return out_idx, out_d2, out_second


def main() -> int:
    load_env()
    setup_logging("remap_ids")
    ap = argparse.ArgumentParser()
    ap.add_argument("--emoji-dir", required=True, help="Folder of <ticker>.png source logos.")
    ap.add_argument("--token-env", default="TELEGRAM_BOT_TOKEN")
    ap.add_argument("--state", default=str(ROOT / "rebuild_dedup_state.json"))
    ap.add_argument("--cache", default=str(ROOT / "remap_live_cache.json"))
    ap.add_argument("--out", default=str(ROOT / "ticker_to_id.json"))
    ap.add_argument("--candidates", default=str(ROOT / "ticker_to_id.candidate.json"),
                    help="Where a refused --apply writes its reviewable result.")
    ap.add_argument("--max-distance", type=float, default=None,
                    help="Reject matches farther than this. REQUIRED with --apply; "
                         "run without --apply first to calibrate it from the "
                         "reported distance distribution.")
    ap.add_argument("--min-margin", type=float, default=None,
                    help="Reject a match whose runner-up is closer than this "
                         "(default: --max-distance). Guards against mapping a "
                         "coin onto whichever of two near-identical stickers "
                         "happened to win by a hair.")
    ap.add_argument("--apply", action="store_true", help="Write the corrected map.")
    args = ap.parse_args()

    emoji_dir = Path(args.emoji_dir)
    if not emoji_dir.is_dir():
        log.error("emoji dir not found: %s", emoji_dir)
        return EXIT_USAGE
    token = os.environ.get(args.token_env, "")
    if not token:
        log.error("%s not set.", args.token_env)
        return EXIT_USAGE
    if args.apply and not (args.max_distance and args.max_distance > 0):
        log.error("--apply needs a calibrated --max-distance > 0; run without "
                  "--apply first and pick a cutoff from the reported distances.")
        return EXIT_USAGE

    # The pack-family lock is held across the WHOLE run -- the live read, the
    # matching and the write -- not just the write. --apply replaces the map
    # wholesale, so serialising the write alone still writes a value computed
    # from a pack that has moved since: a provider appending a sticker AND its
    # map entry in that window had its entry erased by our replacement, even
    # though both writes were "locked". Under this lock the provider either
    # finishes first (its sticker is in our live read and gets matched by
    # content) or waits until after our write and adds to the fresh map.
    # Merging instead of replacing is NOT the fix: --apply exists to throw a
    # corrupted map away, and a merge would carry that corruption straight back.
    #
    # LOCK ORDER, project-wide: this pack-family lock FIRST, canonical_map_lock()
    # (inside _remap) SECOND, never the reverse. exclusive_lock is not reentrant,
    # and _remap takes no pack lock of its own.
    try:
        with exclusive_lock(PACK_LOCK):
            return _remap(args, token)
    except LockBusy as exc:
        log.error("%s", exc)
        return EXIT_FAILED


def _remap(args: argparse.Namespace, token: str) -> int:
    """Match live stickers to local logos; with --apply, replace the map.

    Runs entirely inside the pack-family lock main() takes, so the live pack
    state this reads is still the live pack state when the map is written.
    """
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    sets = sorted(state["sets"], key=lambda s: s["index"])
    tg = Telegram(token)
    log.info("bot @%s; %d sets", tg.get_me()["username"], len(sets))

    cache_path = Path(args.cache)
    cache = load_cache(cache_path)
    incomplete = download_live(tg, token, sets, cache, cache_path)

    cids = list(cache["sigs"].keys())
    tickers, local = build_local(Path(args.emoji_dir))
    if not tickers or not cids:
        log.error("nothing to match: %d local logos, %d live signatures",
                  len(tickers), len(cids))
        return EXIT_FAILED
    live = np.array([np.frombuffer(base64.b64decode(cache["sigs"][c]), dtype=np.uint8)
                     for c in cids], dtype=np.float32)
    log.info("matching %d local logos against %d live stickers ...", len(tickers), len(cids))
    idx, d2, d2_second = nearest(local, live)
    dists = np.sqrt(d2)
    margins = np.sqrt(d2_second) - dists

    # Distance distribution helps choose a cutoff that separates real matches
    # (deduped siblings included) from coins that were never uploaded.
    for thr in (50, 100, 150, 200, 300, 500):
        log.info("  matches with distance > %d: %d", thr, int((dists > thr).sum()))

    max_d = args.max_distance
    margin_min = args.min_margin if args.min_margin is not None else max_d
    new_map: dict[str, str] = {}
    far: list[tuple[str, float]] = []
    ambiguous: list[tuple[str, float, float]] = []
    # strict: four parallel arrays out of the matching step. If one is short the
    # tickers past that point never reach new_map, and --apply then replaces the
    # canonical map with fewer coins than it examined while reporting success.
    for t, j, d, m in zip(tickers, idx, dists, margins, strict=True):
        if max_d is None:
            new_map[t] = cids[j]            # dry run without a cutoff: report only
        elif d > max_d:
            far.append((t, float(d)))       # no genuine live image; omit, don't guess
        elif m < margin_min:
            ambiguous.append((t, float(d), float(m)))
        else:
            new_map[t] = cids[j]

    # Compare against the existing (broken) map.
    old = {}
    outp = Path(args.out)
    if outp.is_file():
        old = json.loads(outp.read_text(encoding="utf-8"))
    changed = sum(1 for t, c in new_map.items() if old.get(t) != c)
    log.info("rebuilt map: %d tickers | changed vs current: %d | omitted: %d too far, "
             "%d ambiguous", len(new_map), changed, len(far), len(ambiguous))
    log.info("match distance: min=%.1f median=%.1f p95=%.1f max=%.1f",
             dists.min(), np.median(dists), np.percentile(dists, 95), dists.max())
    finite = margins[np.isfinite(margins)]
    if finite.size:
        log.info("runner-up margin: min=%.1f median=%.1f", finite.min(), np.median(finite))
    worst = sorted(zip(tickers, dists, strict=True), key=lambda x: -x[1])[:10]
    log.info("worst matches (review): %s", [(t, round(float(d))) for t, d in worst])
    if ambiguous:
        log.warning("ambiguous (best vs runner-up too close): %s",
                    [(t, round(d), round(m)) for t, d, m in ambiguous[:10]])
    shared = sum(1 for n in Counter(new_map.values()).values() if n > 1)
    if shared:
        log.info("%d live stickers are claimed by more than one ticker (expected for "
                 "deduped logos -- review if unexpected)", shared)
    for probe in ("btc", "eth", "usdt", "usdu", "sol", "xrp"):
        if probe in new_map:
            log.info("  %s -> %s (was %s)%s", probe, new_map[probe],
                     old.get(probe), "" if new_map[probe] != old.get(probe) else " [unchanged]")

    if not args.apply:
        log.info("dry run (no file written). Re-run with --max-distance N --apply to save.")
        return EXIT_OK

    # The canonical map is what every consumer resolves tickers through: never
    # overwrite it from data we know is partial or unresolved.
    problems = []
    if incomplete:
        problems.append(f"{len(incomplete)} set(s) not fully downloaded ({incomplete[:5]})")
    if cache["errors"]:
        problems.append(f"{len(cache['errors'])} live sticker(s) failed to analyse")
    if ambiguous:
        problems.append(f"{len(ambiguous)} ambiguous match(es)")
    if problems:
        cand = Path(args.candidates)
        write_json_atomic(cand, new_map)
        log.error("NOT writing %s: %s", outp.name, "; ".join(problems))
        log.error("wrote %d reviewable candidates -> %s", len(new_map), cand)
        return EXIT_PARTIAL

    # ONE lock across read (for the backup and the final diff) and write. Every
    # other writer of the canonical map takes it too, so this replacement cannot
    # interleave with -- or be silently overwritten by -- a concurrent
    # read-modify-write, and the backup is of what was actually replaced.
    # LockBusy from either lock is handled by main(): EXIT_FAILED, nothing written.
    with canonical_map_lock():
        # Re-read under the lock: `old` above was sampled before waiting for
        # it, so it may already describe a file that no longer exists.
        current = json.loads(outp.read_text(encoding="utf-8")) if outp.is_file() else {}
        log.info("replacing the map: %d tickers, %d differ from the file on "
                 "disk now", len(new_map),
                 sum(1 for t, c in new_map.items() if current.get(t) != c))
        bak = outp.with_suffix(".prebroken.json")
        if outp.is_file() and not bak.exists():
            shutil.copyfile(outp, bak)
            log.info("backed up old map -> %s", bak.name)
        write_json_atomic(outp, new_map)
    log.info("WROTE corrected map -> %s", outp)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
