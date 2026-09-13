"""Recovery must never give a stranger's picture our item's identity.

F09. `_near_catalog_match` accepted the single closest candidate by perceptual
hash alone. dHash is a GRAYSCALE STRUCTURE hash, so an opaque red square and an
opaque blue square are distance 0 apart -- and with only the red one in the
catalog, a live blue sticker resolved to the red item's content key. The caller
then wrote that foreign sticker's file_unique_id and custom_emoji_id against our
item and marked it published; the wrong mapping survived a reopen.

The ordinary fresh-upload guard (`identity.same_image`) rejects that pair
outright, because it demands colour agreement as well as structure. The two
paths were asking different questions about the same thing. The hash now only
NOMINATES candidates and `same_image` decides, so a match has to be verified.

What the fix must not do is turn every hard case into a refusal: a genuine
re-encode of our own art still has to resolve, and "I could not look" has to
stay distinct from "this is a stranger".
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tests._panel_fixtures import ROOT

sys.path.insert(0, str(ROOT))

import collection_reconcile as cr
from emojikit import identity
from emojikit.catalog import Catalog


class NearCatalogMatchVerifiesBeforeItAttributes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cats: list[Catalog] = []

    def tearDown(self):
        for cat in self.cats:
            cat.close()
        self.tmp.cleanup()

    def png(self, name, colour, size=(100, 100)) -> Path:
        p = self.dir / name
        Image.new("RGBA", size, colour).save(p)
        return p

    def catalog(self, name, files) -> Catalog:
        cat = Catalog(self.dir / name)
        self.cats.append(cat)
        for f in files:
            cat.add(content_key=identity.content_key(f, "static"), fmt="static",
                    file_path=f, phash=identity.perceptual_hash(f, "static"))
        return cat

    def test_a_foreign_image_of_the_same_shape_is_not_our_item(self):
        """The reproduction. Two uniform squares have dHash distance 0, so the
        hash cannot tell red from blue -- and the hash was the only vote."""
        red, blue = self.png("red.png", (255, 0, 0, 255)), self.png("blue.png", (0, 0, 255, 255))
        self.assertEqual(identity.hamming(identity.perceptual_hash(red, "static"),
                                          identity.perceptual_hash(blue, "static")), 0,
                         "fixture no longer reproduces the hash collision")
        cat = self.catalog("red.db", [red])
        self.assertIsNone(cr._near_catalog_match(cat, blue, "static"))

    def test_our_own_file_still_resolves(self):
        red = self.png("red.png", (255, 0, 0, 255))
        cat = self.catalog("red.db", [red])
        self.assertEqual(cr._near_catalog_match(cat, red, "static"),
                         identity.content_key(red, "static"))

    def test_a_genuine_re_encode_of_our_art_still_resolves(self):
        """This is what recovery is FOR. Telegram re-encodes what it serves
        back, so the content key cannot match and the perceptual path is the
        only way home; tightening it into a refusal would break the feature."""
        red = self.png("red.png", (255, 0, 0, 255))
        cat = self.catalog("red.db", [red])
        reencoded = self.dir / "red_reencoded.webp"
        Image.open(red).convert("RGBA").save(reencoded, "WEBP", quality=88)
        self.assertNotEqual(identity.content_key(reencoded, "static"),
                            identity.content_key(red, "static"))
        self.assertEqual(cr._near_catalog_match(cat, reencoded, "static"),
                         identity.content_key(red, "static"))

    def test_two_genuine_matches_are_ambiguous_not_a_pick(self):
        """Guessing between two verified candidates is what put a foreign llama
        on `sol`. Refusing is the only honest answer."""
        a = self.png("a.png", (200, 0, 0, 255))
        b = self.png("b.png", (206, 0, 0, 255))
        probe = self.png("probe.png", (203, 0, 0, 255))
        cat = self.catalog("ab.db", [a, b])
        self.assertEqual(len(list(cat.all_items())), 2, "fixture deduped itself")
        self.assertEqual(cr._near_catalog_match(cat, probe, "static"), cr.AMBIGUOUS)

    def test_a_candidate_that_cannot_be_examined_is_undecidable(self):
        """Its media is gone, so the comparison never happened. Calling that a
        miss would declare a sticker foreign on the strength of a look we never
        got -- and the caller acts on a miss by treating it as someone else's."""
        ghost = self.png("ghost.png", (255, 0, 0, 255))
        cat = self.catalog("ghost.db", [ghost])
        ghost.unlink()
        blue = self.png("blue.png", (0, 0, 255, 255))
        self.assertEqual(cr._near_catalog_match(cat, blue, "static"), cr.UNDECIDABLE)

    def test_an_empty_catalog_is_a_proven_miss(self):
        """Nothing to compare against is not the same as failing to compare."""
        cat = self.catalog("empty.db", [])
        blue = self.png("blue.png", (0, 0, 255, 255))
        self.assertIsNone(cr._near_catalog_match(cat, blue, "static"))

    def test_animated_keeps_its_old_meaning(self):
        """Vector: there is no raster hash to search with, and a .tgs content
        key survives a re-gzip exactly, so a miss there really is a miss."""
        cat = self.catalog("empty.db", [])
        tgs = self.dir / "x.tgs"
        tgs.write_bytes(b"\x1f\x8b" + b"0" * 40)
        self.assertIsNone(cr._near_catalog_match(cat, tgs, "animated"))

    def test_an_undecodable_raster_is_undecidable_not_a_miss(self):
        """A raster whose hash cannot be computed is a failed look. The old code
        returned None for it, which the caller reads as 'not ours'."""
        cat = self.catalog("empty.db", [])
        broken = self.dir / "broken.png"
        broken.write_bytes(b"\x89PNG\r\n\x1a\n" + b"corrupt" * 8)
        self.assertEqual(cr._near_catalog_match(cat, broken, "static"), cr.UNDECIDABLE)


class TheThreeNonKeyAnswersStayApart(unittest.TestCase):
    """None / AMBIGUOUS / UNDECIDABLE mean three different things, and the
    caller must act differently on each."""

    def test_they_are_distinct_values(self):
        self.assertNotEqual(cr.AMBIGUOUS, cr.UNDECIDABLE)
        self.assertIsNotNone(cr.AMBIGUOUS)
        self.assertIsNotNone(cr.UNDECIDABLE)

class TheResolverActsOnEachAnswerDifferently(unittest.TestCase):
    """Driven through `_resolve_sticker_key` itself, with the download stubbed.

    The distinction only matters because of what the CALLER does with it: a
    None means "a stranger landed in our set" and is acted on, while a refusal
    stops the run for a human. Getting that backwards is how the foreign
    sticker was adopted in the first place.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cats: list[Catalog] = []

    def tearDown(self):
        for cat in self.cats:
            cat.close()
        self.tmp.cleanup()

    def png(self, name, colour, size=(100, 100)) -> Path:
        p = self.dir / name
        Image.new("RGBA", size, colour).save(p)
        return p

    def catalog(self, name, files) -> Catalog:
        cat = Catalog(self.dir / name)
        self.cats.append(cat)
        for f in files:
            cat.add(content_key=identity.content_key(f, "static"), fmt="static",
                    file_path=f, phash=identity.perceptual_hash(f, "static"))
        return cat

    def resolve(self, cat, served: Path):
        """What `_resolve_sticker_key` makes of a live sticker that downloads
        as `served`. No network: the client is a stub with one method."""
        class _Stub:
            def download_file(self, file_id, dest):
                Path(dest).write_bytes(served.read_bytes())
        sticker = {"file_unique_id": "fuid-" + served.stem, "file_id": "fid"}
        return cr._resolve_sticker_key(_Stub(), cat, sticker, self.dir / "dl")

    def test_a_foreign_sticker_resolves_to_nothing(self):
        red = self.png("red.png", (255, 0, 0, 255))
        blue = self.png("blue.png", (0, 0, 255, 255))
        self.assertIsNone(self.resolve(self.catalog("red.db", [red]), blue))

    def test_our_own_sticker_resolves_to_our_key(self):
        red = self.png("red.png", (255, 0, 0, 255))
        cat = self.catalog("red.db", [red])
        self.assertEqual(self.resolve(cat, red), identity.content_key(red, "static"))

    def test_ambiguity_stops_the_run_instead_of_guessing(self):
        a = self.png("a.png", (200, 0, 0, 255))
        b = self.png("b.png", (206, 0, 0, 255))
        probe = self.png("probe.png", (203, 0, 0, 255))
        cat = self.catalog("ab.db", [a, b])
        with self.assertRaises(cr.Unresolvable):
            self.resolve(cat, probe)

    def test_an_unexaminable_candidate_stops_the_run_too(self):
        """The one the old code got wrong in the other direction: it would have
        returned None, and None is acted on as 'this is not ours'."""
        ghost = self.png("ghost.png", (255, 0, 0, 255))
        cat = self.catalog("ghost.db", [ghost])
        ghost.unlink()
        blue = self.png("blue.png", (0, 0, 255, 255))
        with self.assertRaises(cr.Unresolvable):
            self.resolve(cat, blue)


class ScratchFilesArePrivateAndAlwaysRemoved(unittest.TestCase):
    """Downloads land in a file we created, and never outlive the call.

    The names were derived from the sticker's file_unique_id, so they were
    predictable and shared: two runs verifying the same sticker wrote over each
    other, a file or symlink already at that path was written THROUGH, and the
    cleanup deleted whatever was there whether we had made it or not. The
    refusal paths leaked the file entirely.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_calls_never_share_a_path(self):
        with cr._private_download(self.dir, "verify_") as a, \
                cr._private_download(self.dir, "verify_") as b:
            self.assertNotEqual(a, b)
            self.assertTrue(a.exists() and b.exists())

    def test_the_file_exists_before_the_caller_writes_to_it(self):
        """`mkstemp` creates it with O_EXCL, which is what proves it is ours
        and not something that was already sitting at a guessable name."""
        with cr._private_download(self.dir, "verify_") as p:
            self.assertTrue(p.is_file())

    def test_it_is_removed_when_the_block_raises(self):
        leaked = {}
        with self.assertRaises(ValueError):
            with cr._private_download(self.dir, "reconcile_") as p:
                leaked["path"] = p
                raise ValueError("ambiguous, or any other refusal")
        self.assertFalse(leaked["path"].exists(), "a refusal leaked its scratch file")

    def test_nothing_is_left_behind_after_a_normal_call(self):
        with cr._private_download(self.dir, "verify_") as p:
            p.write_bytes(b"downloaded")
        self.assertFalse(p.exists())
        self.assertEqual(list(self.dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
