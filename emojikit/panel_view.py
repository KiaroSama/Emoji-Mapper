"""The panel's view model: which cards the grid shows, and in what order.

Split out of `panel.py`, which serves them over HTTP. Two different jobs, and
that file had reached the size ceiling. Everything here is pure -- a catalog
goes in, a list of card dicts comes out; no sockets, no request state.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from emojikit import operator_config
from emojikit.catalog import PHASH_BITS, Catalog

# Deliberately the panel's logger, not this module's: these messages are the
# panel starting up, and a second logger name would split one run's output.
log = logging.getLogger("panel")

FMT_ORDER = {"static": 0, "video": 1, "animated": 2}
LOGO_KEY = "__brand_logo__"  # pseudo content_key: preview-only, never saved/counted


def order_by_similarity(items: list) -> list:
    """Greedy nearest-neighbour ordering by perceptual hash, grouped by format.

    Items without a perceptual hash (e.g. animated .tgs) keep content order and
    follow the hashed ones within their format group.
    """
    out: list = []
    for fmt in sorted({it.fmt for it in items}, key=lambda f: FMT_ORDER.get(f, 9)):
        group = [it for it in items if it.fmt == fmt]
        hashed = [it for it in group if it.phash is not None]
        plain = [it for it in group if it.phash is None]
        if hashed:
            # The walk stays quadratic on purpose: the greedy nearest-neighbour
            # chain IS the look-alike grouping the panel is for, and every index
            # that would make it sub-quadratic (LSH buckets, BK-tree pruning)
            # changes which near-twin ends up next to which. Only the constant
            # is negotiable, so the distance is inlined rather than called:
            # ``(a ^ b).bit_count()`` is identity.hamming's exact result, and at
            # n=3 600 dropping the per-pair call costs 0.48 s instead of 0.92 s
            # for a byte-identical order. (Against the older string-building
            # hamming it was 3.7 s.)
            # ponytail: O(n^2) scan; revisit only if a catalog grows past ~10k
            # items AND a different grouping is acceptable.
            remaining = hashed[:]
            ordered = [remaining.pop(0)]
            hashes = [it.phash for it in remaining]
            last = ordered[0].phash
            while remaining:
                best, best_d = 0, PHASH_BITS + 1
                for i, h in enumerate(hashes):
                    d = (h ^ last).bit_count()
                    if d < best_d:
                        best, best_d = i, d
                        if d == 0:
                            break   # nothing beats 0, and min() takes the first
                ordered.append(remaining.pop(best))
                last = hashes.pop(best)
            out.extend(ordered)
        out.extend(plain)
    return out


# The collector labels an ingested emoji "premium-id:<id>", and that id is the
# one thing anyone wants off this page. Decided here rather than by a regex
# inside the page's JavaScript, so it can actually be tested.
_PREMIUM_ID = re.compile(r"^premium-id:(\d+)$")


def copy_id_for(label: str) -> str:
    """The id a label offers for copying, or "" when it offers none.

    Anchored on purpose: "xpremium-id:12" and "premium-id:12x" are not ids, and
    a label that merely CONTAINS digits is not one either.
    """
    m = _PREMIUM_ID.match(label or "")
    return m.group(1) if m else ""


def packs_named(data_dir: Path, wanted: set[int]) -> dict[str, int]:
    """``{set name: pack index}`` for the given indices, across every family.

    Read from the publishers' own state files: the index is theirs, and
    deriving a name from the base plus a number would guess at a convention
    the state file already records exactly.

    The index travels WITH the name because the grid has to draw the boundary
    between two already-published packs, and their real sizes (95 and 96, say)
    have nothing to do with the per-set capacity the splits are otherwise
    computed from. A dict is still a container of names, so every membership
    test on it reads the same as before.
    """
    names: dict[str, int] = {}
    for state in sorted(data_dir.glob("publish_*.json")):
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.debug("could not read %s: %s", state.name, exc)
            continue
        for rec in data.get("sets") or []:
            if rec.get("index") in wanted and rec.get("name"):
                names[rec["name"]] = int(rec["index"])
    return names


def build_view(cat: Catalog, bot_username: str = "",
               show_published: bool = False,
               keep_sets: dict[str, int] | None = None, *,
               seed_order: bool = True) -> tuple[list[dict], dict, int]:
    """The cards to render, and where each one's file lives.

    An emoji already live in a pack is hidden by default: the grid is what the
    NEXT pack gets made of, and `is_published` skips those items at publish
    time however they are ticked here, so showing them only invites pruning
    work that changes nothing.

    This was "hide only a FULL set" for one round, on the theory that a set
    still being filled is still the pack being built. `--new-set` ended that --
    a pack can now be left half-empty on purpose, so "full" stopped meaning
    "finished" and the owner kept meeting an abandoned pack's emoji in the grid
    for the next one. Being published is the property that actually settles it,
    and it needs no state file and no capacity arithmetic.

    Hidden, never deleted -- those rows are what dedup recognises a re-download
    by, what maps a source premium id to ours, and what `sync_order` reads to
    re-sort a live set. ``show_published`` (``python -m emojikit.panel --all``) brings them
    back, which is how you reorder a pack that is already published.

    ``keep_sets`` (``python -m emojikit.panel --with-pack N``) is the narrow version of that:
    it un-hides ONE published set so its emoji can be arranged beside the new
    candidates going into it. `--all` is the wrong tool for that -- it also
    brings back every finished pack, which here is hundreds of cards you cannot
    act on.
    """
    # First time only: seed the manual order with the look-alike-grouped
    # similarity order (a nice starting point). After that, always use the saved
    # position order so the user's drag-drop arrangement is what shows/publishes.
    if seed_order and cat.get_meta("order_seeded") != "1":
        seeded = order_by_similarity(cat.all_items())
        cat.set_order([it.content_key for it in seeded])
        cat.set_meta("order_seeded", "1")
    items = cat.all_items()  # saved manual/seeded order (by position)
    hidden = 0
    pack_of: dict[str, int] = {}
    if not show_published:
        live = cat.published_keys()
        if keep_sets:
            # published_set_names() is the right lookup HERE and not in the
            # plain filter above: this asks "which set", where a row with no
            # recorded set name is genuinely unanswerable, so it stays hidden.
            where = cat.published_set_names()
            live = {k for k in live if where.get(k) not in keep_sets}
            pack_of = {k: keep_sets[n] for k, n in where.items() if n in keep_sets}
        keep = [it for it in items if it.content_key not in live]
        hidden = len(items) - len(keep)
        items = keep

    view = []
    by_key: dict[str, Path] = {}

    # Preview only, so unset configuration shows no logo instead of stopping;
    # the publish itself refuses to run without it.
    logo_path = operator_config.brand_logo_path(strict=False)
    branded = (bot_username.lower() in operator_config.brand_logo_bots(strict=False)
               and logo_path is not None and logo_path.is_file())
    if branded and not keep_sets:
        # Preview-only: shows where the brand logo will be inserted on publish.
        # It is NOT part of the catalog, is never counted in the totals, is not
        # clickable/toggleable, and is never sent to /api/save.
        view.append({
            "key": LOGO_KEY, "fmt": "static", "label": "Brand logo (auto-added on publish)",
            "emoji": "", "included": True, "isLogo": True,
        })
        by_key[LOGO_KEY] = logo_path

    for it in items:
        label = (it.keywords[0] if it.keywords else
                 (it.emojis[0] if it.emojis else it.content_key[2:10]))
        card = {
            "key": it.content_key,
            "fmt": it.fmt,
            "label": label,
            "copyId": copy_id_for(label),
            "emoji": it.emojis[0] if it.emojis else "",
            "included": it.included,
        }
        # Only for an emoji that is ALREADY live somewhere: the grid then draws
        # its boundaries from real membership instead of capacity arithmetic,
        # which cannot find the seam between two packs of unequal size.
        if (n := pack_of.get(it.content_key)) is not None:
            card["pack"] = n
        view.append(card)
        by_key[it.content_key] = Path(it.file_path)

    if branded and keep_sets:
        # Every one of these packs ALREADY carries the brand logo as its
        # emoji 0 -- it went up when the pack was created. The single card
        # above sits at the top of the GRID, so with more than one pack on
        # screen it lands on whichever is shown first and leaves the others
        # looking like they never got one; pack 5 was accused of exactly
        # that. One card at the head of each pack's run is what is live.
        out, seen = [], set()
        for card in view:
            n = card.get("pack")
            if n is not None and n not in seen:
                seen.add(n)
                key = f"__logo_pack_{n}__"
                out.append({"key": key, "fmt": "static",
                            "label": f"Brand logo (live in pack {n})",
                            "emoji": "", "included": True, "isLogo": True,
                            "pack": n})
                by_key[key] = logo_path
            out.append(card)
        view = out
    return view, by_key, hidden
