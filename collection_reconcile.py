"""What is actually live in a set, and which of our keys each sticker is.

Every function here answers that question against Telegram rather than
against our own record, because the two disagreeing is the whole reason
this layer exists. ``reconcile_set`` is the entry point; the rest is how
a live sticker gets attributed -- by file_unique_id when we have recorded
one, by content otherwise, and by nothing at all when neither works, which
stays its own outcome rather than collapsing into a no.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from pathlib import Path


from telegram_api import (LiveStateUnknown, SetState)
from emojikit import identity, media
from emojikit.catalog import Catalog
from emojikit.logsetup import redact

from collection_state import (SetDrift, _state_path)

log = logging.getLogger("build_collection")


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


# The upload tolerance now lives with the comparison it belongs to, in
# identity.same_image: the Bot API client needs the identical rule when it checks
# whether its own add landed, and two copies of "is this the same picture"
# drifting apart is how one caller starts accusing a sticker the other accepts.
#
# The catalog-wide tolerance below is a DIFFERENT question and stays here.
#
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

    Fetching is this layer's job; deciding is ``identity.same_image``'s, which the
    Bot API client asks the same question of.
    """
    with _private_download(tmp_dir, "verify_") as tmp:
        try:
            tg.download_file(st["file_id"], tmp)
            return identity.same_image(tmp, source,
                                       media.telegram_sticker_format(st))
        except Exception as exc:  # noqa: BLE001 - a failed probe is not a "no"
            log.warning("upload verify failed for %s: %s", source.name,
                        redact(str(exc)))
            return None


@contextlib.contextmanager
def _private_download(tmp_dir: Path, prefix: str):
    """A scratch path we PROVABLY created, removed however the block ends.

    The names here used to be derived from the sticker's file_unique_id, which
    makes them predictable and shared. Three things follow from that: two runs
    verifying the same sticker fight over one file; a pre-existing file or
    symlink at that path is written THROUGH, which on a multi-user machine
    means writing wherever the link points; and the cleanup deletes whatever
    happens to be there, including something we never created.

    `mkstemp` answers all three: it creates the file itself with O_EXCL and
    owner-only permissions, so the name is unique and the file is ours. The
    caller overwrites it; the contextmanager removes it on every path out,
    including the ambiguous and error paths that used to leak it.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=tmp_dir, prefix=prefix, suffix=".dl")
    os.close(fd)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


# Three answers that are NOT a content key, kept apart on purpose. Collapsing
# any of them into "no match" is how a foreign sticker gets adopted, and
# collapsing any into a key is how it gets our item's identity.
AMBIGUOUS = "ambiguous"        # more than one catalog item is genuinely this
UNDECIDABLE = "undecidable"    # could not look; NOT evidence of anything


def _near_catalog_match(cat: Catalog, path: Path, fmt: str):
    """The one catalog item this image IS, within the re-encode tolerance.

    Returns a content_key, ``None`` for a proven miss (nothing in the catalog
    holds this picture), ``AMBIGUOUS`` when more than one item does, or
    ``UNDECIDABLE`` when the question could not be answered.

    The perceptual hash only NOMINATES candidates; it never decides. dHash is a
    GRAYSCALE structure hash, so an opaque red square and an opaque blue square
    are distance 0 from each other -- and this function used to accept the
    single closest candidate outright. With only the red one in the catalog, a
    live blue sticker resolved to the red item's key, and the caller then wrote
    that foreign sticker's file_unique_id and custom_emoji_id against our item
    and marked it published. The wrong mapping survived a reopen.

    `identity.same_image` is the check that would have caught it -- it demands
    structure AND colour agreement, and it is already what guards a fresh
    upload. The two paths asked different questions about the same thing; now
    they ask the same one, and only a VERIFIED candidate is returned.
    """
    probe = identity.perceptual_hash(path, fmt)
    if probe is None:
        # Animated is vector: there is no raster hash to search with and its
        # content key survives a re-gzip exactly, so a miss really is a miss.
        # For a raster format a missing hash means the decode failed, which is
        # "I could not look" -- a very different answer.
        return None if fmt == "animated" else UNDECIDABLE

    close = [it for it in cat.all_items()
             if it.fmt == fmt and it.phash is not None
             and identity.hamming(it.phash, probe) <= SEARCH_PHASH_TOLERANCE]
    if not close:
        return None

    verified: list[str] = []
    unknown = 0
    for it in close:
        src = Path(it.file_path)
        if not src.is_file():
            unknown += 1            # the item is ours, we just cannot compare
            continue
        same = identity.same_image(src, path, fmt)
        if same is True:
            verified.append(it.content_key)
        elif same is None:
            unknown += 1
    if len(verified) > 1:
        return AMBIGUOUS
    if verified:
        return verified[0]
    # Nothing verified. If any candidate could not be examined, the honest
    # answer is that we do not know -- not that this sticker is a stranger.
    return UNDECIDABLE if unknown else None


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
    # One scratch file, ours, removed on EVERY path out -- including the two
    # refusals below, which used to leak it, and an exception from the
    # near-match search, which used to leak it too.
    with _private_download(tmp_dir, "reconcile_") as tmp:
        try:
            tg.download_file(file_id, tmp)
            key = identity.content_key(tmp, media.telegram_sticker_format(st))
        except Exception as exc:
            log.warning("reconcile download failed (%s): %s", fuid or file_id,
                        redact(str(exc)))
            raise Unresolvable(
                f"could not fetch or hash {fuid or file_id}: {redact(str(exc))}"
            ) from exc
        if cat.get(key) is None:
            # Exact miss. Telegram re-encoded it, so for a raster format the
            # key cannot match -- fall back to the perceptual hash, but only
            # for a VERIFIED match. Guessing is what put a foreign llama on
            # `sol`; anything short of proof is Unresolvable, not a negative.
            near = _near_catalog_match(cat, tmp,
                                       media.telegram_sticker_format(st))
            if near == AMBIGUOUS:
                raise Unresolvable(
                    f"{fuid or file_id} is within the re-encode tolerance of "
                    f"more than one catalog item; refusing to attribute it by "
                    f"guess")
            if near == UNDECIDABLE:
                # A candidate existed but could not be compared -- a missing
                # file, a decoder that failed. Returning None here would call
                # this sticker foreign on the strength of a look we never got.
                raise Unresolvable(
                    f"{fuid or file_id} resembles a catalog item that could "
                    f"not be examined; refusing to decide either way")
            if near is None:
                return None
            key = near
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
