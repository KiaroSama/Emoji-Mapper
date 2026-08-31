"""The canonical ticker map and the inventory it fills.

`coins/ticker_to_id.json` is the only copy of the ticker -> custom-emoji-id
mapping, and `alias_map`, `enhance_map` and `remap_ids --apply` all rewrite it
whole. `coins/_inventory` is the other half of the same pipeline: it re-fills
`premium-id:` lines from that map, and it decides which asset a ticker names.

Three failure modes are locked down here: a guessed id where the name was
ambiguous, a lost update where two writers each read before locking, and a
resolver that answered "which asset is this ticker" differently per tool.

`remap_ids`' own content-based mapping is covered in `test_remap_ids`; the
`--fix` path that repoints entries after a live replacement, in
`test_verify_logos`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import packstate as ps  # noqa: E402
from build_pack import EXIT_FAILED, EXIT_OK  # noqa: E402
from coins import _inventory  # noqa: E402
from coins import fetch_cmc as coins_cmc  # noqa: E402
from coins import fetch_paprika as coins_fp  # noqa: E402
from coins import _paprika_api as coins_api  # noqa: E402
from coins import _dedup_map as coins_dmap  # noqa: E402
from tests._cli_fixtures import _load_standalone, _noise_png_bytes  # noqa: E402

# --------------------------------------------------------------------------- #
# coins/alias_map: an ambiguous normalized name must not be guessed
# --------------------------------------------------------------------------- #
INVENTORY = """\
## \U0001f7e1 — Foo Protocol
   ticker: ccc
   premium-id:

## \U0001f7e2 — Bar Coin
   ticker: eee
   premium-id:
"""


class AliasMapAmbiguity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_standalone(ROOT / "coins" / "alias_map.py", "coins_alias_map")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # "Foo Network" and "Foo Token" both normalize to "foo" but hold
        # different emoji ids; "Bar Labs" is the only "bar".
        (self.tmp / "ticker_to_id.json").write_text(
            json.dumps({"aaa": "111", "bbb": "222", "ddd": "333"}), encoding="utf-8")
        (self.tmp / "keywords.csv").write_text(
            "ticker,name,format,file,keywords\n"
            "aaa,Foo Network,svg,logos/svg/aaa.svg,aaa\n"
            "bbb,Foo Token,svg,logos/svg/bbb.svg,bbb\n"
            "ddd,Bar Labs,svg,logos/svg/ddd.svg,ddd\n", encoding="utf-8")
        (self.tmp / "inv.md").write_text(INVENTORY, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self) -> tuple[dict, str, str]:
        buf = io.StringIO()
        with mock.patch.multiple(self.mod, ROOT=self.tmp,
                                 INV=self.tmp / "inv.md",
                                 OUT_INV=self.tmp / "out.md"), \
                contextlib.redirect_stdout(buf):
            self.assertEqual(self.mod.main(), 0)
        mapping = json.loads((self.tmp / "ticker_to_id.json").read_text("utf-8"))
        return mapping, (self.tmp / "out.md").read_text("utf-8"), buf.getvalue()

    def test_ambiguous_name_is_reported_not_guessed(self):
        mapping, filled, out = self._run()
        self.assertNotIn("ccc", mapping)          # would have been "111" before
        self.assertIn("AMBIGUOUS ccc", out)
        self.assertIn("aaa, bbb", out)
        # ccc's id line stays blank for review instead of holding a wrong id.
        self.assertIn("ticker: ccc\n   premium-id:\n", filled)

    def test_unique_name_is_still_applied(self):
        mapping, filled, _ = self._run()
        self.assertEqual(mapping["eee"], "333")
        self.assertIn("premium-id: 333", filled)

    def test_an_interrupted_write_leaves_the_canonical_map_intact(self):
        """ticker_to_id.json is the only copy of the ticker -> emoji mapping.

        write_text truncates the real file first, so a crash before the bytes
        landed emptied it. The atomic write can only fail on a temp file.
        """
        path = self.tmp / "ticker_to_id.json"
        original = json.loads(path.read_text("utf-8"))
        # os.replace is the last step of write_json_atomic and nothing else in
        # this run uses it: failing it simulates dying just before publication.
        with mock.patch("os.replace", side_effect=OSError("interrupted")), \
                mock.patch.multiple(self.mod, ROOT=self.tmp,
                                    INV=self.tmp / "inv.md",
                                    OUT_INV=self.tmp / "out.md"), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(OSError):
            self.mod.main()
        self.assertEqual(json.loads(path.read_text("utf-8")), original)


# --------------------------------------------------------------------------- #
# every writer of coins/ticker_to_id.json: one lock, and the read INSIDE it
# --------------------------------------------------------------------------- #
class StubBot:
    """Just enough Telegram for remap_ids.main()'s startup log line."""

    def get_me(self) -> dict:
        return {"username": "bot"}


class CanonicalMapWritersCannotLoseAnUpdate(unittest.TestCase):
    """6: locking a whole-file rewrite only helps if the READ is inside it.

    Atomic replacement stops a truncated file. It does nothing about a lost
    update: two tools each read the map, each apply their own edit, and the
    second write silently discards the first. Each test here lands another
    writer's update at the exact moment the lock is taken -- the moment a run
    that read beforehand can no longer see -- and requires both edits to
    survive.
    """

    CONCURRENT = {"zzz": "written-by-the-other-tool"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.map = self.tmp / "ticker_to_id.json"
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _racing_lock(self, mod):
        """The other tool's write lands while this one waits for the lock."""
        real = mod.canonical_map_lock

        @contextlib.contextmanager
        def racing():
            with real() as beat:
                current = json.loads(self.map.read_text("utf-8"))
                current.update(self.CONCURRENT)
                ps.write_json_atomic(self.map, current)
                yield beat

        return mock.patch.object(mod, "canonical_map_lock", racing)

    def _mapping(self) -> dict:
        return json.loads(self.map.read_text("utf-8"))

    def test_alias_map_keeps_the_other_writers_ids(self):
        mod = _load_standalone(ROOT / "coins" / "alias_map.py", "alias_map_lock")
        self.map.write_text(json.dumps({"ddd": "333"}), encoding="utf-8")
        (self.tmp / "keywords.csv").write_text(
            "ticker,name,format,file,keywords\n"
            "ddd,Bar Labs,svg,logos/svg/ddd.svg,ddd\n", encoding="utf-8")
        (self.tmp / "inv.md").write_text(INVENTORY, encoding="utf-8")
        with mock.patch.multiple(mod, ROOT=self.tmp, INV=self.tmp / "inv.md",
                                 OUT_INV=self.tmp / "out.md"), \
                self._racing_lock(mod), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mod.main(), 0)
        self.assertEqual(self._mapping(),
                         {"ddd": "333", "eee": "333", **self.CONCURRENT})

    def test_enhance_map_keeps_the_other_writers_ids(self):
        mod = _load_standalone(ROOT / "coins" / "enhance_map.py",
                               "enhance_map_lock")
        self.map.write_text(json.dumps({"btc": "111"}), encoding="utf-8")
        (self.tmp / "inv.md").write_text("   ticker: btcbsc\n   premium-id:\n",
                                         encoding="utf-8")
        with mock.patch.multiple(mod, ROOT=self.tmp, INV=self.tmp / "inv.md",
                                 OUT_INV=self.tmp / "out.md"), \
                self._racing_lock(mod), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mod.main(), 0)
        self.assertEqual(self._mapping(),
                         {"btc": "111", "btcbsc": "111", **self.CONCURRENT})

    def test_remap_apply_backs_up_and_replaces_under_the_lock(self):
        """--apply replaces the map deliberately; it must still be serialised.

        The backup is the proof: it can only contain the other writer's entry
        if this run reached the copy through ``canonical_map_lock`` rather than
        rewriting the file on its own.
        """
        mod = _load_standalone(ROOT / "coins" / "remap_ids.py", "remap_ids_lock")
        emoji = self.tmp / "emoji"
        emoji.mkdir()
        (emoji / "btc.png").write_bytes(_noise_png_bytes("btc"))
        self.map.write_text(json.dumps({"btc": "STALE"}), encoding="utf-8")
        (self.tmp / "state.json").write_text(
            json.dumps({"sets": [{"index": 1, "name": "s1"}]}), encoding="utf-8")

        def cached(tg, token, sets, cache, cache_path):
            """Skip the download phase: one live signature, already analysed."""
            sig = mod.signature(Image.open(emoji / "btc.png"))
            cache["sigs"]["live-btc"] = mod.base64.b64encode(
                sig.astype(mod.np.uint8).tobytes()).decode()
            return []

        argv = ["remap_ids", "--emoji-dir", str(emoji),
                "--state", str(self.tmp / "state.json"),
                "--cache", str(self.tmp / "cache.json"), "--out", str(self.map),
                "--max-distance", "100", "--apply"]
        with mock.patch.multiple(mod, download_live=cached,
                                 Telegram=lambda token: StubBot(),
                                 load_env=lambda: None,
                                 setup_logging=lambda *a, **k: None), \
                self._racing_lock(mod), \
                mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t"},
                                clear=False), \
                mock.patch.object(sys, "argv", argv):
            self.assertEqual(mod.main(), EXIT_OK)
        self.assertEqual(self._mapping(), {"btc": "live-btc"})
        self.assertEqual(
            json.loads(self.map.with_suffix(".prebroken.json").read_text("utf-8")),
            {"btc": "STALE", **self.CONCURRENT},
            "the backup must be of the file as it stood under the lock")

    def test_a_held_lock_stops_the_map_tools_without_writing(self):
        """Waiting is only safe if LockBusy is an exit, not a traceback.

        Each tool returns EXIT_FAILED and leaves the file untouched -- a
        half-written map here is the canonical map for every other tool.
        """
        self.map.write_text(json.dumps({"btc": "111"}), encoding="utf-8")
        (self.tmp / "inv.md").write_text("   ticker: btcbsc\n   premium-id:\n",
                                         encoding="utf-8")
        (self.tmp / "keywords.csv").write_text("ticker,name\nbtc,Bitcoin\n",
                                               encoding="utf-8")
        before = self.map.read_bytes()
        for script, alias in (("alias_map.py", "alias_map_busy"),
                              ("enhance_map.py", "enhance_map_busy")):
            with self.subTest(script=script):
                mod = _load_standalone(ROOT / "coins" / script, alias)
                with mock.patch.multiple(mod, ROOT=self.tmp,
                                         INV=self.tmp / "inv.md",
                                         OUT_INV=self.tmp / "out.md"), \
                        ps.canonical_map_lock(), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(mod.main(), EXIT_FAILED)
                self.assertEqual(self.map.read_bytes(), before,
                                 f"{script} rewrote the map without the lock")


# --------------------------------------------------------------------------- #
# coins/_inventory: one re-fill loop, one ticker->asset resolver
# --------------------------------------------------------------------------- #
ALIAS_INVENTORY = """\
## \U0001f7e1 — Avalanche C-Chain
   ticker: avaxc
   premium-id:

## \U0001f7e2 — BitTorrent Chain
   ticker: bttc
   premium-id:
"""


class OneInventoryImplementation(unittest.TestCase):
    """Four verbatim copies of the re-fill loop, and a resolver that had drifted.

    enhance_map knew that avaxc is Avalanche and bttc is BitTorrent Chain;
    fetch_paprika's base_ticker did not, so "which asset is this ticker" had two
    answers depending on which tool ran. Merging that alias set is the ONE
    intentional behaviour change here.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.inv = self.tmp / "inv.md"
        self.out = self.tmp / "out.md"
        self.inv.write_text(ALIAS_INVENTORY, encoding="utf-8")

    def test_the_explicit_aliases_now_reach_the_fetchers(self):
        self.assertEqual(_inventory.base_ticker("avaxc"), "avax")
        self.assertEqual(_inventory.base_ticker("bttc"), "btt")
        # The paprika search moved to coins._paprika_api; assert on the module
        # that CALLS it, not on one that only used to re-export it.
        self.assertIs(coins_api.base_ticker, _inventory.base_ticker)
        self.assertIs(coins_cmc.base_ticker, _inventory.base_ticker)

    def test_the_suffix_rules_are_unchanged(self):
        self.assertEqual(_inventory.base_ticker("kibabsc"), "kiba")
        self.assertEqual(_inventory.base_ticker("btc"), "btc")
        # A one-character suffix is not identity evidence: "ghc" must not
        # inherit "gh"'s logo.
        self.assertEqual(_inventory.base_ticker("ghc"), "ghc")

    def test_the_re_fill_loop_has_exactly_one_definition(self):
        """It lived in four verbatim copies; the count IS the regression."""
        copies = sorted(p.name for p in (ROOT / "coins").glob("*.py")
                        if "premium-id: {eid}" in p.read_text(encoding="utf-8"))
        self.assertEqual(copies, ["_inventory.py"])

    def test_every_tool_re_fills_through_the_same_function(self):
        self.assertIs(coins_dmap.refill_inventory, _inventory.refill_inventory)
        self.assertIs(coins_cmc.refill_inventory, coins_fp.refill_inventory,
                      "fetch_cmc must not grow its own copy again")

    def test_an_unmapped_ticker_is_blanked_not_left_stale(self):
        self.inv.write_text("   ticker: btc\n   premium-id: 999\n",
                            encoding="utf-8")
        self.assertEqual(_inventory.refill_inventory({}, self.inv, self.out),
                         (0, 1))
        self.assertEqual(self.out.read_text("utf-8"),
                         "   ticker: btc\n   premium-id:\n")

    def test_indentation_and_ids_survive_the_round_trip(self):
        filled, total = _inventory.refill_inventory(
            {"avaxc": "111", "bttc": "222"}, self.inv, self.out)
        self.assertEqual((filled, total), (2, 2))
        self.assertIn("   premium-id: 111", self.out.read_text("utf-8"))

    def test_parse_missing_skips_what_is_already_mapped(self):
        self.assertEqual(_inventory.parse_missing({"avaxc"}, self.inv),
                         [("BitTorrent Chain", "bttc")])


if __name__ == "__main__":
    unittest.main()
