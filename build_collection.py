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
import logging
import os
import re
import time
from pathlib import Path

from build_pack import (EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, ingest_exit_code, load_env, safe_int_env)
from announce import (announce_packs)
from packstate import (LockBusy, exclusive_lock)
from telegram_api import (AmbiguousUploadError, BotApiError, LiveStateUnknown, SetState, Telegram)
from emojikit import media
from emojikit.catalog import Catalog
from emojikit.logsetup import record_exit_code, redact, setup_logging
from collection_reconcile import (_confirm_new_upload,
                                  _file_is_permanently_rejected,
                                  _live_index, _manifest_mismatch, _probe,
                                  _set_is_open,
                                  reconcile_set)
from collection_state import (BRAND_LOGO_BOTS, BRAND_LOGO_DEFAULT,
                              BRAND_LOGO_EMOJI, BRAND_LOGO_KW,
                              DEFAULT_EMOJI, FMT_TAG, MIXED, PER_SET, ROOT,
                              BrandLogo, SetDrift, StateError, _lock_path, _state_path,
                              _static_is_blank, freeze_plan, load_state,
                              save_json)

log = logging.getLogger("build_collection")


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


def _video_is_blank(path: Path) -> bool:
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
        # media's named thresholds, not a third copy of 10/8: this is the
        # same "is it blank?" rule, applied to a raw frame instead of a
        # decoded image, and a literal drifting here would disagree with
        # every other producer about what ships.
        if sum(v > media.VISIBLE_ALPHA for v in alpha) > media.BLANK_MAX_VISIBLE:
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
                   new_set: bool = False,
                   into_pack: int | None = None) -> tuple[int, int]:
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
    #
    # Which set that is depends on --into-pack: reconciling the LAST one and
    # then filling a different one would refresh the wrong capacity.
    cur = fmt_sets[-1] if fmt_sets else None
    if into_pack is not None:
        cur = next((s for s in fmt_sets if s["index"] == into_pack), None)
        if cur is None:
            raise SetDrift(
                f"no {fmt} pack {into_pack} in this family; "
                f"have {sorted(s['index'] for s in fmt_sets) or 'none'}")
    if cur:
        reconcile_set(tg, cat, cur, data_dir, base)
        save_json(_state_path(data_dir, base), state)
    # A set holding a position we could not attribute is CLOSED: see
    # _set_is_open. Rolling to a fresh set is the only way to keep keys[] and
    # the live positions aligned once a foreign sticker sits between them.
    if cur and not _set_is_open(cur):
        # Asked for THIS pack by number, so silently filling a different one is
        # not a fallback, it is ignoring the instruction.
        if into_pack is not None:
            raise SetDrift(
                f"{cur['name']} holds live sticker(s) this publisher cannot "
                f"identify, so appending to it would hand a new key someone "
                f"else's custom_emoji_id. Refusing --into-pack {into_pack}.")
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
    if into_pack is not None and cur is not None and cur["live"] >= per_set:
        raise SetDrift(
            f"{cur['name']} is full ({cur['live']}/{per_set}); "
            f"--into-pack {into_pack} has nowhere to put anything.")
    # `target` is the record being FILLED. It is not always fmt_sets[-1] any
    # more: --into-pack can aim at a half-empty pack in the middle, and writing
    # the live count or the key order onto the last record instead would leave
    # the state describing a set the uploads never went to -- the drift the
    # whole reconcile path exists to prevent.
    target = None
    if cur and cur["live"] < per_set:
        set_index, set_name, in_set = cur["index"], cur["name"], cur["live"]
        target = cur
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
                target = fmt_sets[-1]
                state["sets"].append(target)
                save_json(_state_path(data_dir, base), state)
                log.info("[%s set %d] %s %s%s", fmt, set_index,
                         "adopted" if adopted else "created", set_name,
                         " (brand logo first)" if logo_png else "")
                if adopted:
                    # Attribute whatever the set already contains (the earlier
                    # create put SOMETHING there), then re-check this item.
                    in_set = reconcile_set(tg, cat, target, data_dir, base)
                    save_json(_state_path(data_dir, base), state)
                    if cat.is_published(base, key):
                        continue  # this very item was the set's first sticker
                    if not _set_is_open(target):
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
            elif target is not None:
                in_set = reconcile_set(tg, cat, target, data_dir, base)
                save_json(_state_path(data_dir, base), state)
                if not _set_is_open(target):
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
        target["live"] = in_set
        # Record actual upload order (for cid mapping) + mark uploaded (committed
        # immediately -> crash-safe duplicate guard). The cid comes from the
        # sticker just identified, so it is right even if the set drifts before
        # _record_cids reads it back.
        target.setdefault("keys", []).append(key)
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
                   target["title"])
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
    if target is not None and not incomplete:
        notify(tg, user_id, state, data_dir, base, target["name"], target["title"])
    elif target is not None:
        log.warning("[%s] not announcing %s: %d item(s) did not make it into "
                    "this run. The link is posted once a run completes cleanly.",
                    fmt, target["name"], incomplete)
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
    Telegram and refuses a perfectly legal name like GodVerify_Emoji_Packs.
    Consecutive underscores are not, and neither is a trailing one, because
    "<base>_" + "1_by_..." is fine but "<base>_" + "_by_..." is not, and the
    rule is easier to hold as "single underscores between parts".
    """
    if not _BASE_RE.fullmatch(base):
        raise SystemExit(
            "ERROR: --base must begin with a letter and contain only letters, "
            "digits and single underscores between them (Telegram's rule for a "
            "sticker-set name). Examples: 'mypack', 'GodVerify_Emoji_Packs'.")
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
    ap.add_argument("--into-pack", type=int, metavar="N",
                    help="Add to pack N instead of the newest one, so any pack "
                         "with room can be topped up. Fails loudly if pack N "
                         "does not exist, is full, or holds a sticker this "
                         "publisher cannot identify.")
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
    if args.new_set and args.into_pack is not None:
        log.error("--new-set opens a FRESH pack and --into-pack fills an "
                  "existing one; they cannot both be right. Pick one.")
        return EXIT_USAGE
    if args.into_pack is not None and args.into_pack < 1:
        log.error("--into-pack takes a pack number (1, 2, ...); got %d.",
                  args.into_pack)
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

        # The God Verify logo is the mandatory first emoji of every set built by
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
                new_set=args.new_set, into_pack=args.into_pack)
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
