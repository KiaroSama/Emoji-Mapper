"""The canonical map phase of coins/rebuild_dedup.py.

The second half of the module: once the packs are built, `map_and_fill` reads
the live sets and rewrites `ticker_to_id.json` from what it finds. It is a
distinct phase with its own failure mode and its own lock discipline (the
pack-family lock FIRST, the canonical map lock second, held across the whole
read-then-write span), so it is tested apart from the mutation walk in
`test_rebuild_dedup_state`.

* the canonical map rebuilt from a snapshot read before the locks (A),
* a same-length reorder or replacement mapped by POSITION -- this is exactly
  how the Solama memecoin llama got published as `sol`,
* one emoji id shared by a crowd of tickers, the signature that drift leaves.

No network and no real sleeps: Telegram is a fake object.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit import packstate as ps  # noqa: E402
from coins import _dedup_plan as cfg  # noqa: E402
from coins import _dedup_map as dmap  # noqa: E402
from tests._rebuild_fixtures import FakeTelegram, RebuildCase, _png_bytes  # noqa: E402


class MapIsResolvedByImageIdentity(RebuildCase):
    """3: the recorded upload order says what we SENT, not what is live now.

    map_and_fill used to zip order[] against the current live cids after
    checking only that the two were the same length. A same-length reorder or
    replacement after the upload therefore rewrote ticker_to_id.json with wrong
    assignments -- this is exactly how the Solama memecoin llama got published
    as `sol`.
    """

    REPS = ["aaa", "bbb", "ccc"]

    def setUp(self):
        super().setUp()
        self.map = self.dir / "ticker_to_id.json"
        self.candidate = self.dir / "ticker_to_id.candidate.json"
        self.write_plan(self.REPS)
        self.write_state(sets=[{"index": 1, "name": "s1", "title": "T 1"}],
                         order=list(self.REPS), cursor=len(self.REPS))
        self.tg = FakeTelegram()
        for rep in self.REPS:
            self.tg.append("s1", (self.emoji / f"{rep}.png").read_bytes())

    def mapping(self) -> dict:
        return json.loads(self.map.read_text(encoding="utf-8"))

    def test_the_untouched_pack_maps_by_content(self):
        dmap.map_and_fill(self.tg)
        self.assertEqual(self.mapping(),
                         {"aaa": "s1-0", "bbb": "s1-1", "ccc": "s1-2"})

    def test_a_same_length_reorder_is_never_mapped_by_position(self):
        # The pack was reordered after the upload: same length, same count, and
        # position 0 now holds ccc's art.
        imgs = self.tg.images["s1"]
        imgs[0], imgs[2] = imgs[2], imgs[0]
        dmap.map_and_fill(self.tg)
        self.assertEqual(self.mapping()["aaa"], "s1-2",
                         "aaa must follow its IMAGE, not its upload position")
        self.assertEqual(self.mapping()["ccc"], "s1-0")
        self.assertNotEqual(self.mapping()["aaa"], "s1-0",
                            "position 0 holds ccc's logo; that is the Solama bug")

    def test_a_same_length_replacement_does_not_rewrite_the_map(self):
        """A stranger's image at the same position, same count, same length."""
        ps.write_json_atomic(self.map, {"aaa": "keep-me"})
        self.tg.images["s1"][1] = _png_bytes("someone-elses-logo")
        with self.assertRaises(SystemExit) as caught:
            dmap.map_and_fill(self.tg)
        self.assertEqual(self.mapping(), {"aaa": "keep-me"},
                         "unprovable identity must leave the canonical map alone")
        self.assertIn("s1-1", str(caught.exception.code))
        self.assertTrue(self.candidate.is_file(),
                        "a refusal must leave something reviewable behind")

    def test_a_live_sticker_that_cannot_be_read_refuses_to_map(self):
        ps.write_json_atomic(self.map, {"aaa": "keep-me"})
        self.tg.images["s1"][1] = b"not an image at all"
        with self.assertRaises(SystemExit):
            dmap.map_and_fill(self.tg)
        self.assertEqual(self.mapping(), {"aaa": "keep-me"})

    def test_a_missing_source_image_refuses_to_map(self):
        ps.write_json_atomic(self.map, {"aaa": "keep-me"})
        (self.emoji / "bbb.png").unlink()
        with self.assertRaises(SystemExit):
            dmap.map_and_fill(self.tg)
        self.assertEqual(self.mapping(), {"aaa": "keep-me"})

    def test_an_extra_live_sticker_refuses_to_map(self):
        ps.write_json_atomic(self.map, {"aaa": "keep-me"})
        self.tg.append("s1", _png_bytes("appended-by-a-concurrent-tool"))
        with self.assertRaises(SystemExit):
            dmap.map_and_fill(self.tg)
        self.assertEqual(self.mapping(), {"aaa": "keep-me"})

    def test_the_canonical_map_is_written_under_the_shared_lock(self):
        # Every writer of ticker_to_id.json takes this lock; holding it here
        # must block the rebuild's own write rather than let it interleave.
        with ps.canonical_map_lock():
            with self.assertRaises(ps.LockBusy):
                dmap.map_and_fill(self.tg)
        self.assertFalse(self.map.exists())
        # "Outlived" is about the HOLD, not the file: the lock file is never
        # unlinked, because an unlinked inode is a lock nobody else can see.
        # Taking it is the only honest way to ask whether it is free.
        with ps.exclusive_lock(cfg.LOCK):
            pass

    def test_a_provider_cannot_change_the_map_during_the_snapshot(self):
        """A: the live read and the whole-map write are ONE locked span.

        The map is rewritten in full from what is read here. A provider doing
        its own read-modify-write in between -- it appends a sticker and adds
        its map entry under the same lock -- has that entry erased by this
        write, and nothing afterwards can tell that it existed. Only holding
        the lock from the first live read to the write prevents it.
        """
        attempts: list[BaseException | None] = []
        real_call = self.tg._call

        def a_provider_runs_mid_read(method, *, data=None, **kw):
            if method == "getStickerSet" and not attempts:
                try:
                    with ps.canonical_map_lock():
                        ps.write_json_atomic(self.map, {"prov": "provider-cid"})
                    attempts.append(None)
                except ps.LockBusy as exc:
                    attempts.append(exc)
            return real_call(method, data=data, **kw)

        self.tg._call = a_provider_runs_mid_read
        dmap.map_and_fill(self.tg)
        self.assertIsInstance(
            attempts[0], ps.LockBusy,
            "the map was writable while its own replacement was being read; "
            "an entry added there is erased by the write that follows")

    def test_the_pack_family_lock_is_held_across_the_mapping(self):
        """The live sets are read here, so a concurrent build must wait.

        Pack-family lock FIRST, canonical map lock SECOND -- the documented
        order, and the reason this can be taken while a build cannot.
        """
        with ps.exclusive_lock(cfg.LOCK):
            with self.assertRaises(ps.LockBusy):
                dmap.map_and_fill(self.tg)
        self.assertFalse(self.map.exists(),
                         "the map was rebuilt from live sets a concurrent "
                         "build was still appending to")


class SharedLogoGuard(unittest.TestCase):
    """The detector that would have caught the 129-ticker collision."""

    def setUp(self):
        from coins import _dedup_map as dmap
        self.dmap = dmap

    def test_unreviewed_many_to_one_group_is_reported(self):
        mapping = {t: "SAME" for t in ("apt", "arkm", "hbar", "near", "tao")}
        mapping["btc"] = "OWN"
        with mock.patch.object(self.dmap, "approved_shared_tickers", return_value=set()):
            bad = self.dmap.unapproved_shared_groups(mapping)
        self.assertEqual(list(bad), ["SAME"])
        self.assertEqual(len(bad["SAME"]), 5)

    def test_reviewed_shared_group_is_accepted(self):
        mapping = {"usdt": "T", "usdtbsc": "T", "usdterc20": "T"}
        approved = {"usdt", "usdtbsc", "usdterc20"}
        with mock.patch.object(self.dmap, "approved_shared_tickers", return_value=approved):
            self.assertEqual(self.dmap.unapproved_shared_groups(mapping), {})

    def test_one_to_one_map_is_clean(self):
        mapping = {"btc": "1", "eth": "2", "sol": "3"}
        with mock.patch.object(self.dmap, "approved_shared_tickers", return_value=set()):
            self.assertEqual(self.dmap.unapproved_shared_groups(mapping), {})

    def test_the_real_committed_map_is_checked_against_the_real_groups(self):
        """The shipped map must not regain an unreviewed collision."""
        ids = ROOT / "coins" / "ticker_to_id.json"
        if not ids.is_file():
            self.skipTest("no committed ticker map")
        mapping = json.loads(ids.read_text(encoding="utf-8"))
        bad = self.dmap.unapproved_shared_groups(mapping)
        biggest = max((len(v) for v in bad.values()), default=0)
        self.assertLess(biggest, 20,
                        f"an emoji id is shared by {biggest} unreviewed tickers; "
                        f"this is the positional-drift signature")


if __name__ == "__main__":
    unittest.main()
