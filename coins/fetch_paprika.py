"""Fetch logos for still-missing inventory coins from CoinPaprika.

CoinGecko search failed for ~29 obscure inventory coins. CoinPaprika
(free, no API key) covers many of them. This script searches CoinPaprika by
coin name (and ticker as fallback), picks a confident match, downloads the logo
from CoinPaprika's static CDN, converts it to a 100x100 emoji PNG, optionally
adds it to the last not-full Telegram pack, captures the new custom_emoji_id,
and re-fills the inventory.

Key design points:
- The logo URL is deterministic: https://static.coinpaprika.com/coin/{id}/logo.png
  so once the coin id is known from search, the image is fetched from the static
  CDN (not the metered API) -> only /search calls count against the free quota.
- The free /search quota is small and returns HTTP 402 when exhausted. On 402 the
  script stops cleanly and persists progress to paprika_matches.json, so a later
  run (after the quota window resets) resumes without re-searching resolved coins.
- Matching is conservative: only "name-exact" or "symbol" matches are accepted.
  Weaker matches are reported but NOT used (a wrong logo is worse than a blank).

Usage:
  python fetch_paprika.py --dry     # search + cache matches, no downloads/packs
  python fetch_paprika.py           # download cached/confident matches, add to packs
"""

from __future__ import annotations

# This script lives in coins/; allow importing the shared engine (build_pack.py)
# from the project root.
import os as _bootstrap_os
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import argparse
import contextlib
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from PIL import Image

from emojikit.build_pack import (ingest_exit_code, load_env, load_keywords, safe_int_env)
from emojikit.packstate import (LockBusy, canonical_map_lock, exclusive_lock, make_intent, pack_family_lock_path, write_json_atomic)
from emojikit.telegram_api import (AmbiguousUploadError, LiveStateUnknown, SetState, Telegram)
from coins import _inventory
from coins._paprika_api import (QUOTA_EXHAUSTED, SLEEP, http_bytes, http_json, search_match,
                                to_emoji_png)
# One definition of the inventory format and of "which asset is this ticker",
# shared with alias_map, enhance_map, fetch_cmc and rebuild_dedup.
from emojikit.identity import _dhash, hamming
# The pipeline's single definition of "this image is effectively empty".

ROOT = Path(__file__).resolve().parent
EMOJI = ROOT / "logos" / "emoji"
# Where a fetcher parks a freshly downloaded logo until its upload is proven.
#
# EMOJI/<ticker>.png is the project's identity oracle: rebuild_dedup and
# remap_ids both decide which live sticker belongs to which ticker by comparing
# against it. Writing a download straight there -- which both fetchers did, from
# main(), with no lock held -- means the LOSER of a race leaves its art in the
# file while the map names the WINNER's sticker. Nothing is duplicated, but
# rebuild_dedup.resolve_by_image then reports MapIdentityUnproven and
# map_and_fill hard-stops until someone repairs it by hand. Staging keeps the
# oracle matching what was actually published.
INV = ROOT / "currency-emoji-inventory.md"
OUT_INV = ROOT / "currency-emoji-inventory.filled.md"
# The canonical publication state: which cryptoemoji* sets exist and how the
# family grows. Shared with rebuild_dedup, check_all_packs, remap_ids,
# verify_logos and write_manifests, which all already read this file.
#
# This used to be rebuild_state.json -- the file rebuild_dedup calls OLD_STATE,
# "the current 30 packs, to delete". Nothing has written it since the rebuild,
# so it is not merely stale, it is ABSENT: a live provider run died on the
# unguarded read below, and back when it did exist the providers appended coins
# to the packs the rebuild was about to delete. The tests never caught either,
# because they point STATE at a temp file.
STATE = ROOT / "rebuild_dedup_state.json"

# The providers' in-flight intent lives under its OWN key in that shared file.
# It cannot use "in_flight": rebuild_dedup validates that key against its frozen
# plan (`in_flight["key"]` must equal the plan entry its cursor just passed), so
# a ticker-keyed provider intent parked there makes the rebuild refuse to start
# with an error about a plan entry that has nothing to do with it. Both tools
# hold PACK_LOCK across their whole read-modify-write, and both round-trip the
# entire dict, so two keys in one file stay consistent.
INTENT_KEY = "provider_in_flight"
TICKER_IDS = ROOT / "ticker_to_id.json"
CACHE = ROOT / "paprika_matches.json"  # resumable {ticker: {id, conf, name}}
KEYWORDS_CSV = ROOT / "keywords.csv"
SET_BASE = "cryptoemoji"
SET_TITLE = "@YourBrand Crypto Emoji"
# One lock for the whole coin pack family, keyed on the BASE NAME. fetch_paprika,
# fetch_cmc, verify_logos --fix and rebuild_dedup all mutate the same
# cryptoemoji* sets; a lock named after whichever state file each tool happens
# to read let them hold three different locks and append concurrently.
#
# LOCK ORDER, project-wide: this pack-family lock FIRST, canonical_map_lock()
# second, never the reverse. Tools that only rewrite the map (alias_map,
# enhance_map, remap_ids --apply, rebuild_dedup map) take the map lock alone,
# so the two orders can never form a cycle.
PACK_LOCK = pack_family_lock_path(SET_BASE)
# A live sticker within this perceptual distance of the PNG we uploaded IS that
# upload: Telegram re-encodes PNG to WEBP, so identical content still differs by
# a bit or two.
SAME_IMAGE_MAX = 8

COIN = "https://api.coinpaprika.com/v1/coins/"
LOGO_CDN = "https://static.coinpaprika.com/coin/{id}/logo.png"
EMOJI_CHAR = "\U0001FA99"
PER_SET = 200
# Load .env at import so the owner id is available even when this module is
# imported by fetch_cmc.py (which reuses USER_ID).
load_env()
# Pack owner numeric Telegram id (from .env / env; never hardcode a personal id).
USER_ID = safe_int_env("PACK_OWNER_USER_ID", 0, minimum=0)


# calls these names and the tests retarget the inventory by patching fp.INV, so
# the paths have to be read at call time, not at import.
def parse_missing(have: set[str]) -> list[tuple[str, str]]:
    return _inventory.parse_missing(have, INV)


def refill_inventory(ticker_to_id: dict[str, str]) -> tuple[int, int]:
    return _inventory.refill_inventory(ticker_to_id, INV, OUT_INV)


def load_cache() -> dict[str, dict]:
    if CACHE.is_file():
        return json.loads(CACHE.read_text("utf-8"))
    return {}


def save_cache(cache: dict[str, dict]) -> None:
    write_json_atomic(CACHE, cache)


# --------------------------------------------------------------------------- #
# Publishing (shared by fetch_paprika and fetch_cmc)
# --------------------------------------------------------------------------- #
def live_stickers(tg: Telegram, name: str) -> list[dict]:
    """Live stickers of a set. Raises rather than guessing an empty set."""
    return tg.get_sticker_set(name).get("stickers", [])


# Unique to THIS process, fixed for its whole life. Two fetchers running at once
# resolve different staging paths for the same ticker, which is the point.
_RUN_TOKEN = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def incoming_dir() -> Path:
    """This RUN's private staging directory.

    A staging path shared by ticker name is still a shared MUTABLE path: the
    download happens before any lock is held, so while this process is inside
    publish_logos() -- hashing the file, sending its bytes, comparing the live
    sticker against it, promoting it -- another process can replace it between
    any two of those steps. The recorded hash, the uploaded bytes, the retry
    proof and the published identity oracle would then describe different
    images, and an ambiguous request could be misclassified or duplicated.
    Per-run isolation removes the shared name entirely.

    Derived from EMOJI rather than being its own constant, so relocating EMOJI
    relocates staging with it and a test cannot write into the real tree.
    """
    return EMOJI.parent / ".incoming" / _RUN_TOKEN


def staged_or_published(tk: str) -> Path:
    """The image to send for ``tk``: THIS run's download if it made one.

    Falls back to the published file so a resumed run, whose staging directory
    belonged to a process that is gone, still has a source.
    """
    staged = incoming_dir() / f"{tk}.png"
    return staged if staged.is_file() else EMOJI / f"{tk}.png"


def publish_source(staged: Path, tk: str, want: int) -> bool:
    """Promote THAT EXACT file to the shared oracle, if it really is the image.

    Takes the path the caller actually uploaded rather than re-deriving one:
    re-resolving mid-mutation is how a different image ends up promoted from
    the one whose hash was recorded and whose bytes were sent.

    ``want`` is checked here rather than trusted from the caller, because the
    recovery path can arrive with a FALLBACK source -- a different run's fresh
    download of the same ticker -- while the map is being pointed at the sticker
    the ORIGINAL image produced. Promoting that would leave ticker_to_id naming
    one image and coins/logos/emoji/<ticker>.png holding another, which is
    exactly the disagreement the oracle exists to rule out.

    Only after the upload is proven and the map is written -- until then the
    file would be claiming an identity for a sticker that may not exist.
    """
    if not staged.is_file():
        return False
    try:
        if source_dhash(staged) != want:
            return False
    except Exception:      # noqa: BLE001 - unreadable is not a match either
        return False
    EMOJI.mkdir(parents=True, exist_ok=True)
    os.replace(staged, EMOJI / f"{tk}.png")
    return True


def discard_staging(keep: Path | None = None) -> None:
    """Remove THIS run's staging, except a file an unresolved intent still needs.

    Never another run's directory: theirs may be mid-upload, and deleting it
    would take the identity oracle out from under a live mutation.

    ``keep`` is the source of an in-flight intent this run could not resolve.
    Deleting it stranded the next run: it would find the recorded path gone,
    fall back to its OWN fresh download of the same ticker, and promote that as
    the oracle for a sticker made from the original image. The file is kept
    until the intent is settled, and settling it promotes or discards the file.
    """
    root = incoming_dir()
    keep = keep if keep and keep.is_file() and keep.parent == root else None
    if keep is None:
        with contextlib.suppress(OSError):
            shutil.rmtree(root)
        return
    with contextlib.suppress(OSError):
        for entry in root.iterdir():
            if entry != keep:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)


def source_dhash(png: Path) -> int:
    """The identity of the image we are about to send, captured ONCE.

    ``EMOJI/<ticker>.png`` is a shared, mutable path: both fetchers download
    into it from main(), with no lock held. Re-deriving the identity oracle from
    that file at check time therefore compares the live sticker against whatever
    art landed there LAST, not against what we sent -- so our own upload reads
    as "not ours", the map is left unwritten, and the ticker is uploaded a
    second time. Hashing at intent time and carrying the value forward is what
    makes the oracle immutable for the life of the mutation.
    """
    return _dhash(Image.open(png).convert("RGBA"))


def _content_matches(tg: Telegram, candidates: list[dict], want: int,
                     label: str) -> list[str]:
    """custom_emoji_ids among ``candidates`` whose image hashes to ``want``.

    Raises LiveStateUnknown for a candidate that cannot be read: an unreadable
    sticker is not a non-match, and scoring it as one re-uploads an emoji that
    is already live.
    """
    hits: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for st in candidates:
            dest = Path(tmp) / str(st.get("file_unique_id") or st["file_id"])
            try:
                tg.download_file(str(st["file_id"]), dest)
                got = _dhash(Image.open(dest).convert("RGBA"))
            except Exception as exc:
                raise LiveStateUnknown(
                    f"sticker {st.get('custom_emoji_id')} in {label}'s set "
                    f"could not be read ({exc})") from exc
            if hamming(want, got) <= SAME_IMAGE_MAX:
                hits.append(str(st["custom_emoji_id"]))
    return hits


def _landed_emoji_id(tg: Telegram, name: str, before: int, want: int,
                     label: str) -> str | None:
    """custom_emoji_id of OUR upload in ``name``, or None when it is not there.

    Identity, never position or count. A sticker added by hand or by another
    run makes the set one longer too, so "it grew by one" is not proof that our
    upload is what grew it -- and the emoji id read off the tail then belongs to
    someone else's sticker. Raises LiveStateUnknown when live state does not
    answer: the caller must stop instead of guessing.
    """
    set_state, sset = tg.probe_set_state(name)
    if set_state is SetState.UNKNOWN:
        raise LiveStateUnknown(f"live state of {name} is unknown")
    if set_state is SetState.MISSING:
        return None                       # a create that never landed
    hits = _content_matches(tg, (sset.get("stickers") or [])[before:], want, label)
    if len(hits) > 1:
        raise LiveStateUnknown(
            f"{len(hits)} live stickers in {name} carry {label}; refusing to "
            f"guess which one is ours")
    return hits[0] if hits else None


def _mutate(call, *args, **kw) -> None:
    """Run a set-growing Telegram call, tolerating an ambiguous outcome.

    An ambiguous failure must never be re-sent (that is what duplicates an
    emoji); the caller's identity check reads live state and decides.
    """
    try:
        call(*args, **kw)
    except AmbiguousUploadError as exc:
        print(f"  {exc}; deciding from live state", flush=True)


def _recover_in_flight(tg: Telegram, state: dict,
                       ticker_to_id: dict[str, str]) -> str | None:
    """Resolve the mutation an earlier run could not verify. Returns its ticker.

    Swallowing an ambiguous add was only half of the contract: when the identity
    check that was supposed to decide it ALSO failed, nothing recorded that an
    emoji might already be live, and the next run added the same image again.
    The intent on disk is that record; it is reconciled here, before this run is
    allowed to mutate anything.
    """
    intent = state.get(INTENT_KEY)
    if not intent:
        return None
    tk, name = intent.get("key"), intent.get("set_name")
    # The file THAT run staged, if it is still there. Its staging directory is
    # private to that process, so nothing else can have replaced its contents;
    # a name-based lookup would instead find whatever this run just downloaded.
    recorded = intent.get("source_path")
    png = Path(recorded) if recorded and Path(recorded).is_file() else \
        staged_or_published(tk)
    # The identity recorded WITH the mutation, not re-derived from the file now.
    # Between that run and this one, main() may have re-downloaded the ticker --
    # it does so whenever the map has no entry, which is exactly the state an
    # unresolved upload leaves behind. Re-hashing the file would compare the
    # live sticker against the NEW art, answer "it did not land", and upload a
    # second copy. No concurrency is needed for that; two sequential runs do it.
    want = intent.get("source_dhash")
    if not tk or not name or (want is None and not png.is_file()):
        raise LiveStateUnknown(
            f"the unresolved upload {intent!r} cannot be checked (its ticker, "
            f"set name or recorded image identity is gone)")
    if want is None:
        # An intent written before identities were recorded. The file is the
        # only oracle left, and it may already have been overwritten -- say so.
        print(f"  {tk}: the unresolved upload predates recorded image "
              f"identities; falling back to {png.name} as it stands now",
              flush=True)
        want = source_dhash(png)
    cid = _landed_emoji_id(tg, name, intent.get("expected_before") or 0,
                           int(want), f"{tk}.png")
    if cid:
        names = {s["name"] for s in state["sets"]}
        if intent.get("operation") == "create" and name not in names:
            # The set exists but was never recorded: without this every later
            # add targets the previous, already-full set.
            # "live" belongs in every set record: rebuild_dedup subscripts it
            # directly when it picks the set to continue, and only survives a
            # record without it because an earlier loop happens to refresh every
            # set from Telegram first. Twelve sets written here were missing the
            # field, and the state file on disk was wrong about them until
            # someone read Telegram by hand. A create leaves exactly one
            # sticker; anything already in the set is corrected by that refresh.
            state["sets"].append({"index": intent.get("set_index"), "name": name,
                                  "title": intent.get("title", ""), "live": 1})
        # Every live sticker in this family must be accounted for by SOME
        # record: that is the invariant rebuild_dedup's consistency gate
        # enforces before it will touch the packs. Its own `order` cannot hold
        # this ticker -- `order` must be a subsequence of the frozen plan, and a
        # provider exists precisely to add coins the plan never had. So the
        # providers keep their own tally in the same file, and the gate counts
        # both.
        _record_provider_add(state, tk)
        ticker_to_id[tk] = cid
        write_json_atomic(TICKER_IDS, ticker_to_id)
        print(f"  recovered {tk}: {cid} landed before the interruption",
              flush=True)
        # The map now names the sticker the ORIGINAL image produced, so only
        # that image may become the oracle. `png` is a fallback whenever the
        # staged original is gone, and a fallback is this run's own fresh
        # download -- a different picture for the same ticker.
        if not publish_source(png, tk, int(want)):
            print(f"  {tk}: {EMOJI.name}/{tk}.png was NOT updated -- the image "
                  f"that produced {cid} is no longer on disk, and publishing a "
                  f"different one would make the map and the local logo "
                  f"disagree. Re-fetch this ticker to refresh it.", flush=True)
    else:
        print(f"  {tk}: the unresolved upload did not land; retrying it",
              flush=True)
    state[INTENT_KEY] = None
    write_json_atomic(STATE, state)
    return tk if cid else None



def _record_provider_add(state: dict, ticker: str) -> None:
    """Tally one provider upload against the shared publication state.

    Idempotent: a recovery run that re-confirms an already-recorded upload must
    not count it twice, or the very gate this feeds would then reject a state
    that is perfectly sound.
    """
    added = state.setdefault("provider_added", [])
    if ticker not in added:
        added.append(ticker)


def publish_logos(tg: Telegram, tickers: list[str],
                  ticker_to_id: dict[str, str]) -> tuple[int, int]:
    """Add one emoji per ticker to the coin pack family; returns (added, failed).

    Both fetchers used to carry their own copy of this loop, and both had
    drifted into the same two defects: a blind retry of the non-idempotent add
    (a timeout after Telegram applied it duplicates the emoji) and reading the
    new emoji ids off the tail of the set.

    Every mutation is written to a durable in-flight ledger in STATE first, so
    an outcome this run cannot verify is resolved by the next one instead of
    being silently re-sent.

    ``ticker_to_id`` is REFRESHED FROM DISK once the locks are held, in place,
    so the caller sees the merged map too.
    """
    keywords = load_keywords(KEYWORDS_CSV)
    added = failed = 0
    # Bound before the try so the finally can read it even when the lock is busy
    # and the body never ran.
    state: dict = {}
    try:
        # Pack lock first, canonical map lock second -- the project-wide order
        # (see PACK_LOCK). The map lock is held for the whole batch because the
        # per-sticker writes below are one read-modify-write of the same file.
        with exclusive_lock(PACK_LOCK), canonical_map_lock():
            # RE-READ under the lock. main() loaded the map BEFORE waiting here,
            # so its snapshot predates whatever the tool we queued behind wrote.
            # Writing the whole file back from it drops that tool's ids -- the
            # two tools serialise their Telegram mutations and still lose each
            # other's map update.
            if TICKER_IDS.is_file():
                ticker_to_id.clear()
                ticker_to_id.update(json.loads(TICKER_IDS.read_text("utf-8")))
            bot = tg.get_me()["username"]
            # Both halves fail CLOSED and say which file is wrong. The read was
            # unguarded and the sets list was indexed blind, so pointing at a
            # file that no longer existed surfaced as a bare FileNotFoundError
            # -- and an empty one would have surfaced as IndexError -- from
            # inside a locked mutation path, which reads as a crash rather than
            # as "this tool is looking in the wrong place".
            if not STATE.is_file():
                print(f"STOP: {STATE.name} does not exist, so there is no "
                      f"record of which packs this family already has. "
                      f"Publishing now would start a second family beside the "
                      f"live one. Run coins/rebuild_dedup.py first, or restore "
                      f"the state file.", flush=True)
                return 0, len(tickers)
            state = json.loads(STATE.read_text("utf-8"))
            try:
                recovered = _recover_in_flight(tg, state, ticker_to_id)
            except LiveStateUnknown as exc:
                print(f"STOP: {exc}; refusing to add anything on top of an "
                      f"upload that may be live", flush=True)
                return 0, len(tickers)
            if not state.get("sets"):
                print(f"STOP: {STATE.name} records no sets. The providers top "
                      f"up an existing pack family; they do not create the "
                      f"first pack. Run coins/rebuild_dedup.py first.",
                      flush=True)
                return 0, len(tickers)
            last = sorted(state["sets"], key=lambda x: x["index"])[-1]
            set_index, set_name = last["index"], last["name"]
            for i, tk in enumerate(tickers):
                if tk == recovered:
                    print(f"  {tk}: added by the interrupted run; not re-adding",
                          flush=True)
                    added += 1
                    continue
                if ticker_to_id.get(tk):
                    # Re-reading the map is pointless while the loop still walks
                    # the list main() built from the map as it was BEFORE this
                    # run queued for the lock: a ticker another provider mapped
                    # while we waited is still in that list, and adding it puts
                    # a SECOND copy of the same coin in the pack and overwrites
                    # the live sticker's id with the copy's. A non-empty entry is
                    # proof enough here -- it is younger than that snapshot, and
                    # every writer of this map (both fetchers, the rebuild,
                    # remap_ids, verify_logos) records an id only after proving
                    # by content which live sticker it belongs to.
                    print(f"  {tk}: mapped to {ticker_to_id[tk]} while this run "
                          f"waited for the lock; not adding a second copy",
                          flush=True)
                    continue
                png = staged_or_published(tk)
                kw = keywords.get(tk, tk)
                try:
                    live = live_stickers(tg, set_name)
                except Exception as exc:  # noqa: BLE001 - nothing was mutated yet
                    print(f"  add failed {tk}: {exc}", flush=True)
                    failed += 1
                    continue
                if len(live) >= PER_SET:
                    index = set_index + 1
                    name = f"{SET_BASE}{index}_by_{bot}"
                    title, op, before = f"{SET_TITLE} {index}", "create", 0
                else:
                    index, name = set_index, set_name
                    title, op, before = last.get("title", ""), "add", len(live)
                # Durable record of the mutation ABOUT to run. An outcome that
                # cannot be verified afterwards is only recoverable if the next
                # run knows which image was sent to which set -- and WHICH IMAGE
                # that was. `png` is this run's private staging path, resolved
                # once here and used unchanged for the hash, the upload, the
                # applied-check and the promotion, so no step can see a
                # different image from the one the step before it saw.
                try:
                    want = source_dhash(png)
                except Exception as exc:  # noqa: BLE001 - unusable source image
                    print(f"  add failed {tk}: cannot read {png.name}: {exc}",
                          flush=True)
                    failed += 1
                    continue
                intent = make_intent(
                    key=tk, operation=op, set_name=name, set_index=index,
                    expected_before=before, title=title)
                intent["source_dhash"] = want
                # The exact file, so a recovery run reads what was sent rather
                # than re-resolving a name that now points somewhere else.
                intent["source_path"] = str(png)
                state[INTENT_KEY] = intent
                write_json_atomic(STATE, state)
                try:
                    if op == "create":
                        _mutate(tg.create_set, USER_ID, name, title, png,
                                EMOJI_CHAR, kw)
                    else:
                        # expected_before makes a retry after a network failure
                        # verify the add instead of repeating it.
                        _mutate(tg.add_sticker, USER_ID, name, png, EMOJI_CHAR,
                                kw, expected_before=before)
                except Exception as exc:  # noqa: BLE001 - one coin must not stop the batch
                    # A Bot API rejection is definitive: _call raises only once
                    # the change is verified NOT applied.
                    print(f"  add failed {tk}: {exc}", flush=True)
                    state[INTENT_KEY] = None
                    write_json_atomic(STATE, state)
                    failed += 1
                    continue
                try:
                    cid = _landed_emoji_id(tg, name, before, want, png.name)
                except LiveStateUnknown as exc:
                    # Leave the intent on disk -- it is the only record of an
                    # upload that may be live -- and stop before the next
                    # mutation, which would make it unresolvable.
                    print(f"STOP: {tk}: {exc}; resolved on the next run",
                          flush=True)
                    return added, failed + len(tickers) - i
                if cid is None:
                    print(f"  add failed {tk}: nothing of ours is live in {name}",
                          flush=True)
                    state[INTENT_KEY] = None
                    write_json_atomic(STATE, state)
                    failed += 1
                    continue
                if op == "create":
                    # Record the set only once it is verifiably live: a phantom
                    # entry sends every later add to a set that does not exist.
                    set_index, set_name = index, name
                    # live=1: a create places exactly one sticker. See the
                    # note on the other append -- every reader expects the field.
                    last = {"index": index, "name": name, "title": title,
                            "live": 1}
                    state["sets"].append(dict(last))
                _record_provider_add(state, tk)
                ticker_to_id[tk] = cid
                write_json_atomic(TICKER_IDS, ticker_to_id)
                # Proven and mapped: this art now identifies a LIVE sticker, so
                # it may become the oracle the other tools trust. The same Path
                # that was hashed and uploaded, never a freshly resolved one.
                publish_source(png, tk, want)
                state[INTENT_KEY] = None
                write_json_atomic(STATE, state)
                added += 1
                time.sleep(0.3)
    except LockBusy as exc:
        print(f"ERROR: {exc}", flush=True)
        return 0, len(tickers)
    finally:
        # Only this run's directory, and NOT the source of an intent this run
        # could not settle. Deleting that one stranded the next run: it would
        # find the recorded path gone, fall back to its OWN fresh download of
        # the same ticker, and promote that as the oracle for a sticker made
        # from the original image -- leaving the map naming one picture and the
        # local logo holding another. Everything else here was never published,
        # so keeping it would leave an unpublished image looking like a live one.
        pending = (state.get(INTENT_KEY) or {}) if isinstance(state, dict) else {}
        src = pending.get("source_path") if isinstance(pending, dict) else None
        discard_staging(Path(src) if src else None)
    return added, failed


def resolve_phase(missing, cache) -> bool:
    """Search CoinPaprika for unresolved coins. Returns True if quota was hit."""
    quota_hit = False
    for name, tk in missing:
        if tk in cache:
            continue
        r = search_match(name, tk)
        if r is QUOTA_EXHAUSTED:
            print("  HTTP 402: CoinPaprika free quota exhausted; stopping search.",
                  flush=True)
            quota_hit = True
            break
        cid, conf = r
        if cid and conf in ("name-exact", "symbol"):
            cache[tk] = {"id": cid, "conf": conf, "name": name}
            print(f"  MATCH {tk:12s} <- {cid}  [{conf}]  ({name})", flush=True)
        else:
            cache[tk] = {"id": None, "conf": conf, "name": name}
            tag = "partial" if conf.startswith("partial") else "none"
            print(f"  {tag:7s} {tk:12s}  {conf}  ({name})", flush=True)
        save_cache(cache)  # persist after every coin (resumable)
    return quota_hit


def main(argv: list[str] | None = None) -> int:
    # argparse, not `"--dry" in sys.argv`: membership testing means every
    # spelling that is not exactly "--dry" -- a typo like --dryy, an unknown
    # flag, a stray positional -- silently selected the LIVE branch, which
    # uploads to Telegram with the owner's credentials and rewrites the
    # canonical map. The safe reading of an argument nobody recognises is to
    # refuse, and argparse refuses with a usage error and exit 2.
    ap = argparse.ArgumentParser(
        description="Fill unmapped coins from CoinPaprika and publish their "
                    "logos to the coin pack family.",
        # allow_abbrev=False: argparse accepts unambiguous prefixes by
        # default, so "--dr" silently became "--dry". A near-miss is the
        # typo class this guard exists for -- it must not be guessed at,
        # in either direction.
        allow_abbrev=False)
    ap.add_argument("--dry", action="store_true",
                    help="resolve and report only; download nothing and make "
                         "no pack or map changes.")
    dry = ap.parse_args(argv).dry
    load_env()
    ticker_to_id: dict[str, str] = json.loads(TICKER_IDS.read_text("utf-8"))
    have = set(ticker_to_id)
    missing = parse_missing(have)
    cache = load_cache()
    print(f"missing to resolve: {len(missing)} | cached: {len(cache)} | dry={dry}",
          flush=True)

    quota_hit = resolve_phase(missing, cache)

    confident = [(tk, v["id"]) for tk, v in cache.items()
                 if v.get("id") and tk not in have]
    print(f"\nconfident(new)={len(confident)} cached_total={len(cache)} "
          f"quota_hit={quota_hit}", flush=True)

    if dry:
        print("dry run: matches cached, no downloads/pack changes.", flush=True)
        return 0
    if not confident:
        print("no new confident matches to add.", flush=True)
        return 0

    # Download confident matches from the static CDN (not metered) -> emoji PNGs.
    # If the deterministic CDN path is missing (404), fall back to the /coins/{id}
    # API 'logo' field (metered, but quota is available when this path is reached).
    fetched: list[str] = []
    failed = 0
    for tk, cid in confident:
        data = http_bytes(LOGO_CDN.format(id=cid))
        if not data:
            detail = http_json(COIN + cid)
            time.sleep(SLEEP)
            url = (detail or {}).get("logo") if detail is not QUOTA_EXHAUSTED else None
            if url and url != LOGO_CDN.format(id=cid):
                data = http_bytes(url)
        if not data:
            print(f"  download failed: {tk} ({cid})", flush=True)
            failed += 1
            continue
        if to_emoji_png(data, incoming_dir() / f"{tk}.png"):
            fetched.append(tk)
            print(f"  got logo: {tk} <- {cid}", flush=True)
        else:
            print(f"  unusable logo: {tk} ({cid})", flush=True)
            failed += 1

    print(f"fetched logos: {len(fetched)}", flush=True)
    if not fetched:
        print("nothing to add.", flush=True)
        return ingest_exit_code(0, failed)

    # Add to the last not-full set, overflow to new sets.
    tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
    added, add_failed = publish_logos(tg, fetched, ticker_to_id)
    filled, total = refill_inventory(ticker_to_id)
    print(f"added {added} stickers; inventory filled: {filled}/{total}", flush=True)
    return ingest_exit_code(added, failed + add_failed)


if __name__ == "__main__":
    raise SystemExit(main())
