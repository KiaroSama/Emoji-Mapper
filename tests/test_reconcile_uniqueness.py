"""R06: unique means every rival was EXCLUDED, not that only one was readable.

The remaining hole in the previous round's F09. That fix made a perceptual match
nominate rather than decide, and verified each candidate by content -- but it
then returned the first verified key before looking at what it had failed to
examine.

The reproduction is the ugly part: with two catalog entries (red 245 and red
250) both matching a live red-248 sticker, the function correctly reported
ambiguity. Deleting ONE candidate's file made it answer "unique" with the
survivor. Removing evidence promoted a guess to a certainty, and reconciliation
writes that attribution permanently.

So the matrix below is verified-count crossed with unknown-count, and the rule
is simple: a verified match is unique only when nothing else could have been the
answer.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from emojikit import collection_reconcile as cr  # noqa: E402
from emojikit import identity  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402


class UniqueRequiresExcludingEveryRival(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cats: list[Catalog] = []

    def tearDown(self):
        for cat in self.cats:
            cat.close()
        self.tmp.cleanup()

    def png(self, name, colour) -> Path:
        p = self.dir / name
        Image.new("RGBA", (100, 100), colour).save(p)
        return p

    def catalog(self, name, files) -> Catalog:
        cat = Catalog(self.dir / name)
        self.cats.append(cat)
        for f in files:
            cat.add(content_key=identity.content_key(f, "static"), fmt="static",
                    file_path=f, phash=identity.perceptual_hash(f, "static"))
        return cat

    # ---- the reproduction, both halves ---------------------------------- #

    def test_two_readable_rivals_are_ambiguous(self):
        """The control: while both files exist it already answered correctly."""
        a, b = self.png("a.png", (245, 0, 0, 255)), self.png("b.png", (250, 0, 0, 255))
        probe = self.png("probe.png", (248, 0, 0, 255))
        cat = self.catalog("two.db", [a, b])
        self.assertEqual(cr._near_catalog_match(cat, probe, "static"), cr.AMBIGUOUS)

    def test_deleting_one_rivals_file_does_not_make_the_other_unique(self):
        """The defect. Less evidence must never mean more certainty."""
        a, b = self.png("a.png", (245, 0, 0, 255)), self.png("b.png", (250, 0, 0, 255))
        probe = self.png("probe.png", (248, 0, 0, 255))
        cat = self.catalog("two.db", [a, b])
        b.unlink()
        self.assertEqual(cr._near_catalog_match(cat, probe, "static"),
                         cr.UNDECIDABLE)

    def test_a_rival_that_fails_to_compare_blocks_uniqueness_too(self):
        """Not just a missing file: any undecidable comparison is a live rival."""
        a, b = self.png("a.png", (245, 0, 0, 255)), self.png("b.png", (250, 0, 0, 255))
        probe = self.png("probe.png", (248, 0, 0, 255))
        cat = self.catalog("two.db", [a, b])
        real = identity.same_image

        def flaky(x, y, fmt):
            return None if Path(x).name == "b.png" else real(x, y, fmt)

        with mock.patch.object(identity, "same_image", flaky):
            self.assertEqual(cr._near_catalog_match(cat, probe, "static"),
                             cr.UNDECIDABLE)

    # ---- the rest of the matrix ----------------------------------------- #

    def test_one_verified_and_no_rival_is_still_unique(self):
        """The fix must not make every attribution undecidable."""
        a = self.png("a.png", (245, 0, 0, 255))
        cat = self.catalog("one.db", [a])
        self.assertEqual(cr._near_catalog_match(cat, a, "static"),
                         identity.content_key(a, "static"))

    def test_a_true_negative_is_still_a_negative(self):
        """A proven miss stays a miss: that is what lets a stranger be spotted."""
        red = self.png("red.png", (255, 0, 0, 255))
        blue = self.png("blue.png", (0, 0, 255, 255))
        cat = self.catalog("red.db", [red])
        self.assertIsNone(cr._near_catalog_match(cat, blue, "static"))

    def test_an_item_with_no_stored_hash_is_compared_not_skipped(self):
        """It cannot be NOMINATED, so it must not be silently excluded either.

        The hash is an optimisation for skipping distant items; without one the
        only honest move is the same content comparison a nominated candidate
        gets. Here that comparison excludes it, so a proven miss is still a
        miss -- the conservative reading must not make attribution impossible
        in a catalog whose rows predate perceptual hashing.
        """
        red = self.png("red.png", (255, 0, 0, 255))
        blue = self.png("blue.png", (0, 0, 255, 255))
        cat = self.catalog("red.db", [red])
        cat.db.execute("UPDATE items SET phash=NULL")
        cat.db.commit()
        self.assertIsNone(cr._near_catalog_match(cat, blue, "static"))

    def test_an_unhashed_item_we_cannot_read_is_a_live_rival(self):
        """No hash AND no file: nothing excludes it, so nothing is unique."""
        red = self.png("red.png", (255, 0, 0, 255))
        blue = self.png("blue.png", (0, 0, 255, 255))
        cat = self.catalog("red.db", [red])
        cat.db.execute("UPDATE items SET phash=NULL")
        cat.db.commit()
        red.unlink()
        self.assertEqual(cr._near_catalog_match(cat, blue, "static"),
                         cr.UNDECIDABLE)

    def test_an_unhashed_item_can_still_be_verified_as_the_match(self):
        red = self.png("red.png", (255, 0, 0, 255))
        cat = self.catalog("red.db", [red])
        cat.db.execute("UPDATE items SET phash=NULL")
        cat.db.commit()
        self.assertEqual(cr._near_catalog_match(cat, red, "static"),
                         identity.content_key(red, "static"))

    def test_a_failed_candidate_enumeration_is_undecidable(self):
        red = self.png("red.png", (255, 0, 0, 255))
        cat = self.catalog("red.db", [red])
        with mock.patch.object(type(cat), "all_items",
                               side_effect=sqlite3.OperationalError("db locked")):
            self.assertEqual(cr._near_catalog_match(cat, red, "static"),
                             cr.UNDECIDABLE)

    def test_an_undecidable_probe_is_not_a_miss(self):
        red = self.png("red.png", (255, 0, 0, 255))
        cat = self.catalog("red.db", [red])
        with mock.patch.object(identity, "perceptual_hash", lambda p, f: None):
            self.assertEqual(cr._near_catalog_match(cat, red, "static"),
                             cr.UNDECIDABLE)


class NothingAmbiguousIsEverWritten(unittest.TestCase):
    """The consequence, through the resolver and back out of SQLite.

    `_resolve_sticker_key` raises for both AMBIGUOUS and UNDECIDABLE, so the
    caller stops for a human instead of recording an attribution. Reopening the
    database is the assertion that matters: the defect's cost was a row that
    outlived the process.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "cat.db"
        self.cat = Catalog(self.db)
        self.addCleanup(self.cat.close)
        self.addCleanup(self.tmp.cleanup)
        self.a = self.dir / "a.png"
        self.b = self.dir / "b.png"
        Image.new("RGBA", (100, 100), (245, 0, 0, 255)).save(self.a)
        Image.new("RGBA", (100, 100), (250, 0, 0, 255)).save(self.b)
        for f in (self.a, self.b):
            self.cat.add(content_key=identity.content_key(f, "static"),
                         fmt="static", file_path=f,
                         phash=identity.perceptual_hash(f, "static"))
        self.probe = self.dir / "probe.png"
        Image.new("RGBA", (100, 100), (248, 0, 0, 255)).save(self.probe)

    def test_an_unprovable_attribution_writes_no_fuid(self):
        self.b.unlink()          # one verified, one unreadable rival

        class _Stub:
            def __init__(self, served):
                self.served = served

            def download_file(self, file_id, dest):
                Path(dest).write_bytes(self.served.read_bytes())

        sticker = {"file_unique_id": "fuid-probe", "file_id": "fid"}
        with self.assertRaises(cr.Unresolvable):
            cr._resolve_sticker_key(_Stub(self.probe), self.cat, sticker,
                                    self.dir / "dl")
        self.cat.close()
        con = sqlite3.connect(self.db)
        try:
            seen = [r[0] for r in con.execute("SELECT file_unique_id FROM seen_files")]
            pubs = con.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
        finally:
            con.close()
        self.assertNotIn("fuid-probe", seen)
        self.assertEqual(pubs, 0)


if __name__ == "__main__":
    unittest.main()
