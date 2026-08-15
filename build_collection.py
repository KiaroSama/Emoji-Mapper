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
import subprocess
import time
from pathlib import Path

from build_pack import (EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE,
                        AmbiguousUploadError, LiveStateUnknown, LockBusy,
                        SetState, Telegram, exclusive_lock, links_chat_id,
                        load_env, safe_int_env, write_json_atomic)
from emojikit import media
from emojikit.catalog import Catalog
from emojikit.logsetup import record_exit_code, redact, setup_logging
from PIL import Image

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("build_collection")

PER_SET = 200                       # Telegram custom-emoji set hard cap
FMT_TAG = {"static": "s", "video": "v", "animated": "a"}
FMT_WORD = {"static": "Static", "video": "Video", "animated": "Animated"}
DEFAULT_EMOJI = "\U0001F600"

# --- Brand logo (first emoji of every set built with the Emoji Mapper bot) --- #
# Only packs published by these bots get the mandatory YourBrand logo as their
# first emoji. The coin bot (@YourCoinEmojiBot) is intentionally
# excluded, so it is NOT in this set.
BRAND_LOGO_BOTS = {"youremojibot"}
# Ships with the repository. This used to be an absolute F:\ path, so on any
# other machine the "mandatory" logo silently vanished from every pack.
BRAND_LOGO_DEFAULT = str(ROOT / "assets" / "emoji-mapper-logo.png")
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


def _static_is_blank(path: Path, min_visible: int = 8) -> bool:
    """True if a static image is effectively empty (guards against blank emoji)."""
    try:
        im = Image.open(path).convert("RGBA")
    except Exception:  # noqa: BLE001 - non-static or unreadable: let upload decide
        return False
    alpha = im.split()[3]
    if alpha.getbbox() is None:
        return True
    return sum(1 for v in alpha.get_flattened_data() if v > 10) <= min_visible


class StateError(RuntimeError):
    """A publish plan/state file exists but cannot be used as it stands."""


class SetDrift(RuntimeError):
    """A live set no longer matches the manifest this publisher recorded."""


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
    """Resume state for ``base``, shape-checked.

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
    if not all(isinstance(s, dict) and isinstance(s.get("name"), str)
               and s.get("fmt") in FMT_TAG and isinstance(s.get("index"), int)
               for s in state["sets"]):
        raise StateError(f"{path}: every entry of 'sets' must record a name, a "
                         f"known format and an index.")
    return state


def freeze_plan(cat: Catalog, data_dir: Path, base: str, formats: list[str]) -> dict:
    """Build/extend the frozen, append-only upload plan from the catalog.

    Existing order is preserved; only newly-catalogued keys are appended, so the
    already-uploaded prefix of every format stays stable across runs.
    """
    plan = load_plan(data_dir, base)
    for fmt in formats:
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
    rows = cat.db.execute(
        "SELECT * FROM items WHERE format=? ORDER BY position, content_key", (fmt,)
    ).fetchall()
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


def _manifest_mismatch(cat: Catalog, live: list[dict], keys: list[str],
                       offset: int, name: str) -> str | None:
    """Why ``live`` no longer matches the recorded ``keys`` -- None if it does.

    Identity comes from ``file_unique_id``: every live sticker whose id we have
    already attributed must still sit on ITS key. A freshly uploaded copy has no
    known id yet (Telegram re-encodes the file, so neither its bytes nor its id
    can be predicted before reading the set back), so those positions are the
    only ones still bound by order -- and only inside a manifest whose length
    and known ids all check out.
    """
    if len(live) < offset + len(keys):
        return (f"{name} holds {len(live)} sticker(s) but this publisher "
                f"recorded {offset + len(keys)}: emoji were removed from the set.")
    for i, key in enumerate(keys):
        fuid = str(live[i + offset].get("file_unique_id") or "")
        known = cat.seen_file_unique_id(fuid) if fuid else None
        if known is not None and known != key:
            return (f"{name} position {i + offset} now holds {known}, but this "
                    f"publisher recorded {key} there: the set was reordered, "
                    f"replaced or edited by hand.")
    return None


def _resolve_sticker_key(tg, cat: Catalog, st: dict, tmp_dir: Path) -> str | None:
    """Map a LIVE sticker back to its catalog content_key.

    Fast path: its ``file_unique_id`` was recorded (ingest or a previous
    publish). Slow path: download the sticker and content-hash it. Returns
    None if it cannot be attributed to any catalog item."""
    fuid = str(st.get("file_unique_id") or "")
    if fuid:
        known = cat.seen_file_unique_id(fuid)
        if known and cat.get(known) is not None:
            return known
    file_id = st.get("file_id")
    if not file_id or not hasattr(tg, "download_file"):
        return None
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"reconcile_{fuid or 'unknown'}.dl"
    try:
        tg.download_file(file_id, tmp)
        key = media.content_key(tmp, media.telegram_sticker_format(st))
    except Exception as exc:  # noqa: BLE001 - unattributable, not fatal
        log.warning("reconcile download failed (%s): %s", fuid or file_id,
                    redact(str(exc)))
        return None
    finally:
        tmp.unlink(missing_ok=True)
    if cat.get(key) is None:
        return None
    if fuid:
        cat.record_file_unique_id(fuid, key)
    return key


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
    why = _manifest_mismatch(cat, live, keys, offset, s["name"])
    if why:
        raise SetDrift(f"{why} Refusing to publish into a set that no longer "
                       f"matches its manifest.")
    start = offset + len(keys)
    tail = live[start:]
    for i, st in enumerate(tail):
        key = _resolve_sticker_key(tg, cat, st, data_dir / "tmp")
        if key is None:
            # Attribution is positional (the cid mapping is), so it stops at the
            # first sticker we cannot recognize -- normally one the owner
            # appended. But if any of OUR emoji sit behind it, our positions
            # were shifted by a hand-INSERTED sticker, and stopping quietly
            # would leave the shifted item live yet unrecorded: still pending,
            # so uploaded a second time. Cheap id lookup only -- nothing past
            # the stop point is downloaded.
            behind = [x for x in tail[i + 1:]
                      if cat.seen_file_unique_id(str(x.get("file_unique_id") or ""))]
            if behind:
                raise SetDrift(
                    f"{s['name']} position {start + i} holds a sticker this "
                    f"publisher cannot recognize, yet {len(behind)} of its own "
                    f"emoji sit behind it: the set was edited by hand. Refusing "
                    f"to attribute by position.")
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


def notify(tg: Telegram, user_id: int, state: dict, data_dir: Path, base: str,
           name: str, title: str) -> None:
    if name in state["sent"]:
        return
    dest = links_chat_id(user_id)
    try:
        tg.send_message(dest, f"\u2705 {title}\nhttps://t.me/addemoji/{name}")
        state["sent"].append(name)
        save_json(_state_path(data_dir, base), state)
        log.info("sent link for %s to %s", name, dest)
    except Exception as exc:  # noqa: BLE001
        log.warning("notify failed for %s (destination %s): %s",
                    name, dest, redact(str(exc)))


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
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
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
    lines = [f"# {s.get('title', s['name'])}", "",
             f"Pack: https://t.me/addemoji/{s['name']}  |  format: {s['fmt']}  |  {len(keys)} emoji",
             "", "| # | Name | Emoji ID |", "|---|------|----------|"]
    for i, key in enumerate(keys, 1):
        it = cat.get(key)
        name = ", ".join(it.keywords[:2]) if it and it.keywords else (
            it.sources[0] if it and it.sources else key)
        cid = (cat.custom_emoji_id_for(base, key) if it else "") or ""
        lines.append(f"| {i} | {name} | {cid} |")
    (md / f"{s['name']}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("manifest written: manifests/%s.md (%d emoji)", s["name"], len(keys))


def publish_format(tg: Telegram, cat: Catalog, *, fmt: str, plan_keys: list[str],
                   base: str, title: str, user_id: int, default_emoji: str,
                   per_set: int, data_dir: Path, state: dict, bot: str,
                   logo: "BrandLogo | None" = None) -> None:
    """Publish all pending items of one format into per-format sets.

    Duplicate-proof: "already uploaded" is decided by the catalog's per-item
    (committed) ``uploaded`` flag, NOT by a positional offset, so skipped items
    can never shift the boundary and cause a re-upload on resume.
    """
    if not plan_keys:
        return
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
    if fmt_sets and fmt_sets[-1]["live"] < per_set:
        cur = fmt_sets[-1]
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

    n = 0
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
        try:
            placed = False
            if in_set != 0:
                try:
                    tg.add_emoji(user_id, set_name, path, fmt, emojis,
                                 item.keywords, expected_before=in_set)
                    placed = True
                except RuntimeError as exc:
                    if "STICKERS_TOO_MUCH" not in str(exc):
                        raise
                    in_set = 0
            if not placed:
                set_index += 1
                set_name = f"{base}{FMT_TAG[fmt]}{set_index}_by_{bot}"
                set_title = f"{title} {FMT_WORD[fmt]} {set_index}"
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
                        tg.create_emoji_set(user_id, set_name, set_title, path, fmt,
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
                    if in_set > (1 if logo_png else 0) + len(fmt_sets[-1]["keys"]):
                        log.warning("[%s] %s has unattributed live stickers; not "
                                    "adding %s yet (will retry)", fmt, set_name, key)
                        continue
                    tg.add_emoji(user_id, set_name, path, fmt, emojis,
                                 item.keywords, expected_before=in_set)
                elif logo_png:
                    # Set now exists with the logo at position 0; protect it from
                    # index rollback, then place this item as the second sticker.
                    in_set = 1
                    tg.add_emoji(user_id, set_name, path, fmt, emojis,
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
            continue
        except RuntimeError as exc:
            if not placed and in_set == 0:
                set_index -= 1
            # Transient/non-blank failure: log and retry on a later run (NOT
            # added to skipped), while the catalog flag keeps it dup-proof.
            log.warning("[%s] upload failed for %s (will retry): %s", fmt, key, redact(str(exc)))
            continue
        in_set += 1
        fmt_sets[-1]["live"] = in_set
        # Record actual upload order (for cid mapping) + mark uploaded (committed
        # immediately -> crash-safe duplicate guard).
        fmt_sets[-1].setdefault("keys", []).append(key)
        cat.mark_uploaded(key, None, base=base, set_name=set_name)
        n += 1
        if n % 20 == 0:
            save_json(_state_path(data_dir, base), state)
        if in_set >= per_set:
            notify(tg, user_id, state, data_dir, base, set_name,
                   f"{title} {FMT_WORD[fmt]} {set_index}")
            in_set = 0
        time.sleep(0.1)

    save_json(_state_path(data_dir, base), state)
    # Assign real custom_emoji_ids (drift-proof, from recorded order) + manifests.
    _record_cids(tg, cat, fmt_sets, base)
    for s in fmt_sets:
        write_manifest(data_dir, cat, s, base)

    if fmt_sets:
        last = fmt_sets[-1]
        notify(tg, user_id, state, data_dir, base, last["name"], last["title"])


def _record_cids(tg: Telegram, cat: Catalog, fmt_sets: list[dict],
                 base: str) -> None:
    """Store each item's real custom_emoji_id, verified against the live set.

    A bare positional mapping is wrong the moment the owner deletes, reorders or
    replaces a sticker: key[i] and live[i+offset] are then different emoji, and
    every id we publish (manifests, the bot, remaps) points at the wrong
    picture. The ids are taken only from a manifest that still matches by
    identity -- see :func:`_manifest_mismatch`.
    """
    for s in fmt_sets:
        keys = s.get("keys") or []
        if not keys:
            continue
        state, sset = _probe(tg, s["name"])
        if state is not SetState.EXISTS:
            log.warning("could not read %s for cid mapping (%s); ids unchanged",
                        s["name"], state.value)
            continue
        live = sset.get("stickers", [])
        offset = 1 if s.get("logo") else 0  # skip the brand logo at position 0
        why = _manifest_mismatch(cat, live, keys, offset, s["name"])
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


def valid_base(base: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", base):
        raise SystemExit("ERROR: --base must start with a letter and contain only "
                         "letters/digits (no underscores), e.g. 'mypack'.")
    return base


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
    ap.add_argument("--formats", default="static,video,animated",
                    help="Comma list of formats to publish, in order.")
    ap.add_argument("--per-set", type=int, default=PER_SET)
    ap.add_argument("--data-dir", default="collection")
    ap.add_argument("--brand-logo", default=BRAND_LOGO_DEFAULT,
                    help="Logo image used as the FIRST emoji of every set built "
                         "by the Emoji Mapper bot (ignored for the coin bot).")
    ap.add_argument("--no-brand-logo", action="store_true",
                    help="Disable the mandatory first-emoji brand logo.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    base = valid_base(args.base)
    try:
        formats = parse_formats(args.formats)
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
    with Catalog(db) as cat:
        # Databases written before publication records existed only knew "this
        # item was uploaded", not to which base. The first base to publish
        # claims that history so it is not re-uploaded.
        cat.adopt_legacy_publication(base)
        plan = freeze_plan(cat, data_dir, base, formats)

        if args.dry_run:
            print("DRY RUN: nothing uploaded.", flush=True)
            note = " (+1 brand logo each)" if logo_planned else ""
            for fmt in formats:
                keys = plan.get(fmt, [])
                n_sets = -(-len(keys) // capacity)
                print(f"  {fmt}: {len(keys)} emoji -> {n_sets} set(s) of up to "
                      f"{capacity}{note}, named {base}{FMT_TAG[fmt]}1_by_<bot> ...",
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

        state = load_state(data_dir, base)
        for fmt in formats:
            publish_format(tg, cat, fmt=fmt, plan_keys=plan.get(fmt, []),
                           base=base, title=args.title, user_id=args.user_id,
                           default_emoji=args.emoji, per_set=args.per_set,
                           data_dir=data_dir, state=state, bot=bot, logo=logo)
        save_json(_state_path(data_dir, base), state)

        print("\nDONE.", flush=True)
        for s in state["sets"]:
            print(f"  https://t.me/addemoji/{s['name']}  [{s['fmt']}]", flush=True)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(record_exit_code(main()))
