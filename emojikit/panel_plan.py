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
