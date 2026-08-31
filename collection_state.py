"""What this publisher run intends to do, and what it has already done.

The frozen plan, the resume state and the files both live under
``--data-dir``. Split out of build_collection.py, which uploads; this
module only decides and records. It is the bottom layer: it imports
neither of the two above it.
"""

from __future__ import annotations

import json
import hashlib
import logging
from pathlib import Path

from PIL import Image

from build_pack import (write_json_atomic)
from emojikit import media
from emojikit.catalog import Catalog

log = logging.getLogger("build_collection")


ROOT = Path(__file__).resolve().parent

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
# Only packs published by these bots get the mandatory God Verify logo as their
# first emoji. The coin bot (@GodVerifyCoinEmojiMapperbot) is intentionally
# excluded, so it is NOT in this set.
BRAND_LOGO_BOTS = {"godverifyemojimapperbot"}
# Ships with the repository. This used to be an absolute F:\ path, so on any
# other machine the "mandatory" logo silently vanished from every pack.
BRAND_LOGO_DEFAULT = str(ROOT / "assets" / "god-verify-emoji-logo.png")
BRAND_LOGO_EMOJI = "\u2705"          # ✅ associated standard emoji for the logo
BRAND_LOGO_KW = ["godverify", "logo"]


class BrandLogo:
    """The brand logo (God Verify) used as the FIRST emoji of every set.

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
