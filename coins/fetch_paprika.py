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
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

from build_pack import (AmbiguousUploadError, LiveStateUnknown, LockBusy,
                        SetState, Telegram, canonical_map_lock, exclusive_lock,
                        ingest_exit_code, load_env, load_keywords, make_intent,
                        pack_family_lock_path, safe_int_env, write_json_atomic)
from emojikit.media import _dhash, hamming
# The pipeline's single definition of "this image is effectively empty".
from make_emoji_pngs import _is_blank

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
STATE = ROOT / "rebuild_state.json"
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

SEARCH = "https://api.coinpaprika.com/v1/search?c=currencies&limit=10&q="
COIN = "https://api.coinpaprika.com/v1/coins/"
LOGO_CDN = "https://static.coinpaprika.com/coin/{id}/logo.png"
HEADERS = {"User-Agent": "Mozilla/5.0 (logo-fetcher; local)"}
EMOJI_CHAR = "\U0001FA99"
PER_SET = 200
# Load .env at import so the owner id is available even when this module is
# imported by fetch_cmc.py (which reuses USER_ID).
load_env()
# Pack owner numeric Telegram id (from .env / env; never hardcode a personal id).
USER_ID = safe_int_env("PACK_OWNER_USER_ID", 0, minimum=0)
SIZE = 100
SLEEP = 2.5  # seconds between metered /search calls

QUOTA_EXHAUSTED = object()  # sentinel returned by http_json on HTTP 402


def norm(s: str) -> str:
    """Lowercase, drop parenthetical qualifiers, keep only [a-z0-9]."""
    s = re.sub(r"\(.*?\)", " ", s.lower())
    return "".join(re.findall(r"[a-z0-9]+", s))


def base_ticker(t: str) -> str:
    """Strip common chain suffixes (e.g. kibabsc -> kiba)."""
    for suf in ("mainnet", "erc20", "bep20", "trc20", "polygon", "base", "matic",
                "avax", "bsc", "arb", "ton", "sol", "trx", "eth", "op"):
        if t.endswith(suf) and len(t) > len(suf) + 1:
            return t[: -len(suf)]
    return t


def http_json(url: str, retries: int = 4):
    """GET JSON. Returns dict, None (transient failure), or QUOTA_EXHAUSTED on 402."""
    for a in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 402:
                return QUOTA_EXHAUSTED  # free quota gone; do not retry
            print(f"  http retry {a}: HTTP {exc.code}", flush=True)
            time.sleep(min(4 * a, 20))
        except Exception as exc:  # noqa: BLE001
            print(f"  http retry {a}: {exc}", flush=True)
            time.sleep(min(4 * a, 20))
    return None


def http_bytes(url: str, retries: int = 3):
    for a in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            time.sleep(2 * a)
    return None


def to_emoji_png(data: bytes, dest: Path) -> bool:
    """Crop to alpha bbox, fit into a transparent 100x100 RGBA canvas.

    Returns False -- writing nothing -- for a blank provider image. A fully
    transparent placeholder used to pass straight through here (no alpha bbox
    means nothing was cropped) and be uploaded as an empty emoji. The source is
    judged by the same visible-alpha rule as the rest of the pipeline, BEFORE
    the fit: scaling a handful of stray pixels up to 100px would hide the very
    emptiness we are testing for.
    """
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return False
    if _is_blank(im):
        return False
    im = im.crop(im.split()[3].getbbox())  # _is_blank guarantees a bbox
    w, h = im.size
    sc = min(SIZE / w, SIZE / h)
    nw, nh = max(1, round(w * sc)), max(1, round(h * sc))
    im = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(im, ((SIZE - nw) // 2, (SIZE - nh) // 2), im)
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest, format="PNG", optimize=True)
    return True


def classify(cands: list[dict], name: str, ticker: str):
    """Pick best candidate. Returns (id, confidence) or (None, reason)."""
    if not cands:
        return None, "no-results"
    nn = norm(name)
    bt = base_ticker(ticker)
    for c in cands:  # 1) exact normalized name
        if norm(c.get("name", "")) == nn:
            return c["id"], "name-exact"
    for c in cands:  # 2) symbol equals full or base ticker
        if str(c.get("symbol", "")).lower() in (ticker, bt):
            return c["id"], "symbol"
    for c in cands:  # 3) partial (reported, not auto-used)
        cn = norm(c.get("name", ""))
        if cn and (cn in nn or nn in cn):
            return None, f"partial:{c['id']}"
    return None, f"no-confident-match(top={cands[0]['id']})"


def search_match(name: str, ticker: str):
    """Up to two metered /search calls. Returns (id, conf) | (None, reason) | QUOTA."""
    q1 = re.sub(r"\(.*?\)", "", name).strip()
    res = http_json(SEARCH + urllib.parse.quote(q1))
    if res is QUOTA_EXHAUSTED:
        return QUOTA_EXHAUSTED
    time.sleep(SLEEP)
    cands = (res or {}).get("currencies", [])
    cid, conf = classify(cands, name, ticker)
    if cid:
        return cid, conf
    # Fallback: search by ticker only when the name search was unhelpful.
    if not cands or conf.startswith("no-confident") or conf == "no-results":
        res2 = http_json(SEARCH + urllib.parse.quote(ticker))
        if res2 is QUOTA_EXHAUSTED:
            return QUOTA_EXHAUSTED
        time.sleep(SLEEP)
        cands2 = (res2 or {}).get("currencies", [])
        cid2, conf2 = classify(cands2, name, ticker)
        if cid2:
            return cid2, conf2
        return None, conf2 if cands2 else conf
    return None, conf


def parse_missing(have: set[str]) -> list[tuple[str, str]]:
    text = INV.read_text(encoding="utf-8")
    blocks = re.findall(r"##\s*(\S+)\s*[\u2014-]+\s*(.+?)\n\s*ticker:\s*(\S+)", text)
    return [(name.strip(), tk.lower()) for _h, name, tk in blocks
            if tk.lower() not in have]


def refill_inventory(ticker_to_id: dict[str, str]) -> tuple[int, int]:
    text = INV.read_text(encoding="utf-8")
    lines = text.split("\n")
    t_re = re.compile(r"^\s*ticker:\s*(?P<v>.+?)\s*$")
    p_re = re.compile(r"^(?P<prefix>\s*)premium-id:\s*.*$")
    cur = None
    filled = total = 0
    for i, ln in enumerate(lines):
        m = t_re.match(ln)
        if m:
            cur = m.group("v").strip().lower()
            total += 1
            continue
        pm = p_re.match(ln)
        if pm and cur is not None:
            eid = ticker_to_id.get(cur, "")
            lines[i] = (f"{pm.group('prefix')}premium-id: {eid}").rstrip()
            if eid:
                filled += 1
            cur = None
    OUT_INV.write_text("\n".join(lines), encoding="utf-8")
    return filled, total


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
            except Exception as exc:  # noqa: BLE001
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
    intent = state.get("in_flight")
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
            state["sets"].append({"index": intent.get("set_index"), "name": name,
                                  "title": intent.get("title", "")})
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
    state["in_flight"] = None
    write_json_atomic(STATE, state)
    return tk if cid else None


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
            state = json.loads(STATE.read_text("utf-8"))
            try:
                recovered = _recover_in_flight(tg, state, ticker_to_id)
            except LiveStateUnknown as exc:
                print(f"STOP: {exc}; refusing to add anything on top of an "
                      f"upload that may be live", flush=True)
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
                state["in_flight"] = intent
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
                    state["in_flight"] = None
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
                    state["in_flight"] = None
                    write_json_atomic(STATE, state)
                    failed += 1
                    continue
                if op == "create":
                    # Record the set only once it is verifiably live: a phantom
                    # entry sends every later add to a set that does not exist.
                    set_index, set_name = index, name
                    last = {"index": index, "name": name, "title": title}
                    state["sets"].append(dict(last))
                ticker_to_id[tk] = cid
                write_json_atomic(TICKER_IDS, ticker_to_id)
                # Proven and mapped: this art now identifies a LIVE sticker, so
                # it may become the oracle the other tools trust. The same Path
                # that was hashed and uploaded, never a freshly resolved one.
                publish_source(png, tk, want)
                state["in_flight"] = None
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
        pending = (state.get("in_flight") or {}) if isinstance(state, dict) else {}
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


def main() -> int:
    dry = "--dry" in sys.argv
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
