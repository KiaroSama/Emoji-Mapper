"""The Telegram Bot API client, and the caps it has to respect.

Split out of build_pack.py, which decides WHAT to upload; this module only
talks to the API. It is a leaf: nothing here reaches back into the engine.

The non-idempotent calls (``createNewStickerSet``, ``addStickerToSet``) are
never blind-retried -- a retry that already landed creates a duplicate. They
go through the verified-retry path, which re-reads the live set and decides
from what is actually there.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time

import requests
from enum import Enum
from pathlib import Path

# Every wait and retry this client makes reaches the log, not just the console.
# It had no logger once: a publish that stalled 269 s on a flood wait left the
# log file silent for the whole pause, which reads exactly like a hung process,
# and every tool shares this client so they all went blind together. The name
# stays "build_pack" so existing log setups keep collecting these lines.
log = logging.getLogger("build_pack")


# Custom emoji must be 100x100 PNG; build_pack uploads the prepared PNGs.
_MIME = {".png": "image/png", ".webp": "image/webp"}
DEFAULT_EMOJI = "\U0001FA99"  # ߞ coin
# Telegram's hard cap for a custom-emoji set. See
# https://core.telegram.org/bots/api#addstickertoset -- "Emoji sticker sets can
# have up to 200 stickers." Exceeding it only produces STICKERS_TOO_MUCH at
# upload time, after the wrong set count has already been planned.
MAX_PER_SET = 200
PER_SET = MAX_PER_SET
# Upper bound on how long a create may wait for a deleted set name to be
# released, across all of its retries.
NAME_LOCK_TIMEOUT = 300.0


def api_base() -> str:
    """Resolve the API base per call, not at import.

    ``load_env()`` runs inside main(), which is *after* this module is imported,
    so reading the env var at import time silently ignores a TELEGRAM_API_BASE
    that is configured only in .env.
    """
    return os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")


class SetState(Enum):
    """Three distinct answers to "does this sticker set exist?".

    Collapsing UNKNOWN into MISSING is how a network blip becomes "the set is
    empty", which then justifies re-uploading or re-creating it.
    """

    EXISTS = "exists"
    MISSING = "missing"
    UNKNOWN = "unknown"


class LiveStateUnknown(RuntimeError):
    """Live Telegram state could not be determined; callers must not guess."""


def _usable_fuids(stickers: list) -> set[str] | None:
    """Distinct file_unique_ids of ``stickers``, or None if they cannot identify.

    Identity only works when EVERY sticker carries one and they are distinct.
    A missing id would collapse several stickers onto the same key, making a
    set look unchanged when it is not.
    """
    fuids = set()
    for s in stickers:
        fuid = s.get("file_unique_id")
        if not fuid:
            return None
        fuids.add(str(fuid))
    return fuids if len(fuids) == len(stickers) else None


class BotApiError(RuntimeError):
    """Telegram answered ok:false -- a definite rejection of THIS request.

    Distinct from a RuntimeError raised after the retries ran out, which means
    we never got an answer at all. That difference decides whether it is safe
    to send a replacement request: a rejection definitely did not apply, an
    unanswered request may have.
    """


class AmbiguousUploadError(RuntimeError):
    """A state-changing call failed at the network level and the live state
    could not be verified: the change may or may not have been applied.
    Callers must reconcile against live Telegram state instead of re-sending
    (re-sending a non-idempotent call such as addStickerToSet would DUPLICATE
    its effect)."""


class Telegram:
    def __init__(self, token: str) -> None:
        self.token = token
        self.s = requests.Session()

    def _safe(self, exc: BaseException) -> str:
        """Exception text with the bot token stripped.

        Every request URL embeds the token, and requests puts the URL in its
        exception message -- printing ``str(exc)`` raw publishes the token to
        the console and, via the coin runner's output redirect, to a log file.
        """
        return str(exc).replace(self.token, "[REDACTED]")

    def _call(self, method: str, *, data=None, files=None, retries: int = 5,
              applied_check=None):
        """POST a Bot API method with retries.

        ``applied_check`` makes retries safe for NON-idempotent methods
        (addStickerToSet / createNewStickerSet). After a network-level failure
        (the request may have been processed even though the response never
        arrived) it probes the live state and returns:
          True  -> the change IS live: report success, never re-send;
          False -> definitely not applied: safe to re-send;
          None  -> live state unknown: raise AmbiguousUploadError so the
                   caller reconciles instead of guessing.
        Without a check, such methods keep the historical blind-retry behavior.
        """
        url = f"{api_base()}/bot{self.token}/{method}"
        # A just-deleted set name stays locked for ~2 min, so a CREATE may
        # legitimately need to wait it out. For every other method
        # STICKERSET_INVALID means "no such set" -- a permanent answer that must
        # be returned at once, not slept on for six minutes.
        name_lock_retry = method == "createNewStickerSet"
        deadline = time.monotonic() + NAME_LOCK_TIMEOUT
        for attempt in range(1, retries + 1):
            try:
                r = self.s.post(url, data=data, files=files, timeout=60)
                try:
                    payload = r.json()
                except ValueError as exc:
                    # A proxy or gateway can answer with an HTML error page.
                    # That is a transport failure, not a Bot API reply, so it
                    # must go through the same retry/applied_check path as any
                    # other network error instead of escaping raw.
                    raise requests.exceptions.InvalidJSONError(
                        f"non-JSON response (HTTP {r.status_code})") from exc
                if payload.get("ok"):
                    return payload["result"]
                desc = str(payload.get("description", ""))
                # Honor flood waits.
                if "retry after" in desc.lower():
                    wait = int(payload.get("parameters", {}).get("retry_after", 5))
                    print(f"  flood wait {wait}s ({method})", flush=True)
                    log.warning("flood wait %ss (%s)", wait, method)
                    time.sleep(wait + 1)
                    continue
                if "stickerset_invalid" in desc.lower():
                    remaining = deadline - time.monotonic()
                    if not name_lock_retry or attempt >= retries or remaining <= 0:
                        raise BotApiError(f"{method} failed: {desc}")
                    wait = min(30 * attempt, 90, remaining)
                    print(f"  stickerset_invalid; name not released yet, "
                          f"wait {wait:.0f}s ({method})", flush=True)
                    log.warning("stickerset_invalid; name not released yet, "
                                "wait %.0fs (%s)", wait, method)
                    time.sleep(wait)
                    continue
                raise BotApiError(f"{method} failed: {desc}")
            except requests.RequestException as exc:
                if applied_check is not None:
                    time.sleep(2)  # let Telegram settle before probing
                    applied = applied_check()
                    if applied is True:
                        print(f"  {method}: network error but the change is "
                              f"verified live; not re-sending", flush=True)
                        log.warning("%s: network error but the change is "
                                    "verified live; not re-sending", method)
                        return {"verified_applied": True}
                    if applied is None:
                        raise AmbiguousUploadError(
                            f"{method}: network failure and live state "
                            f"unknown ({self._safe(exc)})") from exc
                    # applied is False: definitely not applied, safe to re-send.
                if attempt >= retries:
                    break          # never sleep after the final attempt
                wait = min(3 * attempt, 20)
                print(f"  net retry {attempt}/{retries} ({method}): "
                      f"{self._safe(exc)} (wait {wait}s)", flush=True)
                log.warning("net retry %d/%d (%s): %s (wait %ss)",
                            attempt, retries, method, self._safe(exc), wait)
                time.sleep(wait)
        raise RuntimeError(f"{method} failed after {retries} attempts")

    def probe_sticker_set(self, name: str) -> tuple[bool, dict | None]:
        """Single, non-retrying live probe of a sticker set.

        Returns ``(known, set)``: ``(True, dict)`` it exists, ``(True, None)``
        it definitely does not exist, ``(False, None)`` live state unknown
        (network/API failure). Unlike getStickerSet via ``_call`` this never
        raises and never sleeps (``_call`` treats STICKERSET_INVALID as a
        name-release lock and waits minutes, which a probe must not do).
        """
        try:
            r = self.s.post(f"{api_base()}/bot{self.token}/getStickerSet",
                            data={"name": name}, timeout=30)
            payload = r.json()
        except (requests.RequestException, ValueError):
            return False, None
        if payload.get("ok"):
            return True, payload["result"]
        if "stickerset_invalid" in str(payload.get("description", "")).lower():
            return True, None
        return False, None

    def probe_set_state(self, name: str) -> tuple[SetState, dict | None]:
        """Tri-state probe: EXISTS / MISSING / UNKNOWN plus the set when known."""
        known, sset = self.probe_sticker_set(name)
        if not known:
            return SetState.UNKNOWN, None
        return (SetState.EXISTS, sset) if sset is not None else (SetState.MISSING, None)

    def live_count_strict(self, name: str) -> int:
        """Live sticker count, or raise LiveStateUnknown.

        Never returns 0 for "could not tell": a caller that rolls back or
        retries a mutation on that 0 will re-send an upload that already
        landed.
        """
        state, sset = self.probe_set_state(name)
        if state is SetState.EXISTS:
            return len(sset.get("stickers", []))
        if state is SetState.MISSING:
            return 0
        raise LiveStateUnknown(f"live state of {name} is unknown")

    def _added_check(self, name: str, expected_before: int | None, *,
                     known_before: set[str] | None = None,
                     source: Path | None = None):
        """applied_check for addStickerToSet: did OUR sticker land?

        A count is not identity. "the set grew by one" is equally true when a
        second writer added something entirely different while our request
        failed -- and acting on that marks the wrong item done, which is how a
        ticker ends up pointing at another coin's artwork.

        When the caller captures the set's file_unique_ids beforehand
        (``known_before``) the check becomes identity-based: exactly one NEW
        identity appeared, so something really was added and we can attribute
        it. Two or more new identities means a concurrent writer was involved
        and attribution is unsafe -> UNKNOWN. Callers that cannot supply the
        snapshot fall back to the count, which is weaker and documented as such.
        """
        if expected_before is None:
            return None

        def check():
            # Remember that a live probe was needed. A failed attempt can let a
            # FOREIGN sticker in before our retry lands, so the set grows by two
            # while the caller counts one; ``_live_after_add`` re-reads the size
            # whenever this ran instead of assuming an increment.
            check.fired = True
            known, sset = self.probe_sticker_set(name)
            if not known or sset is None:
                return None  # unknown / set vanished: reconcile, don't guess
            stickers = sset.get("stickers", [])
            now = _usable_fuids(stickers)
            if known_before is None or now is None:
                # Without usable identities nothing here can be proved. A count
                # of expected+1 is NOT evidence -- it is equally produced by
                # someone else's sticker landing while ours failed -- and
                # answering False would re-send an upload that may have landed.
                return None
            new = now - known_before
            if not new:
                return False         # definitely nothing was added
            if len(new) > 1:
                return None          # someone else wrote too: unattributable
            if source is None:
                return None          # cannot prove the newcomer is ours
            # Exactly one new sticker. That is still not proof it is OURS: our
            # request may have failed while an external or manual add landed.
            # Compare its content with the image we sent.
            added = next((s for s in stickers
                          if str(s.get("file_unique_id")) in new), None)
            if added is None:
                return None
            same = self._sticker_matches(added, source)
            if same is True:
                return True
            if same is False:
                return False         # a foreign sticker landed; ours did not
            return None              # could not verify: reconcile, don't guess

        check.fired = False
        # Exposed so the post-add size read can require the set to actually
        # contain OUR sticker, rather than trusting a bare len(). Both halves
        # are needed there: the snapshot says which identities are new, the
        # source says which of them we sent.
        check.known_before = known_before
        check.source = source
        return check

    def _live_after_add(self, name: str, check) -> int | None:
        """The set's live size after an add, or None when +1 is sound.

        Only a RETRIED add can move the size by anything but one: the attempt
        that failed may have let a foreign sticker in (``_added_check`` answers
        False for exactly that, which re-sends ours), leaving the set two bigger
        while the caller counts one. Every later ``expected_before`` is derived
        from that number, so it has to describe the set rather than our own
        intentions. Probing only when the check actually ran keeps the cost at
        one extra round trip per network failure instead of one per sticker.
        """
        if check is None or not check.fired:
            return None
        known, sset = self.probe_sticker_set(name)
        # A bare len() is not a measurement of the set we just wrote to. Two
        # answers look like a number and are not one:
        #   * a read that has not caught up with our own acknowledged write
        #     reports one too few -- this client already assumes that lag
        #     elsewhere (it sleeps before probing), and booking the short count
        #     puts every later expected_before permanently out by one;
        #   * a MISSING set reports ZERO, which the caller reads as an empty set
        #     and answers by creating a second pack, sending its link, exiting 0.
        # Both are excluded by requiring the read to actually CONTAIN the
        # sticker we just added. Anything else is "no answer", which is what
        # AmbiguousUploadError already means here.
        if not known or sset is None:
            raise AmbiguousUploadError(
                f"addStickerToSet applied after a retry but {name} could not be "
                f"read back afterwards (unknown live state, or the set is gone)")
        stickers = sset.get("stickers", [])
        now = _usable_fuids(stickers)
        if now is not None and check.known_before is not None:
            new = now - check.known_before
            # "Something new is here" is not "ours is here", and a FOREIGN
            # sticker landing during the failed attempt is the exact case this
            # whole path exists for -- so a bare difference passed the guard in
            # precisely the situation it was written to catch, and the size read
            # off a set that may not hold our sticker at all was booked as the
            # result of our upload. Only the content answers it. This costs
            # nothing on the happy path: it runs only when a live probe was
            # already needed, and stops at the first match.
            if not any(self._sticker_matches(s, check.source) is True
                       for s in stickers
                       if str(s.get("file_unique_id")) in new):
                raise AmbiguousUploadError(
                    f"addStickerToSet reported success for {name} but no "
                    f"sticker in the set read back afterwards holds the image "
                    f"we sent; its size cannot be trusted")
        return len(stickers)

    def set_fuids(self, name: str) -> set[str] | None:
        """Usable identities of a set's stickers, or None when there are none.

        None means "identity is not available here" -- the caller must fall
        back to the weaker count check rather than conclude anything.
        """
        state, sset = self.probe_set_state(name)
        if state is not SetState.EXISTS:
            return None
        return _usable_fuids(sset.get("stickers", []))

    def _created_check(self, name: str, *, expect_first: Path | None = None):
        """applied_check for createNewStickerSet: did WE create this set?

        Mere existence is not proof. A set with the same name may already
        belong to someone else, or be left over from an earlier run, and
        adopting it after a transport failure silently attaches our state to a
        pack we did not build. When ``expect_first`` names the image we were
        creating the set with, the check also requires the live set to hold
        exactly one sticker whose content matches it.
        """
        def check():
            known, sset = self.probe_sticker_set(name)
            if not known:
                return None
            if sset is None:
                return False
            if expect_first is None:
                return True
            stickers = sset.get("stickers", [])
            if len(stickers) != 1:
                return None       # not the shape our create would have left
            return self._sticker_matches(stickers[0], expect_first)

        return check

    def _sticker_matches(self, sticker: dict, source: Path) -> bool | None:
        """Does a live sticker hold the image in ``source``?

        Telegram re-encodes on upload, so bytes never match. Comparing the
        project's content key was the first attempt and it is not enough
        either: that key is a SHA of exact pixels, so a LOSSY re-encode changes
        it for a picture that is visually identical. Every answer this gave for
        our own uploads was therefore "different", and because the caller reads
        False as "a foreign sticker landed", a 449-emoji publish stopped dead on
        a sticker that had landed perfectly well.

        ``identity.same_image`` owns the decision now: exact key first, then a
        measured tolerance for static, and None for anything it has no measured
        tolerance for. Returns None when the comparison could not be made OR
        could not be trusted, so the caller reconciles instead of accusing.
        """
        try:
            from emojikit import identity, media
        except Exception:         # noqa: BLE001 - media stack unavailable
            return None
        # A PRIVATE directory, not a name derived from the sticker id in the
        # shared system temp. That name was predictable and reused, so two runs
        # verifying the same sticker overwrote each other's download; a file or
        # symlink already sitting there was written THROUGH, which on a
        # multi-user machine means writing wherever the link points; and the
        # cleanup then deleted whatever was there, ours or not. A fresh
        # directory is unique, owner-only, and takes its contents with it.
        try:
            fmt = media.telegram_sticker_format(sticker)
            with tempfile.TemporaryDirectory(prefix="emoji-verify-") as box:
                tmp = Path(box) / "sticker.dl"
                self.download_file(sticker["file_id"], tmp)
                return identity.same_image(tmp, source, fmt)
        except Exception:         # noqa: BLE001 - a failed probe is not a "no"
            return None

    def get_me(self) -> dict:
        return self._call("getMe")

    def send_message(self, chat_id: int | str, text: str, *,
                     disable_preview: bool = False) -> None:
        """Send a notification.

        ``disable_preview`` matters for a message that is mostly links: the coin
        family posts 30+ addemoji URLs and a preview card per link buries them.
        It used to be a private ``_call`` in the coin script for exactly that;
        it lives here so every announcer can ask for it.

        sendMessage is not idempotent and the Bot API offers no dedup key, so a
        network failure AFTER Telegram accepted the message cannot be told from
        one before -- a retry may post the link twice. Callers guard against
        repeats across runs (``state["sent"]``); within a run the exposure is
        bounded by retrying only once instead of the default five times, which
        keeps a genuine transient blip recoverable without turning one outage
        into five identical posts.
        """
        self._call("sendMessage", retries=2, data={
            "chat_id": chat_id, "text": text,
            "disable_web_page_preview": disable_preview,
        })

    # NOTE: all upload methods pass the file CONTENT (bytes), not an open
    # handle: a retried request must re-send the full body, and a file object
    # is already exhausted after the first attempt (a flood-wait or network
    # retry would silently send an empty file and fail the sticker).

    def upload_sticker(self, user_id: int, path: Path) -> str:
        mime = _MIME.get(path.suffix.lower(), "application/octet-stream")
        res = self._call(
            "uploadStickerFile",
            data={"user_id": user_id, "sticker_format": "static"},
            files={"sticker": (path.name, path.read_bytes(), mime)},
        )
        return res["file_id"]

    def check_uploadable(self, user_id: int, path: Path, fmt: str) -> None:
        """Ask Telegram to accept a sticker FILE, touching no set.

        ``uploadStickerFile`` runs the same validator as ``addStickerToSet``, so
        this answers "will this file be refused?" in one call and without
        mutating a pack. Raises ``BotApiError`` when Telegram refuses it.

        Worth its own method because the refusal it catches is not one any local
        check can predict: Telegram's uploader is stricter than its player, and
        a `.tgs` it happily renders in a published pack can still be rejected on
        the way in. One such file cost a 46-minute publish.
        """
        self._call("uploadStickerFile",
                   data={"user_id": user_id, "sticker_format": fmt},
                   files={"sticker": (path.name, path.read_bytes(),
                                      _mime_for_path(path))})

    def create_set(self, user_id: int, name: str, title: str, png: Path,
                   emoji: str, keywords: str) -> None:
        # Upload the image inline via attach:// (1 request instead of 2).
        self._call("createNewStickerSet", data={
            "user_id": user_id, "name": name, "title": title,
            "sticker_type": "custom_emoji",
            "stickers": json.dumps([_sticker_json(emoji, keywords)]),
        }, files={"file0": (png.name, png.read_bytes(), "image/png")},
            applied_check=self._created_check(name, expect_first=png))

    def add_sticker(self, user_id: int, name: str, png: Path,
                    emoji: str, keywords: str, *,
                    expected_before: int | None = None) -> int | None:
        """Add a static sticker. Pass ``expected_before`` (the live sticker
        count the caller expects BEFORE this add) to make network retries
        duplicate-proof; without it the historical blind retry is kept.

        The set's identities are snapshotted first so the applied-check can ask
        "did exactly one NEW sticker appear?" rather than "is the count one
        higher?", which a concurrent writer can satisfy while our own request
        failed.

        Returns the live sticker count when the add had to be retried, else
        None -- see ``_live_after_add``, which explains why the caller may not
        simply add one in that case."""
        before = self.set_fuids(name) if expected_before is not None else None
        check = self._added_check(name, expected_before,
                                  known_before=before, source=png)
        self._call("addStickerToSet", data={
            "user_id": user_id, "name": name,
            "sticker": json.dumps(_sticker_json(emoji, keywords)),
        }, files={"file0": (png.name, png.read_bytes(), "image/png")},
            applied_check=check)
        return self._live_after_add(name, check)

    # ----- multi-format helpers (static / animated / video) -------------- #
    def set_sticker_position(self, file_id: str, position: int) -> None:
        """Move an existing sticker to ``position`` (zero-based) in its set.

        The one mutation here that IS idempotent: setting the same sticker to
        the same index twice leaves the same set. So unlike addStickerToSet it
        can be retried normally -- no verified-retry machinery, no ambiguity to
        reconcile.

        Nothing is re-uploaded and nothing is recreated, so the sticker keeps
        its file_id AND its custom_emoji_id: anyone already using the emoji is
        unaffected by a reorder.
        """
        self._call("setStickerPositionInSet",
                   data={"sticker": file_id, "position": int(position)})

    def get_sticker_set(self, name: str) -> dict:
        """Return the full Bot API StickerSet object for a set short name."""
        return self._call("getStickerSet", data={"name": name})

    def get_custom_emoji_stickers(self, custom_emoji_ids: list[str]) -> list[dict]:
        """Resolve custom-emoji IDs to their Sticker objects (max 200 per call).

        Any bot can resolve arbitrary ``custom_emoji_id`` values; the Bot API
        silently drops IDs it cannot find, so the returned list may be shorter
        than the input. Each returned Sticker carries ``custom_emoji_id``,
        ``file_id`` and ``file_unique_id`` plus the animated/video flags.
        """
        if not custom_emoji_ids:
            return []
        if len(custom_emoji_ids) > 200:
            raise ValueError("getCustomEmojiStickers accepts at most 200 IDs per call")
        return self._call("getCustomEmojiStickers",
                          data={"custom_emoji_ids": json.dumps(custom_emoji_ids)})

    def download_file(self, file_id: str, dest: Path, retries: int = 5) -> Path:
        """Download a Telegram file (by file_id) to ``dest`` (with retries)."""
        info = self._call("getFile", data={"file_id": file_id})
        url = f"{api_base()}/file/bot{self.token}/{info['file_path']}"
        for attempt in range(1, retries + 1):
            try:
                r = self.s.get(url, timeout=60)
                r.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.content)
                return dest
            except requests.RequestException as exc:
                if attempt >= retries:
                    break
                wait = min(3 * attempt, 15)
                print(f"  download retry {attempt}/{retries}: "
                      f"{self._safe(exc)} (wait {wait}s)", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"download failed for file_id {file_id}")

    def create_emoji_set(self, user_id: int, name: str, title: str, path: Path,
                         fmt: str, emoji_list: list[str], keywords: list[str],
                         *, needs_repainting: bool = False) -> None:
        """Create a custom-emoji set whose first emoji is ``path`` (any format).

        ``needs_repainting`` asks the CLIENT to paint every emoji in the set
        with the text/accent colour, which is how Telegram's own monochrome
        marks (TopicIcons and friends) look right anywhere. It is settable ONLY
        here: the Bot API exposes the field on `Sticker` and on this method and
        nowhere else, so a set created without it can never gain it. It is also
        a WHOLE-SET property -- switching it on flattens every full-colour emoji
        in the same pack -- which is why repaintable art needs its own set.
        """
        data = {
            "user_id": user_id, "name": name, "title": title,
            "sticker_type": "custom_emoji",
            "stickers": json.dumps([_input_sticker(fmt, emoji_list, keywords)]),
        }
        if needs_repainting:
            data["needs_repainting"] = "true"
        self._call("createNewStickerSet", data=data,
                   files={"file0": (path.name, path.read_bytes(),
                                    _mime_for_path(path))},
                   applied_check=self._created_check(name, expect_first=path))

    def add_emoji(self, user_id: int, name: str, path: Path, fmt: str,
                  emoji_list: list[str], keywords: list[str], *,
                  expected_before: int | None = None) -> int | None:
        """Add one emoji (any format) to an existing custom-emoji set.

        ``expected_before`` (the live sticker count expected BEFORE this add)
        makes network retries duplicate-proof, and the return value reports the
        live size after a retry; see ``add_sticker``."""
        before = self.set_fuids(name) if expected_before is not None else None
        check = self._added_check(name, expected_before,
                                  known_before=before, source=path)
        self._call("addStickerToSet", data={
            "user_id": user_id, "name": name,
            "sticker": json.dumps(_input_sticker(fmt, emoji_list, keywords)),
        }, files={"file0": (path.name, path.read_bytes(), _mime_for_path(path))},
            applied_check=check)
        return self._live_after_add(name, check)


_MIME_BY_EXT = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tgs": "application/gzip",
    ".webm": "video/webm",
}


def _mime_for_path(path: Path) -> str:
    """MIME type derived from the file's real extension (preferred for uploads)."""
    return _MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")


def _trim_keywords(keywords: list[str]) -> list[str]:
    """Clamp a keyword list to Telegram's per-sticker budget (<=20, ~64 chars)."""
    kw: list[str] = []
    total = 0
    for raw in keywords:
        k = (raw or "").strip()[:48]
        if not k:
            continue
        if kw and total + len(k) + 1 > 60:
            break
        kw.append(k)
        total += len(k) + 1
        if len(kw) >= 20:
            break
    return kw


def _input_sticker(fmt: str, emoji_list: list[str], keywords: list[str]) -> dict:
    """Build a Bot API InputSticker for any custom-emoji format (uploaded as file0)."""
    emojis = [e for e in (emoji_list or []) if e][:20] or [DEFAULT_EMOJI]
    return {"sticker": "attach://file0", "format": fmt,
            "emoji_list": emojis, "keywords": _trim_keywords(keywords)}


def _sticker_json(emoji: str, keywords: str) -> dict:
    """Static InputSticker from a comma-separated keyword string."""
    return {"sticker": "attach://file0", "format": "static",
            "emoji_list": [emoji],
            "keywords": _trim_keywords(keywords.split(","))}
