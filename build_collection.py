"""Publish the catalog into new Telegram custom-emoji packs (multi-format).

Reads pending emoji from the content-addressed catalog and uploads them into
fresh custom-emoji sets owned by the configured user, with a new base name and
title. Static, animated and video emoji are published into SEPARATE sets by
default for organization/clarity. NOTE: since Bot API 7.2 (March 2024) a single
set MAY contain mixed formats, so this split is a choice, not a requirement --
the brand logo (static) is added as the first emoji of every set regardless of
the set's format.

Duplicate-proof & resumable:

* A FROZEN, append-only plan (``publish_plan_<base>.json``) fixes the upload
  order per format, so resume is deterministic.
* Progress is reconciled from LIVE Telegram counts (sum of stickers actually
  present), exactly like the coin rebuild tool -> interrupting and resuming can
  never create a duplicate. ``pending = plan[fmt][live_total:]``.

Usage:
  python build_collection.py --base mypack --title "My Pack" \
      [--token-env GENERAL_BOT_TOKEN] [--user-id N] [--emoji 😀] \
      [--formats static,video,animated] [--per-set 200] [--data-dir collection] \
      [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from build_pack import (EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE,
                        AmbiguousUploadError, BotApiError, LiveStateUnknown,
                        LockBusy, SetState, Telegram, exclusive_lock,
                        ingest_exit_code, announce_packs, load_env,
                        safe_int_env, write_json_atomic)
from emojikit import media
from emojikit.catalog import Catalog
from emojikit.logsetup import record_exit_code, redact, setup_logging
from PIL import Image

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("build_collection")

PER_SET = 200                       # Telegram custom-emoji set hard cap
FMT_TAG = {"static": "s", "video": "v", "animated": "a"}
# Publishing every format into ONE family. Since Bot API 7.2 (March 2024) a set
# may hold mixed formats, so the per-format split this tool did by default was a
# choice, not a rule -- and a costly one: the curate panel's order runs ACROSS
# formats, so splitting regrouped a hand-arranged pack into three blocks and
# threw the arrangement away. In this mode the sets are named `<base><n>` with
# no format letter, and each item is uploaded with ITS OWN format.
#
# It is a flag, not the new default, for one concrete reason: `state["sets"]`
# entries and the frozen plan are keyed by format, so flipping the default would
# make an existing half-published family unresumable.
MIXED = "mixed"
DEFAULT_EMOJI = "\U0001F600"

# --- Brand logo (first emoji of every set built with the Emoji Mapper bot) --- #
# Only packs published by these bots get the mandatory YourBrand logo as their
# first emoji. The coin bot (@YourCoinEmojiBot) is intentionally
# excluded, so it is NOT in this set.
BRAND_LOGO_BOTS = {"youremojibot"}
# Ships with the repository. This used to be an absolute F:\ path, so on any
# other machine the "mandatory" logo silently vanished from every pack.
BRAND_LOGO_DEFAULT = str(ROOT / "assets" / "yourbrand-emoji-logo.png")
BRAND_LOGO_EMOJI = "\u2705"          # ✅ associated standard emoji for the logo
BRAND_LOGO_KW = ["yourbrand", "logo"]


class BrandLogo:
    """The brand logo (YourBrand) used as the FIRST emoji of every set.

    Since Bot API 7.2 (March 2024) a single custom-emoji set may contain mixed
    formats, so the logo is always a **static** 100x100 PNG and can lead a
    static, video OR animated set alike. Prepared once and cached under
    ``<data_dir>/brand/logo.png``.
    """

    def __init__(self, src: str, data_dir: Path) -> None:
        self.src = Path(src) if src else None
        self.dir = data_dir / "brand"
        self._png: Path | None = None

    def available(self) -> bool:
        return bool(self.src and self.src.is_file())

    def static_png(self) -> Path | None:
        """Return a ready 100x100 PNG logo path, or None if unavailable.

        The cache file is named after a digest of the SOURCE image, so
        replacing the logo produces a different name and is picked up. A fixed
        ``logo.png`` was reused forever, so a changed brand logo kept
        publishing the old pixels.
        """
        if not self.available():
            return None
        if self._png and self._png.is_file():
            return self._png
        from emojikit import media
        try:
            digest = hashlib.sha256(self.src.read_bytes()).hexdigest()[:12]
            out = self.dir / f"logo_{digest}.png"
            if not out.is_file():
                media.to_static_png(self.src, out)
            self._png = out
            return out
        except Exception as exc:  # noqa: BLE001 - logo is best-effort, never fatal
            log.warning("brand logo prepare failed: %s", exc)
            return None


def _static_is_blank(path: Path) -> bool:
    """True if a static image is effectively empty (guards against blank emoji).

    Delegates to the shared rule rather than carrying a third copy of it: this
    file, emojikit.media and coins/check_all_packs each had their own pixel loop
    with their own re-declared thresholds, so "is this emoji blank?" -- the
    pipeline's central quality gate -- had more than one implementation that
    could drift apart. media.is_blank_image is also the fast one, and this runs
    on every static item of every publish.
    """
    try:
        im = Image.open(path)
    except Exception:  # noqa: BLE001 - non-static or unreadable: let upload decide
        return False
    return media.is_blank_image(im)


class StateError(RuntimeError):
    """A publish plan/state file exists but cannot be used as it stands."""


class SetDrift(Exception):
    """A live set no longer matches the manifest this publisher recorded.

    Deliberately NOT a ``RuntimeError``, unlike every failure a Telegram call
    raises: this is a REFUSAL, not a call that failed. As a RuntimeError it was
    swallowed by the generic upload handler in :func:`publish_format` -- logged
    as "upload failed (will retry)" and counted as retryable work -- so a
    refusal to publish into a drifted set became a carry-on: the loop rolled
    ``set_index`` back over a set record it had already appended and adopted the
    same name again, leaving two sets under one name in the state file. The next
    run's :func:`load_state` then refused that file and the whole pack family
    could no longer be published at all. Only :func:`main` catches this, which
    is the one place that can turn it into a non-retryable stop.
    """


def _state_path(data_dir: Path, base: str) -> Path:
    return data_dir / f"publish_{base}.json"


def _plan_path(data_dir: Path, base: str) -> Path:
    return data_dir / f"publish_plan_{base}.json"


def _lock_path(data_dir: Path, base: str) -> Path:
    return data_dir / f"publish_{base}.lock"


def load_json(path: Path, default):
    """Load a state/plan file. ONLY an absent file may fall back to ``default``.

    A file that exists but cannot be parsed (truncated by a crash, edited by
    hand) used to be swallowed into the default -- i.e. "no plan, nothing
    published yet", which discards the frozen upload order and re-uploads the
    whole pack. Existing-but-unreadable has to stop the run instead.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(
            f"cannot read {path}: {exc}. Refusing to start from scratch -- that "
            f"would re-upload everything already published. Inspect or delete "
            f"the file deliberately, then re-run.") from exc


def save_json(path: Path, data) -> None:
    # Atomic: a half-written state file is exactly the corruption load_json now
    # refuses to start from.
    write_json_atomic(path, data)


def load_plan(data_dir: Path, base: str) -> dict:
    """The frozen plan ``{format: [content_key, ...]}``, shape-checked."""
    path = _plan_path(data_dir, base)
    plan = load_json(path, {})
    if not isinstance(plan, dict) or not all(
            isinstance(k, str) and isinstance(v, list)
            and all(isinstance(x, str) for x in v) for k, v in plan.items()):
        raise StateError(f"{path} is not a {{format: [content_key, ...]}} plan. "
                         f"Refusing to rebuild it: the frozen upload order is "
                         f"what makes resume duplicate-proof.")
    return plan


def load_state(data_dir: Path, base: str) -> dict:
    """Resume state for ``base``, fully shape-checked.

    A state file belonging to a DIFFERENT pack family records other packs' set
    names and upload order; publishing ``base`` from it would add this catalog
    to someone else's sets and renumber this one from zero.
    """
    path = _state_path(data_dir, base)
    state = load_json(path, {"base": base, "sets": [], "sent": []})
    if not isinstance(state, dict):
        raise StateError(f"{path} is not a publish-state object.")
    if state.setdefault("base", base) != base:
        raise StateError(f"{path} belongs to base {state['base']!r}, not {base!r}. "
                         f"Use a different --data-dir, or delete that file "
                         f"deliberately.")
    for field in ("sets", "sent", "skipped"):
        if not isinstance(state.setdefault(field, []), list):
            raise StateError(f"{path}: {field!r} must be a list.")
    _validate_state(state, path)
    return state


def _validate_state(state: dict, path: Path) -> None:
    """Reject resume state that cannot be trusted, BEFORE anything mutates.

    Modelled on :func:`build_pack.validate_state_shape`. Valid JSON is not valid
    state: a file can parse cleanly and still claim a negative live count, two
    sets sharing an index, one emoji recorded in two sets, or more recorded keys
    than the set holds stickers -- and every later decision is built on those
    numbers. Which set is active, how much room is left and, above all, which
    LIVE POSITION holds which key all come from here, so a set that records more
    keys than it has stickers hands the next emoji another one's
    custom_emoji_id. Checking only a name/format/index left every one of those
    through.
    """
    def bad(msg: str) -> StateError:
        return StateError(
            f"{path}: {msg} Refusing to publish from state that cannot be "
            f"trusted; inspect or delete the file deliberately, then re-run.")

    for field in ("sent", "skipped"):
        if not all(isinstance(x, str) and x for x in state[field]):
            raise bad(f"{field!r} must hold non-empty strings.")

    seen_names: set[str] = set()
    seen_keys: set[str] = set()
    last_index: dict[str, int] = {}          # highest index seen, per format
    for i, s in enumerate(state["sets"]):
        if not isinstance(s, dict):
            raise bad(f"sets[{i}] is not an object.")
        name, fmt, index = s.get("name"), s.get("fmt"), s.get("index")
        if not isinstance(name, str) or not name:
            raise bad(f"sets[{i}] has no name.")
        # MIXED is a real recorded value: a --mixed family stores one set list
        # under it. Leaving it out of this check made the validator reject the
        # state the publisher had just written itself.
        if fmt not in FMT_TAG and fmt != MIXED:
            raise bad(f"sets[{i}] ({name}) has unknown format {fmt!r}.")
        # bool is an int in Python, and JSON `true` must not pass as index 1.
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            raise bad(f"sets[{i}] ({name}) has a bad index {index!r}.")
        if name in seen_names:
            raise bad(f"sets[{i}] repeats the set name {name}.")
        if index <= last_index.get(fmt, 0):
            raise bad(f"sets[{i}] ({name}) index {index} does not follow "
                      f"{last_index.get(fmt, 0)} for format {fmt}.")
        seen_names.add(name)
        last_index[fmt] = index

        title = s.setdefault("title", name)
        if not isinstance(title, str) or not title:
            raise bad(f"sets[{i}] ({name}) has a bad title {title!r}.")
        logo = s.setdefault("logo", False)
        if not isinstance(logo, bool):
            raise bad(f"sets[{i}] ({name}) has a non-boolean 'logo' {logo!r}.")
        keys = s.setdefault("keys", [])
        if not isinstance(keys, list) or not all(
                isinstance(k, str) and k for k in keys):
            raise bad(f"sets[{i}] ({name}) 'keys' must be a list of item keys.")
        if len(set(keys)) != len(keys):
            raise bad(f"sets[{i}] ({name}) records the same emoji twice.")
        clash = sorted(seen_keys.intersection(keys))
        if clash:
            raise bad(f"sets[{i}] ({name}) records {clash[0]}, which an earlier "
                      f"set already claims.")
        seen_keys.update(keys)
        live = s.setdefault("live", 0)
        if isinstance(live, bool) or not isinstance(live, int) \
                or not 0 <= live <= PER_SET:
            raise bad(f"sets[{i}] ({name}) live count {live!r} is outside "
                      f"0..{PER_SET}.")
        recorded = (1 if logo else 0) + len(keys)
        if live < recorded:
            raise bad(f"sets[{i}] ({name}) claims {live} live sticker(s) but "
                      f"records {recorded}.")


def freeze_plan(cat: Catalog, data_dir: Path, base: str, formats: list[str]) -> dict:
    """Build/extend the frozen, append-only upload plan from the catalog.

    Existing order is preserved; only newly-catalogued keys are appended, so the
    already-uploaded prefix of every format stays stable across runs.
    """
    plan = load_plan(data_dir, base)
    for fmt in formats:
        # In mixed mode `formats` is [MIXED] and _all_items returns every
        # format in panel order, so the loop below needs no special case.
        existing = plan.get(fmt, [])
        have = set(existing)
        # Catalog rows in deterministic content_key order (matches Catalog.pending).
        ordered = [it.content_key for it in _all_items(cat, fmt)]
        appended = [k for k in ordered if k not in have]
        plan[fmt] = existing + appended
        if appended:
            log.info("plan[%s]: %d existing + %d new = %d", fmt, len(existing),
                     len(appended), len(plan[fmt]))
    save_json(_plan_path(data_dir, base), plan)
    return plan


def _all_items(cat: Catalog, fmt: str):
    """All catalog items of a format in deterministic order (uploaded or not)."""
    # Publish in the manual curate-panel order (position), content_key as a
    # stable tiebreak, so each format's set follows the order you arranged.
    if fmt == MIXED:
        # One list across every format, in exactly the order the panel saved --
        # which is the whole point of publishing mixed.
        rows = cat.db.execute(
            "SELECT * FROM items ORDER BY position, content_key").fetchall()
    else:
        rows = cat.db.execute(
            "SELECT * FROM items WHERE format=? ORDER BY position, content_key",
            (fmt,)).fetchall()
    from emojikit.catalog import _row_to_item  # local import to avoid cycle noise
    return [_row_to_item(r) for r in rows]


def _probe(tg, name: str) -> tuple[SetState, dict | None]:
    """Tri-state live probe: EXISTS / MISSING / UNKNOWN, plus the set when known.

    "Unknown" must stay distinct from "missing": collapsing a network failure
    into "the set is not there" is what justifies re-creating or re-uploading a
    set that is very much alive. Uses the non-retrying probe (getStickerSet via
    ``_call`` treats STICKERSET_INVALID as a name-release lock and sleeps
    minutes, which a lookup of a possibly-nonexistent set must never do).
    """
    probe = getattr(tg, "probe_set_state", None)
    if probe is not None:
        return probe(name)
    try:  # clients/fakes without the tri-state probe
        return SetState.EXISTS, tg.get_sticker_set(name)
    except Exception as exc:  # noqa: BLE001
        if "stickerset_invalid" in str(exc).lower():
            return SetState.MISSING, None
        return SetState.UNKNOWN, None


def _manifest_mismatch(tg, cat: Catalog, live: list[dict], keys: list[str],
                       offset: int, name: str, *, base: str,
                       tmp_dir: Path) -> str | None:
    """Why ``live`` no longer matches the recorded ``keys`` -- None if it does.

    EVERY recorded position must resolve BY IDENTITY to the key recorded there.
    There is no positional window, not even for a fresh upload. There used to
    be one: an unknown file_unique_id was accepted whenever that key had no
    stored custom_emoji_id yet, on the theory that Telegram re-encodes on upload
    so a fresh copy's id cannot be predicted. It cannot be predicted -- but it
    CAN be read back at the moment of the upload, which is what
    :func:`_confirm_new_upload` now does. Trusting order in the meantime is
    exactly how ``sol`` ended up on a Solama memecoin llama: reorder or replace
    a same-length set inside that window and a foreign sticker inherits our key,
    our publication record and our custom_emoji_id.

    So a position resolves by the recorded custom_emoji_id of that very
    sticker, by a recorded file_unique_id, or by downloading and
    content-hashing it (the owner may have re-uploaded the very same picture: a
    new id, but not a different emoji) -- or it is drift.
    """
    if len(live) < offset + len(keys):
        return (f"{name} holds {len(live)} sticker(s) but this publisher "
                f"recorded {offset + len(keys)}: emoji were removed from the set.")
    for i, key in enumerate(keys):
        st = live[i + offset]
        fuid, cid = _identity(st)
        if cid and cat.custom_emoji_id_for(base, key) == cid:
            continue                     # this exact live sticker is ours
        known = cat.seen_file_unique_id(fuid) if fuid else None
        if known == key:
            continue
        try:
            if _resolve_sticker_key(tg, cat, st, tmp_dir) == key:
                continue
        except Unresolvable as exc:
            # An unreadable position is not a confirmed mismatch, but it is also
            # not a pass: the custom_emoji_ids written after this point are
            # positional, so publishing on an unverified manifest is what points
            # a key at the wrong emoji.
            return (f"{name} position {i + offset} could not be examined "
                    f"({exc}), so this publisher cannot confirm it still holds "
                    f"{key}.")
        return (f"{name} position {i + offset} now holds "
                f"{known or 'a sticker this publisher cannot identify'}, but "
                f"this publisher recorded {key} there: the set was reordered, "
                f"replaced or edited by hand.")
    return None


def _set_is_open(s: dict) -> bool:
    """True while EVERY live sticker of ``s`` is one this publisher attributed.

    A live position nobody could attribute (the owner appended a sticker, or
    reconcile could not recognize one) gets no slot in ``keys``. Since
    :func:`_record_cids` maps ``keys[i]`` onto ``live[i + offset]``, anything
    added after that hole would hand a new key the foreign sticker's
    custom_emoji_id. Such a set is closed: publishing rolls to a new one.
    """
    return s.get("live", 0) == (1 if s.get("logo") else 0) + len(s.get("keys") or [])


# Telegram messages that describe the FILE, not the moment. They do not become
# true on a later run, so an item that earns one must stop being retried: the
# publisher logged "will retry" and did exactly that on every future run, for a
# file that can never be accepted -- and exited non-zero forever because of it.
# Deliberately narrow: a skip is permanent, and mislabelling a transient error
# loses an emoji. Only messages proven deterministic belong here.
#
# "wrong file type" was earned by a real .tgs whose only fault was a SUBTRACT
# mask (masksProperties[].mode == "s"). Telegram's uploader refuses those while
# its player shows them happily -- the same file downloaded from a live pack and
# sent back untouched is refused too, so it is not something this project broke.
_PERMANENT_FILE_REJECTIONS = ("wrong file type",)


def _file_is_permanently_rejected(exc: Exception) -> bool:
    """True when Telegram's complaint is about the bytes, not the moment."""
    msg = str(exc).lower()
    return any(m in msg for m in _PERMANENT_FILE_REJECTIONS)


class Unresolvable(Exception):
    """The sticker could not be looked at -- which is not "it is not ours".

    Downloading and hashing a live sticker can fail for reasons that say nothing
    about whose image it is: the fetch times out, getFile errors, ffmpeg is off
    PATH so a video/animated hash cannot be taken at all. Collapsing those into
    the same ``None`` that means "resolved, and it is not in our catalog" makes
    every caller read a failure to look as proof of absence -- and the callers
    act on absence by publishing another copy. The run that leaves a sticker
    unrecorded usually died of a network fault, so the recovery run is exactly
    when the fetch is least reliable, and on a host without ffmpeg the blindness
    is permanent rather than intermittent.
    """


# How far Telegram's own re-encode may move an image and still be OUR upload.
#
# Measured, not guessed: a 100x100 WEBP from this catalog came back from
# Telegram with 2304 of 16384 normalised bytes changed (mean delta 2.38) and a
# perceptual distance of 1 bit out of 64. Exact content_key equality therefore
# CANNOT hold for a fresh upload -- the publisher stopped on its own integrity
# guard before a single emoji was recorded.
#
# This does not weaken the guard. It is compared against ONE expected source --
# the file we just uploaded -- not searched across the catalog, so a false
# positive would have to be a foreign sticker that is visually that exact
# image. A foreign llama sits tens of bits away.
UPLOAD_PHASH_TOLERANCE = 6

# The same tolerance is NOT safe for a catalog-wide search. Verifying an upload
# compares against ONE expected file; reconciling an unknown live sticker asks
# "which of 200 is this?", and this catalog is full of near-identical marks --
# at 6 bits, two items matched and the run stopped as ambiguous, correctly.
#
# Measured on the live sticker: the right item sits at 0 bits (dHash shrugs off
# Telegram's re-encode, which moved the exact key), and the nearest rival at 5.
# 2 separates them with room, and anything closer than that on both sides would
# be reported as ambiguous rather than guessed.
SEARCH_PHASH_TOLERANCE = 2


def _same_image(tg, st: dict, source: Path, tmp_dir: Path) -> bool | None:
    """Is this live sticker the image in ``source``? None = could not tell.

    Exact first, because when Telegram's re-encode happens to be pixel-exact
    that is the strongest possible answer. Perceptual second, bounded.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"verify_{st.get('file_unique_id') or 'x'}.dl"
    try:
        fmt = media.telegram_sticker_format(st)
        tg.download_file(st["file_id"], tmp)
        if media.content_key(tmp, fmt) == media.content_key(source, fmt):
            return True
        a, b = media.perceptual_hash(tmp, fmt), media.perceptual_hash(source, fmt)
        if a is None or b is None:
            # Animated (vector) has no raster hash. Nothing further to compare,
            # and "I could not tell" must not read as "not ours".
            return None
        d = media.hamming(a, b)
        log.debug("upload verify: perceptual distance %d for %s", d, source.name)
        return d <= UPLOAD_PHASH_TOLERANCE
    except Exception as exc:  # noqa: BLE001 - a failed probe is not a "no"
        log.warning("upload verify failed for %s: %s", source.name, redact(str(exc)))
        return None
    finally:
        tmp.unlink(missing_ok=True)


def _near_catalog_match(cat: Catalog, path: Path, fmt: str):
    """The one catalog item this image is, within the re-encode tolerance.

    Returns the content_key, None for "no catalog item looks like this", or
    the string "ambiguous" when more than one does -- which the caller must
    treat as "I could not tell", never as a pick.

    Animated is vector and has no raster hash, so there is nothing to compare:
    its content key survives a re-gzip exactly, and a miss there is a real miss.
    """
    probe = media.perceptual_hash(path, fmt)
    if probe is None:
        return None
    close = [it.content_key for it in cat.all_items()
             if it.fmt == fmt and it.phash is not None
             and media.hamming(it.phash, probe) <= SEARCH_PHASH_TOLERANCE]
    if len(close) > 1:
        return "ambiguous"
    return close[0] if close else None


def _resolve_sticker_key(tg, cat: Catalog, st: dict, tmp_dir: Path) -> str | None:
    """Map a LIVE sticker back to its catalog content_key.

    Fast path: its ``file_unique_id`` was recorded (ingest or a previous
    publish). Slow path: download the sticker and content-hash it.

    Returns None ONLY for a proven negative: the content was resolved and no
    catalog item holds it. Raises ``Unresolvable`` when the answer could not be
    obtained, so a caller must decide deliberately instead of inheriting a
    silent "no"."""
    fuid = str(st.get("file_unique_id") or "")
    if fuid:
        known = cat.seen_file_unique_id(fuid)
        if known and cat.get(known) is not None:
            return known
    file_id = st.get("file_id")
    if not file_id or not hasattr(tg, "download_file"):
        raise Unresolvable(
            f"sticker {fuid or '<no id>'} carries no file_id to fetch"
            if not file_id else
            f"this Telegram client cannot download {fuid or file_id}")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"reconcile_{fuid or 'unknown'}.dl"
    tmp_kept = tmp
    try:
        tg.download_file(file_id, tmp)
        key = media.content_key(tmp, media.telegram_sticker_format(st))
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        log.warning("reconcile download failed (%s): %s", fuid or file_id,
                    redact(str(exc)))
        raise Unresolvable(
            f"could not fetch or hash {fuid or file_id}: {redact(str(exc))}"
        ) from exc
    if cat.get(key) is None:
        # Exact miss. Telegram re-encoded it, so for a raster format the key
        # cannot match -- fall back to the perceptual hash, but ONLY when the
        # answer is unambiguous. Two catalog items within tolerance means we
        # cannot tell which one this is, and guessing is what put a foreign
        # llama on `sol`; that is Unresolvable, not a negative.
        near = _near_catalog_match(cat, tmp_kept, media.telegram_sticker_format(st))
        if near == "ambiguous":
            raise Unresolvable(
                f"{fuid or file_id} is within the re-encode tolerance of more "
                f"than one catalog item; refusing to attribute it by guess")
        if near is None:
            tmp_kept.unlink(missing_ok=True)
            return None
        key = near
    tmp_kept.unlink(missing_ok=True)
    if fuid:
        cat.record_file_unique_id(fuid, key)
    return key


def _identity(st: dict) -> tuple[str, str]:
    """A live sticker's Telegram identity: (file_unique_id, custom_emoji_id).

    Both are assigned by Telegram and unique to one sticker, so either one
    distinguishes it from any other -- unlike its position in the set.
    """
    return (str(st.get("file_unique_id") or ""),
            str(st.get("custom_emoji_id") or ""))


def _live_index(tg, name: str) -> dict[tuple[str, str], dict]:
    """Live stickers of a set, keyed by identity.

    Raises instead of guessing: this snapshot is what tells our upload apart
    from everything else in the set, and a wrong snapshot means a wrong
    identity.
    """
    state, sset = _probe(tg, name)
    if state is SetState.UNKNOWN:
        raise LiveStateUnknown(f"live state of {name} is unknown")
    if state is SetState.MISSING:
        return {}
    return {_identity(st): st for st in sset.get("stickers", [])}


def _confirm_new_upload(tg, cat: Catalog, set_name: str, key: str,
                        before: dict[tuple[str, str], dict],
                        tmp_dir: Path) -> dict:
    """Identify the sticker an upload just created -- by identity, not position.

    Telegram re-encodes on upload, so the new copy's identity cannot be
    predicted; it can only be READ BACK, which is what this does. ``before`` is
    the set's identities from immediately before the mutation, so exactly one
    new identity must have appeared. None means the add did not land; several
    mean somebody else wrote to the set at the same time. In both cases which
    sticker is ours would be a guess, and guessing is what put a foreign llama
    on ``sol``.

    Recording that identity is what closes the fresh-upload window for good:
    from here on the position is checked by identity on every later run (see
    :func:`_manifest_mismatch`), so a reorder or a replacement before the first
    read-back is drift instead of a silent re-pointing.
    """
    new = [st for ident, st in _live_index(tg, set_name).items()
           if ident not in before and any(ident)]
    if len(new) != 1:
        raise SetDrift(
            f"cannot identify the sticker just uploaded for {key} in "
            f"{set_name}: {len(new)} new identities appeared, expected exactly "
            f"one. Refusing to attribute it by position.")
    st = new[0]
    # "Exactly one new sticker" is still not "OUR new sticker": if our add
    # failed while an external or manual one landed, exactly one new identity
    # also appears. Resolve the candidate's CONTENT and require it to be this
    # key before anything durable is written -- otherwise a foreign FUID and
    # CID get bound to our catalog entry permanently.
    # Compared against the file we JUST uploaded, not searched across the
    # catalog by exact hash. Telegram re-encodes on upload, so the exact key
    # cannot survive -- measured on this catalog: 14% of normalised bytes
    # changed, perceptual distance 1 of 64 bits. Searching by exact key made
    # every fresh upload "an unidentifiable image" and stopped the publish
    # before a single emoji was recorded.
    item = cat.get(key)
    if item is None:
        raise SetDrift(f"{key} is no longer in the catalog; refusing to "
                       f"attribute a live sticker to a missing item.")
    same = _same_image(tg, st, Path(item.file_path), tmp_dir)
    if same is None:
        raise SetDrift(
            f"the sticker that appeared in {set_name} while uploading {key} "
            f"could not be examined, so it cannot be proven to be ours. "
            f"Nothing was recorded; re-run to reconcile it from live state.")
    if not same:
        raise SetDrift(
            f"the sticker that appeared in {set_name} while uploading {key} "
            f"is not that image. Our upload did not land, or someone else "
            f"wrote to this set; refusing to record a foreign sticker as ours.")
    fuid = str(st.get("file_unique_id") or "")
    owner = cat.seen_file_unique_id(fuid) if fuid else None
    if owner is not None and owner != key:
        raise SetDrift(
            f"the sticker just uploaded for {key} in {set_name} is already "
            f"known as {owner}: refusing to attribute one live sticker to two "
            f"emoji.")
    if fuid:
        cat.record_file_unique_id(fuid, key)
    return st


def reconcile_set(tg, cat: Catalog, s: dict, data_dir: Path, base: str) -> int:
    """Sync one set's records with its LIVE stickers; returns the live count.

    Any live sticker beyond what the state recorded is an upload a previous
    run (or an ambiguous network failure in this run) applied without
    recording it. Each one is attributed back to its catalog item by
    file_unique_id or downloaded content and marked uploaded, so pending
    computations can NEVER upload it a second time.

    The WHOLE recorded manifest is validated, not just that tail: a sticker
    deleted, replaced or reordered inside the recorded prefix is invisible to a
    tail-only check, yet it silently re-points every custom_emoji_id written
    afterwards and shifts the live capacity used for the next upload."""
    state, sset = _probe(tg, s["name"])
    if state is SetState.UNKNOWN:
        # Returning the stale recorded count here is a guess, and the caller
        # mutates on it (it is the live capacity of the next add).
        raise LiveStateUnknown(
            f"live state of {s['name']} is unknown; not touching it")
    if state is SetState.MISSING:
        raise SetDrift(
            f"{s['name']} no longer exists on Telegram, but this publisher "
            f"recorded {len(s.get('keys') or [])} emoji in it. Refusing to "
            f"guess: restore the set, or clear this pack family "
            f"(Catalog.forget_publication({base!r})) and delete "
            f"{_state_path(data_dir, base).name} to rebuild it.")
    live = sset.get("stickers", [])
    keys = s.setdefault("keys", [])
    offset = 1 if s.get("logo") else 0
    why = _manifest_mismatch(tg, cat, live, keys, offset, s["name"], base=base,
                             tmp_dir=data_dir / "tmp")
    if why:
        raise SetDrift(f"{why} Refusing to publish into a set that no longer "
                       f"matches its manifest.")
    start = offset + len(keys)
    tail = live[start:]
    for i, st in enumerate(tail):
        try:
            key = _resolve_sticker_key(tg, cat, st, data_dir / "tmp")
        except Unresolvable as exc:
            # NOT the same as "it is not ours". Breaking here would close the
            # set and roll publishing to a new one -- and if this sticker was in
            # fact ours, that publishes a second live copy of it. Refuse: an
            # unreadable position is a question, and the answer decides whether
            # an emoji is duplicated.
            raise SetDrift(
                f"{s['name']} position {start + i} could not be examined "
                f"({exc}). Refusing to decide whether it is ours by assuming it "
                f"is not; retry when Telegram and the media tools are "
                f"reachable.") from exc
        if key is None:
            # Attribution is positional (the cid mapping is), so it stops at the
            # first sticker we cannot recognize -- normally one the owner
            # appended. But if any of OUR emoji sit behind it, our positions
            # were shifted by a hand-INSERTED sticker, and stopping quietly
            # would leave the shifted item live yet unrecorded: still pending,
            # so uploaded a second time.
            #
            # The look-behind resolves by CONTENT, not by id: an emoji THIS
            # program uploaded moments before the run died never got its
            # file_unique_id recorded, so an id-only lookup answers "nothing of
            # ours behind" for the very sticker the next run is about to upload
            # into a new set. Bound: the rest of the tail, stopping at the first
            # hit -- exactly the stickers this loop would have downloaded anyway
            # had the foreign one not been sitting in front of them, so a full
            # scan costs no more than the ordinary path already does.
            for j, other in enumerate(tail[i + 1:], i + 1):
                try:
                    mine = _resolve_sticker_key(tg, cat, other, data_dir / "tmp")
                except Unresolvable as exc:
                    # The look-behind is a safety net; a net that reports "empty"
                    # when it could not look is worse than none, because the
                    # caller acts on the empty answer by publishing again.
                    raise SetDrift(
                        f"{s['name']} position {start + i} is unrecognized and "
                        f"position {start + j} behind it could not be examined "
                        f"({exc}). Refusing to conclude that none of this "
                        f"publisher's emoji sit behind it.") from exc
                if mine is None:
                    continue
                raise SetDrift(
                    f"{s['name']} position {start + i} holds a sticker this "
                    f"publisher cannot recognize, yet its own {mine} sits "
                    f"behind it at position {start + j}: the set was edited by "
                    f"hand. Refusing to attribute by position.")
            log.warning("[%s] unrecognized live sticker in %s at position %d; "
                        "stopping attribution there", s.get("fmt", "?"),
                        s["name"], start + i)
            break
        if key in keys:
            # Already placed earlier in the manifest, so this is not "an upload
            # we forgot to record" -- the emoji is live twice.
            raise SetDrift(
                f"{s['name']} holds {key} at both position "
                f"{offset + keys.index(key)} and {start + i}; refusing to "
                f"attribute one emoji twice.")
        if not cat.is_published(base, key):
            log.info("[%s] reconciled from live: %s was already uploaded to %s",
                     s.get("fmt", "?"), key, s["name"])
        cat.mark_uploaded(key, str(st.get("custom_emoji_id") or "") or None,
                          base=base, set_name=s["name"])
        fuid = str(st.get("file_unique_id") or "")
        if fuid:
            cat.record_file_unique_id(fuid, key)
        keys.append(key)
    s["live"] = len(live)
    return len(live)


# One probe per file plus this pause; a 200-emoji queue takes about a minute.
PREFLIGHT_DELAY = 0.15


def pending_keys(cat: Catalog, plan: dict, fmt: str, base: str,
                 skipped: set[str]) -> list[str]:
    """The keys this run would actually upload for one format.

    The publisher, the dry run and the preflight all have to agree on what is
    queued, or each reports a different number for the same catalog.
    """
    return [k for k in plan.get(fmt, [])
            if (it := cat.get(k)) and it.included
            and not cat.is_published(base, k) and k not in skipped]


def preflight(tg: Telegram, cat: Catalog, user_id: int, plan: dict,
              formats: list[str], base: str, skipped: set[str]) -> int:
    """Ask Telegram to validate every queued file before anything is published.

    ``uploadStickerFile`` runs the same validator as ``addStickerToSet`` and
    touches no set, so a file Telegram will refuse can be found in seconds
    instead of at whatever minute of the publish it happens to reach. One `.tgs`
    with a subtract mask was found 46 minutes into a run, after 99 uploads and
    two flood waits, and it would have been the very first thing this reported.

    A refusal here is the file's own problem, not the pack's: it is reported and
    the run is NOT started, so nothing is half-published while you fix it.
    """
    refused: list[tuple[str, str, str]] = []
    checked = 0
    for fmt in formats:
        for key in pending_keys(cat, plan, fmt, base, skipped):
            it = cat.get(key)
            path = Path(it.file_path)
            if not path.is_file():
                refused.append((key, path.name, "file is missing on disk"))
                continue
            checked += 1
            try:
                tg.check_uploadable(user_id, path, it.fmt)
            except BotApiError as exc:
                refused.append((key, path.name, redact(str(exc))))
            except RuntimeError as exc:
                # Transport, not a verdict. Saying "bad file" here would send
                # someone editing artwork over a dropped connection.
                log.warning("could not check %s: %s", key, redact(str(exc)))
            time.sleep(PREFLIGHT_DELAY)

    print(f"\nPREFLIGHT: {checked} file(s) checked, {len(refused)} refused.",
          flush=True)
    for key, name, why in refused:
        print(f"  REFUSED {key}  ({name})\n          {why}", flush=True)
        log.error("preflight: Telegram refuses %s (%s): %s", key, name, why)
    if refused:
        print("\nNothing was published. Fix or deselect these, then publish.\n",
              flush=True)
        return EXIT_FAILED
    print("Every queued file is acceptable to Telegram.\n", flush=True)
    return EXIT_OK


def notify(tg: Telegram, user_id: int, state: dict, data_dir: Path, base: str,
           name: str, title: str) -> None:
    if name in state["sent"]:
        return
    # With a Worker deployed, the BOT posts the announcement and this process
    # never talks to the channel. `state["sent"]` still guards it, so which path
    # sent it does not change whether a re-run announces twice.
    try:
        dest = announce_packs(tg, user_id, [{"name": name, "title": title}],
                              bot="general")
        state["sent"].append(name)
        save_json(_state_path(data_dir, base), state)
        log.info("sent link for %s to %s", name, dest)
    except Exception as exc:  # noqa: BLE001
        log.warning("notify failed for %s: %s", name, redact(str(exc)))


_FRAME_BYTES = 64 * 64 * 4          # one sampled RGBA frame


def _video_is_blank(path: Path, min_visible: int = 8) -> bool:
    """True only when EVERY sampled frame of a video is effectively empty.

    The first frame alone is not evidence: any animation that fades in, or
    simply starts on an empty canvas, has a transparent frame 0 -- and the
    verdict is written to ``skipped``, so that emoji is never published again.
    One ffmpeg pass samples the whole (<=3 s) clip.
    """
    cmd = [media.ffmpeg_path(), "-v", "error", "-t", str(media.WEBM_MAX_SECONDS),
           "-i", str(path), "-an", "-vf", "fps=4,scale=64:64,format=rgba",
           "-f", "rawvideo", "-"]
    # Bounded child, like every other ffmpeg call in the project: media._run
    # enforces the wall limit and kills the whole process tree on timeout. A
    # bare subprocess.run had no timeout at all, so one corrupt clip could
    # freeze the publish indefinitely -- exactly what that runner exists for.
    raw = media._run(cmd, capture=True).stdout
    if len(raw) < _FRAME_BYTES:
        return False                # nothing decoded: let the upload decide
    for start in range(0, len(raw) - _FRAME_BYTES + 1, _FRAME_BYTES):
        alpha = raw[start + 3:start + _FRAME_BYTES:4]
        if sum(1 for v in alpha if v > 10) > min_visible:
            return False
    return True


def _media_ok(path: Path, fmt: str) -> bool:
    """False if the media is effectively blank (guards against blank emoji)."""
    if fmt == "static":
        return not _static_is_blank(path)
    if fmt == "video":
        try:
            return not _video_is_blank(path)
        except Exception:  # noqa: BLE001 - probing failed; let the upload decide
            return True
    return True  # animated (.tgs) validity is enforced at creation time


def write_manifest(data_dir: Path, cat: Catalog, s: dict, base: str) -> None:
    """Write a per-pack manifest: emoji name (keywords) + custom_emoji_id."""
    keys = s.get("keys") or []
    if not keys:
        return
    md = data_dir / "manifests"
    md.mkdir(parents=True, exist_ok=True)
    # The brand logo is the set's first sticker but not a catalog item, so
    # len(keys) is one short of what is actually in the pack. Reporting the
    # short number here made the manifest say 199 for a 200-emoji pack, which
    # is the internal row count and not what anyone opening this file wants.
    logo = 1 if s.get("logo") else 0
    lines = [f"# {s.get('title', s['name'])}", "",
             f"Pack: https://t.me/addemoji/{s['name']}  |  format: {s['fmt']}"
             f"  |  {len(keys) + logo} emoji",
             "", "| # | Name | Emoji ID |", "|---|------|----------|"]
    if logo:
        lines.append("| 1 | brand logo | |")
    for i, key in enumerate(keys, 1 + logo):
        it = cat.get(key)
        name = ", ".join(it.keywords[:2]) if it and it.keywords else (
            it.sources[0] if it and it.sources else key)
        cid = (cat.custom_emoji_id_for(base, key) if it else "") or ""
        lines.append(f"| {i} | {name} | {cid} |")
    (md / f"{s['name']}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("manifest written: manifests/%s.md (%d emoji)",
             s["name"], len(keys) + logo)


def publish_format(tg: Telegram, cat: Catalog, *, fmt: str, plan_keys: list[str],
                   base: str, title: str, user_id: int, default_emoji: str,
                   per_set: int, data_dir: Path, state: dict, bot: str,
                   logo: "BrandLogo | None" = None,
                   new_set: bool = False) -> tuple[int, int]:
    """Publish all pending items of one format; returns (uploaded, failed).

    Duplicate-proof: "already uploaded" is decided by the catalog's per-item
    (committed) ``uploaded`` flag, NOT by a positional offset, so skipped items
    can never shift the boundary and cause a re-upload on resume.

    ``failed`` counts items that stayed pending because an upload errored or
    could not be confirmed -- i.e. work a later run must retry. Items dropped
    by :func:`skip` are a recorded permanent decision (missing or blank media),
    not retryable work, so they are not counted as failures.
    """
    if not plan_keys:
        return 0, 0
    fmt_sets = [s for s in state["sets"] if s["fmt"] == fmt]
    skipped = set(state.setdefault("skipped", []))

    # Reconcile the ACTIVE (last, non-full) set against its LIVE content
    # before anything else: uploads applied by a previous run but never
    # recorded (crash or ambiguous network failure) are attributed back to
    # their catalog items here, so the pending computation below can never
    # upload them a second time. This also refreshes the live capacity.
    if fmt_sets:
        reconcile_set(tg, cat, fmt_sets[-1], data_dir, base)
        save_json(_state_path(data_dir, base), state)
    # A set holding a position we could not attribute is CLOSED: see
    # _set_is_open. Rolling to a fresh set is the only way to keep keys[] and
    # the live positions aligned once a foreign sticker sits between them.
    cur = fmt_sets[-1] if fmt_sets else None
    if cur and not _set_is_open(cur):
        log.warning("[%s] %s holds unattributed live sticker(s); publishing "
                    "continues in a new set", fmt, cur["name"])
        cur = None
    # Without this the only way to reach set N+1 is to fill set N to `per_set`:
    # a pack the owner wants to leave half-empty (to start the next one with
    # different content) had no supported path, and the workarounds -- a smaller
    # --per-set, or a second base -- either cap every LATER set at the same
    # wrong size or re-upload the whole catalog into a new family. Deliberately
    # reuses the closed-set path above rather than adding a second way to roll,
    # and is consulted once per run: later items follow `in_set` as usual.
    if cur and new_set:
        log.info("[%s] --new-set: %s stays at %d live; this run starts a fresh "
                 "set", fmt, cur["name"], cur.get("live", 0))
        cur = None
    if cur and cur["live"] < per_set:
        set_index, set_name, in_set = cur["index"], cur["name"], cur["live"]
    else:
        set_index = max((s["index"] for s in fmt_sets), default=0)
        set_name, in_set = "", 0

    # Pending = plan keys neither already uploaded, excluded in the panel, nor
    # permanently skipped.
    # "Already uploaded" is per pack family: a global flag meant publishing to
    # one base marked the items done for every other base too.
    pending = [k for k in plan_keys
               if (it := cat.get(k)) and it.included
               and not cat.is_published(base, k) and k not in skipped]
    log.info("[%s] %d sets, active in_set=%d, %d pending (of %d planned)",
             fmt, len(fmt_sets), in_set, len(pending), len(plan_keys))

    def skip(key: str, why: str) -> None:
        log.warning("[%s] skip %s: %s", fmt, key, why)
        skipped.add(key)
        state["skipped"] = sorted(skipped)
        save_json(_state_path(data_dir, base), state)

    n = failed = 0
    skipped_at_start = len(skipped)   # only THIS run's skips block the link
    for key in pending:
        item = cat.get(key)
        if item is None or cat.is_published(base, key):
            continue  # attributed by an in-run reconcile after an ambiguous failure
        path = Path(item.file_path)
        if not path.is_file() or path.stat().st_size == 0:
            skip(key, "missing/empty media")
            continue
        if not _media_ok(path, fmt):
            skip(key, "blank media (no blank emoji)")
            continue
        emojis = item.emojis or [default_emoji]
        # The set's live identities BEFORE this upload: what the copy it creates
        # is identified against afterwards (see _confirm_new_upload). A create
        # starts from nothing, so an empty snapshot is the correct baseline.
        before: dict[tuple[str, str], dict] = {}
        try:
            placed = False
            if in_set != 0:
                before = _live_index(tg, set_name)
                try:
                    tg.add_emoji(user_id, set_name, path, item.fmt, emojis,
                                 item.keywords, expected_before=in_set)
                    placed = True
                except RuntimeError as exc:
                    if "STICKERS_TOO_MUCH" not in str(exc):
                        raise
                    in_set = 0
            if not placed:
                before = {}             # the create below starts from nothing
                set_index += 1
                set_name = f"{base}{FMT_TAG.get(fmt, '')}{set_index}_by_{bot}"
                # Numbered across ALL formats in creation order, so the
                # owner sees "<title> 1, 2, 3" and not three separate
                # sequences with a format word in each. state["sets"]
                # holds every set already created, so a resumed run
                # continues the count instead of restarting it.
                set_title = f"{title} {len(state['sets']) + 1}"
                logo_png = logo.static_png() if logo else None
                adopted = False
                try:
                    if logo_png:
                        # Brand logo is ALWAYS the first emoji of the set. Mixed-format
                        # sets are allowed (Bot API 7.2+), so a STATIC logo can lead a
                        # static, video or animated set alike.
                        tg.create_emoji_set(user_id, set_name, set_title, logo_png,
                                            "static", [BRAND_LOGO_EMOJI], BRAND_LOGO_KW)
                    else:
                        tg.create_emoji_set(user_id, set_name, set_title, path,
                                            item.fmt,
                                            emojis, item.keywords)
                except RuntimeError as exc:
                    # If an earlier attempt of THIS name actually landed (network
                    # failure after apply, or a leftover from a crashed run), the
                    # set exists: adopt it instead of erroring forever on
                    # "name is already occupied" or re-creating it.
                    if not (isinstance(exc, AmbiguousUploadError)
                            or "occupied" in str(exc).lower()):
                        raise
                    # MISSING: the create really failed. UNKNOWN: never adopt a
                    # set on a guess -- report the original failure and retry.
                    if _probe(tg, set_name)[0] is not SetState.EXISTS:
                        raise
                    adopted = True
                fmt_sets.append({"fmt": fmt, "index": set_index, "name": set_name,
                                 "title": set_title, "live": 1 if logo_png else 0,
                                 "logo": bool(logo_png), "keys": []})
                state["sets"].append(fmt_sets[-1])
                save_json(_state_path(data_dir, base), state)
                log.info("[%s set %d] %s %s%s", fmt, set_index,
                         "adopted" if adopted else "created", set_name,
                         " (brand logo first)" if logo_png else "")
                if adopted:
                    # Attribute whatever the set already contains (the earlier
                    # create put SOMETHING there), then re-check this item.
                    in_set = reconcile_set(tg, cat, fmt_sets[-1], data_dir, base)
                    save_json(_state_path(data_dir, base), state)
                    if cat.is_published(base, key):
                        continue  # this very item was the set's first sticker
                    if not _set_is_open(fmt_sets[-1]):
                        log.warning("[%s] %s has unattributed live stickers; not "
                                    "adding %s yet (will retry)", fmt, set_name, key)
                        failed += 1
                        continue
                    before = _live_index(tg, set_name)
                    tg.add_emoji(user_id, set_name, path, item.fmt, emojis,
                                 item.keywords, expected_before=in_set)
                elif logo_png:
                    # Set now exists with the logo at position 0; protect it from
                    # index rollback, then place this item as the second sticker.
                    in_set = 1
                    before = _live_index(tg, set_name)
                    tg.add_emoji(user_id, set_name, path, item.fmt, emojis,
                                 item.keywords, expected_before=in_set)
        except AmbiguousUploadError as exc:
            # The add/create may or may not be live. NEVER blind-retry (that is
            # exactly how the same emoji lands in a pack twice) -- reconcile the
            # live set now; if the item did land it gets marked uploaded, and
            # if not it stays pending for a later run.
            log.warning("[%s] %s for %s; reconciling live set", fmt, exc, key)
            if not placed and in_set == 0:
                set_index -= 1  # the create never registered a set
            elif fmt_sets:
                in_set = reconcile_set(tg, cat, fmt_sets[-1], data_dir, base)
                save_json(_state_path(data_dir, base), state)
                if not _set_is_open(fmt_sets[-1]):
                    in_set = 0   # closed by an unattributed position: new set
            if not cat.is_published(base, key):
                failed += 1      # still pending -> a later run must retry it
            continue
        except RuntimeError as exc:
            if not placed and in_set == 0:
                set_index -= 1
            if _file_is_permanently_rejected(exc):
                # Not a bad moment -- a bad FILE. The same bytes fail every run,
                # so "will retry" means retrying forever and never finishing.
                skip(key, f"Telegram refuses this file: {redact(str(exc))}")
                continue
            # Transient/non-blank failure: log and retry on a later run (NOT
            # added to skipped), while the catalog flag keeps it dup-proof.
            log.warning("[%s] upload failed for %s (will retry): %s", fmt, key, redact(str(exc)))
            failed += 1
            continue
        # Identity BEFORE any record: keys[], the publication row and the
        # custom_emoji_id all map this key onto a live position, so the position
        # has to be proven ours first, not assumed from order.
        st = _confirm_new_upload(tg, cat, set_name, key, before,
                                 data_dir / "tmp")
        in_set += 1
        fmt_sets[-1]["live"] = in_set
        # Record actual upload order (for cid mapping) + mark uploaded (committed
        # immediately -> crash-safe duplicate guard). The cid comes from the
        # sticker just identified, so it is right even if the set drifts before
        # _record_cids reads it back.
        fmt_sets[-1].setdefault("keys", []).append(key)
        cat.mark_uploaded(key, str(st.get("custom_emoji_id") or "") or None,
                          base=base, set_name=set_name)
        n += 1
        # One line per sticker that actually landed. Only FAILURES used to be
        # logged, so a healthy run wrote its plan and then nothing for the next
        # hour -- indistinguishable from a hung process, and useless afterwards
        # for answering "when did this emoji go in". The count is bounded by the
        # work itself, and this is the record that makes a resume auditable.
        log.info("[%s] uploaded %s -> %s #%d (%d/%d this run)",
                 fmt, key, set_name, in_set, n, len(pending))
        if n % 20 == 0:
            save_json(_state_path(data_dir, base), state)
        if in_set >= per_set:
            # The RECORDED title, not a rebuilt one: rebuilding it here is how
            # the announcement and the actual set name drift apart.
            notify(tg, user_id, state, data_dir, base, set_name,
                   fmt_sets[-1]["title"])
            in_set = 0
        time.sleep(0.1)

    save_json(_state_path(data_dir, base), state)
    # Assign real custom_emoji_ids (drift-proof, from recorded order) + manifests.
    _record_cids(tg, cat, fmt_sets, base, data_dir)
    for s in fmt_sets:
        write_manifest(data_dir, cat, s, base)

    # The trailing set is announced ONLY by a run that finished cleanly. This
    # used to be unconditional, so a run that ended 199 of 200 -- one emoji
    # refused by Telegram -- still posted the pack link to the channel as if it
    # were finished. A link means "this pack is done"; publishing one for a pack
    # that is still missing an emoji, or that hit an error, is a false claim.
    #
    # A skip counts as incomplete just like a failure: with permanent-refusal
    # classification an unpublishable file no longer raises, and without this it
    # would turn the very case that caused the complaint into a silent success.
    #
    # Nothing is lost by withholding it -- `state["sent"]` never records it, so
    # the next clean run announces it. A set that filled to capacity mid-run was
    # already announced above, and that one IS complete by definition.
    incomplete = failed + (len(skipped) - skipped_at_start)
    if fmt_sets and not incomplete:
        last = fmt_sets[-1]
        notify(tg, user_id, state, data_dir, base, last["name"], last["title"])
    elif fmt_sets:
        log.warning("[%s] not announcing %s: %d item(s) did not make it into "
                    "this run. The link is posted once a run completes cleanly.",
                    fmt, fmt_sets[-1]["name"], incomplete)
    return n, failed


def _record_cids(tg: Telegram, cat: Catalog, fmt_sets: list[dict], base: str,
                 data_dir: Path) -> None:
    """Store each item's real custom_emoji_id, verified against the live set.

    A bare positional mapping is wrong the moment the owner deletes, reorders or
    replaces a sticker: key[i] and live[i+offset] are then different emoji, and
    every id we publish (manifests, the bot, remaps) points at the wrong
    picture. The ids are taken only from a manifest that still matches by
    identity -- see :func:`_manifest_mismatch`.

    Every recorded set is checked here, not just the active one, and a set that
    cannot be read is never shrugged off: warning and continuing turned a pack
    that had been DELETED into a clean success, and a network blip into a
    permanent "done" for ids that were never written.
    """
    for s in fmt_sets:
        keys = s.get("keys") or []
        if not keys:
            continue
        state, sset = _probe(tg, s["name"])
        if state is SetState.MISSING:
            raise SetDrift(
                f"{s['name']} no longer exists on Telegram, but this publisher "
                f"recorded {len(keys)} emoji in it. Refusing to report a "
                f"complete publication for a pack that is gone.")
        if state is SetState.UNKNOWN:
            raise LiveStateUnknown(
                f"live state of {s['name']} is unknown; its custom_emoji_ids "
                f"were not written")
        live = sset.get("stickers", [])
        offset = 1 if s.get("logo") else 0  # skip the brand logo at position 0
        why = _manifest_mismatch(tg, cat, live, keys, offset, s["name"],
                                 base=base, tmp_dir=data_dir / "tmp")
        if why:
            raise SetDrift(f"{why} Refusing to write custom_emoji_ids that "
                           f"would point at the wrong emoji.")
        for i, key in enumerate(keys):
            st = live[i + offset]
            cat.mark_uploaded(key, str(st.get("custom_emoji_id")),
                              base=base, set_name=s["name"])
            # Remember the uploaded copy's file_unique_id: it is what makes the
            # manifest check above identity-based on every later run, and it
            # lets a later fetch of our own pack skip the download.
            fuid = str(st.get("file_unique_id") or "")
            if fuid:
                cat.record_file_unique_id(fuid, key)


# Telegram's own rule for a sticker-set name: English letters, digits and
# underscores, must begin with a letter, NO consecutive underscores, must end in
# "_by_<bot_username>", 1-64 characters. This validates the part we choose; the
# "_by_<bot>" tail is appended for us and is not optional -- Telegram rejects a
# name without it.
_BASE_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*")
# The longest suffix a base can pick up: one format letter, a set index, and
# "_by_" plus the bot username. Checked against the real username at publish
# time; this is the static part.
_NAME_MAX = 64


def valid_base(base: str) -> str:
    """The chosen part of a set name, checked against Telegram's rule.

    Underscores are allowed -- this used to reject them, which is stricter than
    Telegram and refuses a perfectly legal name like YourBrand_Emoji_Packs.
    Consecutive underscores are not, and neither is a trailing one, because
    "<base>_" + "1_by_..." is fine but "<base>_" + "_by_..." is not, and the
    rule is easier to hold as "single underscores between parts".
    """
    if not _BASE_RE.fullmatch(base):
        raise SystemExit(
            "ERROR: --base must begin with a letter and contain only letters, "
            "digits and single underscores between them (Telegram's rule for a "
            "sticker-set name). Examples: 'mypack', 'YourBrand_Emoji_Packs'.")
    return base


def check_name_length(base: str, bot: str, tag: str = "") -> None:
    """Refuse a base that cannot fit Telegram's 64-character set name.

    Caught here rather than as a Bot API error on the first upload, which is
    after the plan is frozen and the run has already started.
    """
    longest = f"{base}{tag}999_by_{bot}"
    if len(longest) > _NAME_MAX:
        raise SystemExit(
            f"ERROR: --base '{base}' is too long: the set name would reach "
            f"{len(longest)} characters ('{longest}') and Telegram allows "
            f"{_NAME_MAX}. Shorten --base by {len(longest) - _NAME_MAX}.")


def parse_formats(raw: str) -> list[str]:
    """Validate --formats. Dropping unknown values silently made ``--formats
    garbage`` a successful run that published nothing at all."""
    parts = [f.strip() for f in raw.split(",")]
    if any(f not in FMT_TAG for f in parts) or len(set(parts)) != len(parts):
        raise ValueError(f"--formats must be a comma list of "
                         f"{'/'.join(FMT_TAG)} with no duplicates or blanks; "
                         f"got {raw!r}.")
    return parts


def main(argv: list[str] | None = None) -> int:
    load_env()
    setup_logging("build_collection")
    ap = argparse.ArgumentParser(description="Publish the catalog into new emoji packs.")
    ap.add_argument("--base", required=True, help="Set-name base (letters/digits).")
    ap.add_argument("--title", required=True, help="Human-readable set title.")
    ap.add_argument("--token-env", default="GENERAL_BOT_TOKEN")
    ap.add_argument("--user-id", type=int,
                    default=safe_int_env("PACK_OWNER_USER_ID", 0, minimum=0))
    ap.add_argument("--emoji", default=DEFAULT_EMOJI, help="Fallback associated emoji.")
    ap.add_argument("--mixed", action="store_true",
                    help="Publish every format into ONE family named "
                         "<base><n>_by_<bot>, in the curate panel's order. "
                         "Without it each format gets its own sets, which "
                         "regroups a hand-arranged pack into format blocks.")
    ap.add_argument("--formats", default="static,video,animated",
                    help="Comma list of formats to publish, in order.")
    ap.add_argument("--per-set", type=int, default=PER_SET)
    ap.add_argument("--new-set", action="store_true",
                    help="Start this run in a FRESH set instead of filling the "
                         "current one. Use to begin the next pack while the "
                         "one before it is deliberately left unfinished.")
    ap.add_argument("--data-dir", default="collection")
    ap.add_argument("--brand-logo", default=BRAND_LOGO_DEFAULT,
                    help="Logo image used as the FIRST emoji of every set built "
                         "by the Emoji Mapper bot (ignored for the coin bot).")
    ap.add_argument("--no-brand-logo", action="store_true",
                    help="Disable the mandatory first-emoji brand logo.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--preflight", action="store_true",
                    help="Ask Telegram to validate every queued file, "
                         "then stop. Publishes nothing.")
    args = ap.parse_args(argv)

    base = valid_base(args.base)
    try:
        formats = [MIXED] if args.mixed else parse_formats(args.formats)
    except ValueError as exc:
        log.error("%s", exc)
        return EXIT_USAGE
    if not 1 <= args.per_set <= PER_SET:
        log.error("--per-set must be between 1 and %d (Telegram's cap for a "
                  "custom-emoji set); got %d.", PER_SET, args.per_set)
        return EXIT_USAGE
    data_dir = (ROOT / args.data_dir) if not os.path.isabs(args.data_dir) else Path(args.data_dir)
    db = data_dir / "catalog.db"
    if not db.is_file():
        log.error("no catalog at %s (run fetch_pack.py / add_media.py first).", db)
        return EXIT_USAGE

    # The brand logo is the first emoji of every set it leads, so it costs one
    # slot per set. Which bot runs is only known after getMe, so the dry-run
    # assumes the logo is used whenever it is enabled and present.
    logo_planned = bool(not args.no_brand_logo and Path(args.brand_logo).is_file())
    capacity = args.per_set - (1 if logo_planned else 0)
    if capacity < 1:
        log.error("--per-set %d leaves no room for the brand logo; use at least "
                  "2, or pass --no-brand-logo.", args.per_set)
        return EXIT_USAGE

    try:
        # One publisher per pack family: two runs would read the same state, see
        # the same items pending, and both upload them.
        with exclusive_lock(_lock_path(data_dir, base)):
            return _publish(args, base=base, formats=formats, data_dir=data_dir,
                            db=db, capacity=capacity, logo_planned=logo_planned)
    except LockBusy as exc:
        log.error("%s", exc)
        return EXIT_FAILED
    except (StateError, SetDrift) as exc:
        log.error("%s", exc)
        return EXIT_FAILED
    except LiveStateUnknown as exc:
        log.error("%s. Nothing further was changed; retry when Telegram is "
                  "reachable.", exc)
        return EXIT_PARTIAL


def _publish(args, *, base: str, formats: list[str], data_dir: Path, db: Path,
             capacity: int, logo_planned: bool) -> int:
    """Publish (or dry-run) one pack family, with its lock already held."""
    # Validated FIRST, before the catalog adopts legacy publication records, the
    # frozen plan is rewritten or a single Telegram call is made: every one of
    # those acts on the numbers in this file.
    state = load_state(data_dir, base)
    # A family already published per format cannot be continued as one family,
    # or the other way round: `state["sets"]` and the frozen plan are keyed by
    # format, so the sets already created would be stranded -- invisible to the
    # resume logic and re-created under new names.
    started = {rec.get("fmt") for rec in state.get("sets", [])}
    if started and started != set(formats) and not started <= set(formats):
        want = "--mixed" if args.mixed else "per-format"
        log.error("this pack family was started as %s; %s cannot continue it. "
                  "Use a new --base, or finish it the way it was started.",
                  ", ".join(sorted(started)), want)
        return EXIT_USAGE
    with Catalog(db) as cat:
        # Databases written before publication records existed only knew "this
        # item was uploaded", not to which base. The first base to publish
        # claims that history so it is not re-uploaded.
        cat.adopt_legacy_publication(base)
        plan = freeze_plan(cat, data_dir, base, formats)

        if args.dry_run:
            print("DRY RUN: nothing uploaded.", flush=True)
            note = " (+1 brand logo each)" if logo_planned else ""
            skipped = set(state.get("skipped", []))
            for fmt in formats:
                # Count what will ACTUALLY publish, using the same filter the
                # publisher uses. Counting raw plan keys reported 200 emoji and
                # "2 sets" for a catalog with one item deselected, when the real
                # answer is 199 + logo = exactly one set -- and one-pack-or-two
                # is the whole question a dry run is asked.
                keys = pending_keys(cat, plan, fmt, base, skipped)
                n_sets = -(-len(keys) // capacity) if keys else 0
                print(f"  {fmt}: {len(keys)} emoji -> {n_sets} set(s) of up to "
                      f"{capacity}{note}, named "
                      f"{base}{FMT_TAG.get(fmt, '')}1_by_<bot> ...",
                      flush=True)
            return EXIT_OK

        token = os.environ.get(args.token_env, "")
        if not token:
            log.error("%s not set (env or .env).", args.token_env)
            return EXIT_USAGE
        if not args.user_id:
            log.error("provide --user-id or PACK_OWNER_USER_ID.")
            return EXIT_USAGE

        tg = Telegram(token)
        bot = tg.get_me()["username"]
        log.info("Publishing as @%s, owner=%s", bot, args.user_id)
        # Now that the real username is known, prove the names will fit before
        # the first upload freezes anything.
        for f in formats:
            check_name_length(base, bot, FMT_TAG.get(f, ""))

        if args.preflight:
            # After getMe (so the token is proven) but before the logo is
            # resolved or a single set is touched.
            return preflight(tg, cat, args.user_id, plan, formats, base,
                             set(state.get("skipped", [])))

        # The YourBrand logo is the mandatory first emoji of every set built by
        # the Emoji Mapper bot; the coin bot is excluded by design.
        logo = None
        if not args.no_brand_logo and bot.lower() in BRAND_LOGO_BOTS:
            if logo_planned:
                logo = BrandLogo(args.brand_logo, data_dir)
                log.info("brand logo enabled (first emoji of every set): %s",
                         args.brand_logo)
            else:
                log.warning("brand logo requested but not found: %s", args.brand_logo)

        ok = failed = 0
        for fmt in formats:
            done, bad = publish_format(
                tg, cat, fmt=fmt, plan_keys=plan.get(fmt, []),
                base=base, title=args.title, user_id=args.user_id,
                default_emoji=args.emoji, per_set=args.per_set,
                data_dir=data_dir, state=state, bot=bot, logo=logo,
                new_set=args.new_set)
            ok += done
            failed += bad
        save_json(_state_path(data_dir, base), state)

        # A run whose every upload failed used to print DONE and exit 0, so any
        # retry logic or menu action treated a dead run as a finished pack.
        print(f"\nDONE: {ok} uploaded, {failed} failed." if failed
              else "\nDONE.", flush=True)
        for s in state["sets"]:
            print(f"  https://t.me/addemoji/{s['name']}  [{s['fmt']}]", flush=True)
    return ingest_exit_code(ok, failed)


if __name__ == "__main__":
    raise SystemExit(record_exit_code(main()))
