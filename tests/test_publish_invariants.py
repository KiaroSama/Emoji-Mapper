"""Two invariants the publisher states but only checked in one place each.

Both came out of the audit's adjacent-path list rather than its numbered
findings, and both were confirmed by driving the real `publish_format`.

**A closed set stayed the target.** When adoption found live stickers this
publisher cannot attribute, it logged, counted a failure and moved on -- but
left `in_set` at the reconciled count. The NEXT pending item therefore took the
ordinary add branch and appended to the very set just judged unsafe. One item
was protected; every item after it was not. `_set_is_open` is now asked before
every add.

**`--mixed` switched off the blank-media guard.** `_media_ok(path, fmt)` was
handed the FAMILY format. Under `--mixed` that is "mixed", which matches
neither the static branch nor the video branch, so the function returned True
for everything -- in exactly the layout this project publishes with.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tests._bc_fixtures import ROOT, FakeTG, _make_png, _sticker

sys.path.insert(0, str(ROOT))

import build_collection as bc
from collection_state import MIXED, SetDrift
from emojikit import identity
from emojikit.catalog import Catalog

SET = "pks1_by_YourEmojiBot"


class AClosedSetIsNeverAppendedTo(unittest.TestCase):
    """Drive TWO pending items through a set holding a foreign sticker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, n=2):
        """n items, each carrying the source file_unique_id reconcile uses."""
        keys = []
        with Catalog(self.data / "catalog.db") as cat:
            for i in range(n):
                p = self.data / "media" / "static" / f"item{i}.png"
                _make_png(p, color=(10, (40 * (i + 1)) % 255, 200, 255))
                key = identity.content_key(p, "static")
                cat.add(content_key=key, fmt="static", file_path=p,
                        emojis=["\U0001f600"], keywords=[f"item{i}"],
                        file_unique_id=f"SRC-item{i}")
                keys.append(key)
        return keys

    def closed_live_set(self):
        """Our own sticker at position 0, a stranger's at position 1.

        The order matters. A foreign sticker BEFORE our attributed prefix is a
        manifest mismatch and `reconcile_set` refuses the whole set for that
        reason alone. The case this test is about is the other one: the
        manifest still matches as far as it goes, and the extra live position
        simply belongs to nobody -- which is what `_set_is_open` is for.
        """
        return {SET: [_sticker("SRC-item0", "cid-ours"),
                      _sticker("FOREIGN-1", "cid-foreign")]}

    def closed_state(self):
        return {"base": "pk", "sent": [], "sets": [
            {"fmt": "static", "index": 1, "name": SET, "title": "Pack 1",
             "live": 2, "logo": False, "keys": []}]}

    def publish(self, tg, keys, state, **kw):
        with Catalog(self.data / "catalog.db") as cat:
            return bc.publish_format(
                tg, cat, fmt="static", plan_keys=keys, base="pk", title="Pack",
                user_id=1, default_emoji="\U0001f600", per_set=200,
                data_dir=self.data, state=state, bot="YourEmojiBot",
                logo=None, **kw)

    def test_no_pending_item_is_added_to_a_set_with_a_foreign_sticker(self):
        """The whole point: not the first item, and not the second either."""
        keys = self.seed(2)
        tg = FakeTG(sets=self.closed_live_set())
        self.publish(tg, keys, self.closed_state())
        self.assertEqual(len(tg.sets[SET]), 2,
                         "an item was appended to a set holding an "
                         "unattributed sticker")

    def test_the_run_continues_in_a_fresh_set_instead(self):
        """Refusing must not mean refusing to publish at all: a closed set
        rolls to a new one, which is what the startup path already does."""
        keys = self.seed(2)
        tg = FakeTG(sets=self.closed_live_set())
        state = self.closed_state()
        self.publish(tg, keys, state)
        fresh = [n for n in tg.sets if n != SET]
        self.assertTrue(fresh, "publishing stopped entirely instead of rolling")
        self.assertGreaterEqual(sum(len(tg.sets[n]) for n in fresh), 1)

    def test_into_pack_refuses_rather_than_switching_targets(self):
        """Asked for THIS pack by number, silently filling a different one is
        not a fallback -- it is ignoring the instruction."""
        keys = self.seed(2)
        tg = FakeTG(sets=self.closed_live_set())
        with self.assertRaises(SetDrift):
            self.publish(tg, keys, self.closed_state(), into_pack=1)


class MixedFamiliesStillCheckEachItemsOwnFormat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def blank_png(self) -> Path:
        """Transparent but for a couple of stray pixels: the shared
        visible-alpha rule calls this blank, and a blank emoji occupies one of
        the 200 slots forever while showing nothing."""
        p = self.dir / "blank.png"
        im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        im.putpixel((5, 5), (255, 255, 255, 255))
        im.save(p)
        return p

    def test_a_blank_static_is_caught_when_asked_about_static(self):
        self.assertFalse(bc._media_ok(self.blank_png(), "static"))

    def test_the_family_format_answers_yes_to_everything(self):
        """Not a defect in `_media_ok` -- it is a defect to CALL it this way.
        Pinned so the shape of the bug stays visible: "mixed" is not a media
        format and never was."""
        self.assertTrue(bc._media_ok(self.blank_png(), MIXED))

    def test_the_publisher_asks_about_the_item_not_the_family(self):
        """The behaviour that actually matters: a blank item inside a mixed
        family must still be skipped."""
        with Catalog(self.dir / "catalog.db") as cat:
            blank = self.blank_png()
            key = identity.content_key(blank, "static")
            cat.add(content_key=key, fmt="static", file_path=blank,
                    emojis=["\U0001f600"], keywords=["blank"])
            state = {"base": "pk", "sent": [], "sets": []}
            tg = FakeTG()
            bc.publish_format(tg, cat, fmt=MIXED, plan_keys=[key], base="pk",
                              title="Pack", user_id=1,
                              default_emoji="\U0001f600", per_set=200,
                              data_dir=self.dir, state=state,
                              bot="YourEmojiBot", logo=None)
            self.assertEqual(tg.uploaded, [],
                             "a blank emoji was published into a mixed family")
            self.assertIn(key, state.get("skipped", []))


if __name__ == "__main__":
    unittest.main()
