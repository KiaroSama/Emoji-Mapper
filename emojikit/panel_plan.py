"""The move plan the curate panel hands to whoever rearranges the real packs.

The panel is an INTENT editor, not a mirror of Telegram. The owner drags an
emoji into the pack they want it to end up in; nothing is moved on Telegram at
that moment, and nothing can be -- the Bot API has no move-between-sets call, so
a real move is a delete plus a re-add that mints a new ``custom_emoji_id``.

That gap is exactly why this file exists. Order alone cannot express the
intent: with fixed 200-emoji buckets a pack boundary does not follow from
position, so "which pack did the owner want this in" has to be recorded
explicitly or guessed later. It is written plainly, one line per emoji that
moves, so the person or script doing the real work reads a decision rather than
reverse-engineering one.

The panel's own model supplies the TARGET pack; the server's view supplies what
is LIVE. Neither side is asked to remember the other's half.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from emojikit.packstate import write_json_atomic

PLAN_NAME = "pack_plan.json"


def build_plan(view: list[dict], targets: dict[str, int], per_set: int) -> dict:
    """Diff the live membership in ``view`` against the panel's ``targets``.

    ``view`` entries carry ``pack`` only when the emoji is already live in one.
    ``targets`` is ``{key: intended pack}`` as the panel currently draws it.
    An emoji the panel has no opinion about is simply absent from the plan --
    saying nothing is different from saying "leave it", and only the first is
    honest.
    """
    moves, held, counts = [], [], {}
    for card in view:
        key = card.get("key")
        if not key or card.get("isLogo"):
            continue
        live = card.get("pack")
        want = targets.get(key)
        label = card.get("label") or key
        # Parked in the holding tray: the owner took it OUT of its pack and has
        # not said where it goes. That is a decision too, and the publish step
        # needs it -- it is what makes room for an arriving emoji.
        if not card.get("included"):
            if live is not None:
                held.append({"key": key, "label": label, "from_pack": live})
            continue
        if want is not None:
            counts[want] = counts.get(want, 0) + 1
        if live is not None and want is not None and want != live:
            moves.append({"key": key, "label": label,
                          "from_pack": live, "to_pack": want})
    # Reported, never enforced here: the panel refuses an over-full drop while
    # curating, but a plan read back later must still be able to say plainly
    # that a pack is over its cap rather than look fine and fail at publish.
    over = {str(pack): n for pack, n in sorted(counts.items()) if n > per_set}
    return {
        "version": 1,
        "written_utc": _dt.datetime.now(_dt.timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "per_set": per_set,
        "counts": {str(pack): n for pack, n in sorted(counts.items())},
        "over_capacity": over,
        "moves": moves,
        "held": held,
    }


def write_plan(data_dir: Path, plan: dict) -> Path:
    """Write the plan beside the catalog, atomically.

    Atomic because this file is read by a later step that may start at any
    moment: a half-written plan is a scrambled instruction set, and the project
    has already paid once for a half-written map.
    """
    path = Path(data_dir) / PLAN_NAME
    write_json_atomic(path, plan)
    return path


class PlanError(ValueError):
    """A saved intent is unreadable; never replace it with an empty plan."""


def read_plan(data_dir: Path) -> dict | None:
    import json
    from emojikit.state_artifacts import plan_keys

    path = Path(data_dir) / PLAN_NAME
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("unsupported plan path")
        doc = json.loads(path.read_text(encoding="utf-8"))
        plan_keys(doc)
        targets = target_map(doc)
        if any(not isinstance(n, int) or isinstance(n, bool) or n < 1
               for n in targets.values()):
            raise ValueError("pack numbers must be positive integers")
        return doc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Name the exact file. This refusal also blocks the page itself, so the
        # only way out is editing or moving that file -- a message that says
        # only "pack_plan.json" leaves the owner hunting for which data
        # directory the panel was started against.
        raise PlanError(f"cannot read {path}; preserve and repair it: {exc}") from exc


def target_map(plan: dict | None) -> dict[str, int]:
    """Read complete current intent, or explicit moves from a legacy plan."""
    if plan is None:
        return {}
    pairs = plan.get("targets")
    if pairs is None:
        pairs = [[row["key"], row["to_pack"]] for row in plan["moves"]]
    targets = dict(pairs)
    if len(targets) != len(pairs):
        raise PlanError("duplicate pack target keys")
    return targets


def overlay_targets(view: list[dict], plan: dict | None) -> list[dict]:
    """Render intent without changing the authoritative LIVE membership."""
    targets = target_map(plan)
    return [dict(card, pack=targets[card["key"]])
            if not card.get("isLogo") and card["key"] in targets else dict(card)
            for card in view]


def merge_plan(previous: dict | None, view: list[dict], targets: dict[str, int],
               scope: set[str], per_set: int, live: set[str] | None = None) -> dict:
    """Replace only this page's scope; preserve decisions it could not see.

    ``live`` is every key the catalog still holds. Without it the merge can only
    add: a page sees part of the catalog, so out-of-scope decisions are kept
    unconditionally and a key that has since been deleted is kept forever --
    counted in ``counts`` and able to report an ``over_capacity`` for a pack the
    owner cannot find. Passing the catalog's own key set is what lets a decision
    about an emoji that no longer exists be dropped rather than preserved; it
    must come from the database under the same lease that writes the plan, never
    from the render view, which hides published packs by design.
    """
    old = previous or {}
    scoped = [card for card in view if card.get("isLogo") or card["key"] in scope]
    plan = build_plan(scoped, targets, per_set)
    combined = {key: value for key, value in target_map(previous).items() if key not in scope}
    combined.update(targets)
    for field in ("moves", "held"):
        plan[field] = ([dict(row) for row in old.get(field, []) if row["key"] not in scope]
                       + plan[field])
    old_excluded = set(old.get("excluded", [])) | {row["key"] for row in old.get("held", [])}
    excluded = (old_excluded - scope) | {c["key"] for c in scoped
                                         if not c.get("isLogo") and not c["included"]}
    known = (set(old.get("known", [])) | set(combined) | old_excluded | scope)
    if live is not None:
        # Everything below is keyed by content key, so one filter covers the
        # whole plan. `scope` is already inside `live` -- the save handler
        # refuses otherwise -- so this only ever drops the stale remainder.
        combined = {key: number for key, number in combined.items() if key in live}
        known &= live
        excluded &= live
        for field in ("moves", "held"):
            plan[field] = [row for row in plan[field] if row["key"] in live]
    plan["targets"] = [[key, combined[key]] for key in sorted(combined)]
    plan["known"] = sorted(known)
    plan["excluded"] = sorted(excluded)
    counts: dict[int, int] = {}
    for key, number in combined.items():
        if key not in excluded:
            counts[number] = counts.get(number, 0) + 1
    # A brand logo consumes a slot in each pack, not once in the whole family.
    logos = dict(old.get("logo_slots", {}))
    for card in view:
        if card.get("isLogo"):
            packs = [card["pack"]] if card.get("pack") is not None else counts
            for number in packs:
                logos[str(number)] = 1
    if live is not None:
        # A pack nobody targets any more has no slot to reserve. Left in, its
        # stale entry still counts toward over_capacity for a pack that is gone.
        alive = {str(number) for number in counts} | {
            str(card["pack"]) for card in view
            if card.get("isLogo") and card.get("pack") is not None}
        logos = {number: slot for number, slot in logos.items() if number in alive}
    plan["logo_slots"] = logos
    plan["counts"] = {str(number): count for number, count in sorted(counts.items())}
    plan["over_capacity"] = {str(number): count + logos.get(str(number), 0)
                             for number, count in sorted(counts.items())
                             if count + logos.get(str(number), 0) > per_set}
    return plan
