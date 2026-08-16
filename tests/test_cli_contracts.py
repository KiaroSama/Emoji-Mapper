"""Entry-point contracts: exit codes, argument validation, resource bounds.

Each test here locks down a failure that a run reported as success (or an
unbounded read that a hostile input could turn into an OOM). They exercise the
real entry points with deterministic fakes -- no network, no sleeping, no
writes outside a temp directory.
"""

from __future__ import annotations

import contextlib
import csv
import gzip
import importlib.util
import io
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from PIL import Image  # noqa: E402

import build_pack as bp  # noqa: E402
import fetch_pack  # noqa: E402
import make_emoji_pngs as m  # noqa: E402
import panel as p  # noqa: E402
from build_pack import EXIT_FAILED, EXIT_OK, EXIT_USAGE  # noqa: E402
from emojikit.media import TGS_MAX_UNPACKED  # noqa: E402

RED = (240, 20, 20, 255)
EMPTY_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"></svg>'


def _load_standalone(path: Path, name: str):
    """Import a script as its own module object, with a neutral ``sys.argv``.

    ``coins/`` is not a package and its scripts read ``sys.argv`` at import
    time, so a plain import under a test runner would parse the runner's own
    arguments. Loading a private module object also keeps the reload-based
    tests below from mutating what other test modules already imported.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", [path.name]):
        spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# fetch_pack: pack-level failures and --limit validation
# --------------------------------------------------------------------------- #
class DeadTelegram:
    """Authenticates, but every pack lookup fails (deleted/misspelled names)."""

    def __init__(self, *_a, **_k):
        self.lookups: list[str] = []

    def get_me(self):
        return {"username": "testbot"}

    def get_sticker_set(self, name):
        self.lookups.append(name)
        raise RuntimeError("Bad Request: STICKERSET_INVALID")


class FetchPackExitCodes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # setup_logging would add handlers and write a real file into logs/.
        self.patches = [
            mock.patch.object(fetch_pack, "setup_logging", lambda *a, **k: None),
            mock.patch.object(fetch_pack, "load_env", lambda *a, **k: None),
            mock.patch.dict(os.environ, {"GENERAL_BOT_TOKEN": "unit-test-token"}),
        ]
        for pt in self.patches:
            pt.start()

    def tearDown(self):
        for pt in reversed(self.patches):
            pt.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _main(self, *args) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fetch_pack.main(["--data-dir", str(self.tmp), *args])
        return code, buf.getvalue()

    def test_every_pack_failing_exits_nonzero(self):
        tg = DeadTelegram()
        with mock.patch.object(fetch_pack, "Telegram", lambda *a, **k: tg):
            code, out = self._main("gone_by_bot", "https://t.me/addemoji/also_gone")
        self.assertEqual(tg.lookups, ["gone_by_bot", "also_gone"])
        # Nothing was ingested and both packs failed: this must not look clean.
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("packs_failed=2", out)

    def test_negative_limit_is_rejected_before_any_api_call(self):
        tg = DeadTelegram()
        with mock.patch.object(fetch_pack, "Telegram", lambda *a, **k: tg):
            code, _ = self._main("somepack", "--limit", "-1")
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(tg.lookups, [])  # rejected before touching the API

    def test_out_of_range_phash_threshold_is_a_usage_error(self):
        # Catalog() raises ValueError for this, but only long after argparse is
        # done: the run died with a stack trace instead of the usage exit.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as ctx:
            fetch_pack.main(["somepack", "--data-dir", str(self.tmp),
                             "--phash-threshold", "64"])
        self.assertEqual(ctx.exception.code, EXIT_USAGE)
        self.assertIn("out of range", stderr.getvalue())

    def test_usable_phash_threshold_is_still_accepted(self):
        tg = DeadTelegram()
        with mock.patch.object(fetch_pack, "Telegram", lambda *a, **k: tg):
            code, _ = self._main("somepack", "--phash-threshold", "4")
        self.assertEqual(code, EXIT_FAILED)  # the pack fails, the argument does not
        self.assertEqual(tg.lookups, ["somepack"])


# --------------------------------------------------------------------------- #
# make_emoji_pngs: --limit validation and source fallback
# --------------------------------------------------------------------------- #
class MakeEmojiPngsContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "in"
        self.out = self.tmp / "out"
        self.src.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _raster(self, name: str, color=RED) -> Path:
        path = self.src / name
        Image.new("RGBA", (40, 70), color).save(path)
        return path

    def _run(self, limit: int = 0) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return m._run_general(self.src, self.out, limit)

    def test_negative_limit_is_rejected(self):
        self._raster("a.png")
        argv = ["make_emoji_pngs.py", "--in", str(self.src),
                "--out", str(self.out), "--limit", "-1"]
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()):
            code = m.main()
        self.assertEqual(code, EXIT_USAGE)
        self.assertFalse(self.out.exists())  # no silent empty "success" run

    def test_blank_svg_falls_back_to_the_healthy_raster(self):
        # The SVG wins on priority but renders to nothing; foo.png is the only
        # way this name gets an emoji at all.
        (self.src / "foo.svg").write_text(EMPTY_SVG, encoding="utf-8")
        self._raster("foo.png", RED)
        self.assertEqual(self._run(), EXIT_OK)
        with Image.open(self.out / "foo.png") as im:
            self.assertEqual(im.size, (100, 100))
            px = im.convert("RGBA").getpixel((50, 50))
        self.assertGreater(px[0], px[2])          # red raster, not a blank SVG
        self.assertFalse(m._is_blank(Image.open(self.out / "foo.png")))

    def test_group_with_no_usable_source_still_fails(self):
        (self.src / "foo.svg").write_text(EMPTY_SVG, encoding="utf-8")
        self._raster("foo.png", (0, 0, 0, 0))     # blank raster too
        self.assertEqual(self._run(), EXIT_FAILED)
        self.assertFalse((self.out / "foo.png").exists())

    def test_preferred_source_is_still_tried_first(self):
        (self.src / "foo.svg").write_text(EMPTY_SVG, encoding="utf-8")
        self._raster("foo.png")
        self.assertEqual([s.name for s in m._pick_sources(self.src)], ["foo.svg"])


class MakeEmojiPngsLegacyFallback(unittest.TestCase):
    """Legacy mode must count failure per OUTPUT STEM, like general mode.

    A blank SVG followed by a healthy logos/png/<t>.png produces the emoji just
    fine, but the SVG attempt stayed on the failed counter -- so a completely
    successful run reported PARTIAL and the launcher retried it forever.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        logos = self.tmp / "logos"
        self.svg = logos / "svg"
        self.png = logos / "png"
        self.out = logos / "emoji"
        for d in (self.svg, self.png):
            d.mkdir(parents=True)
        self.patch = mock.patch.multiple(
            m, ROOT=self.tmp, SVG_DIR=self.svg, PNG_DIR=self.png, OUT_DIR=self.out)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, limit: int = 0) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.code = m._run_legacy(limit)
        return buf.getvalue()

    def test_blank_svg_with_healthy_png_fallback_exits_ok(self):
        (self.svg / "btc.svg").write_text(EMPTY_SVG, encoding="utf-8")
        Image.new("RGBA", (40, 70), RED).save(self.png / "btc.png")
        out = self._run()
        self.assertEqual(self.code, EXIT_OK)
        self.assertIn("failed=0", out)
        with Image.open(self.out / "btc.png") as im:
            self.assertEqual(im.size, (100, 100))

    def test_stem_with_no_usable_source_anywhere_still_fails(self):
        (self.svg / "bad.svg").write_text(EMPTY_SVG, encoding="utf-8")
        self._run()
        self.assertEqual(self.code, EXIT_FAILED)
        self.assertFalse((self.out / "bad.png").exists())

    def test_blank_png_fallback_is_counted_once_not_twice(self):
        (self.svg / "bad.svg").write_text(EMPTY_SVG, encoding="utf-8")
        Image.new("RGBA", (40, 70), (0, 0, 0, 0)).save(self.png / "bad.png")
        Image.new("RGBA", (40, 70), RED).save(self.png / "good.png")
        out = self._run()
        # One broken stem, one good one -> PARTIAL, and "failed" counts the
        # stem once even though both of its sources failed.
        self.assertIn("failed=1", out)
        self.assertEqual(self.code, 3)


# --------------------------------------------------------------------------- #
# panel: /lottie/ must not decompress without a bound
# --------------------------------------------------------------------------- #
class PanelLottieBound(unittest.TestCase):
    TOKEN = "test-token-value"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        good = self.tmp / "good.tgs"
        good.write_bytes(gzip.compress(
            json.dumps({"v": "5.5", "w": 512, "h": 512, "fr": 60,
                        "ip": 0, "op": 60, "layers": []}).encode()))
        # A zip bomb: ~9 MB of JSON, a few KB on disk. gzip.decompress would
        # happily materialise all of it (and a real one, gigabytes).
        bomb = self.tmp / "bomb.tgs"
        bomb.write_bytes(gzip.compress(
            json.dumps({"pad": "A" * (TGS_MAX_UNPACKED + 1_000_000)}).encode()))

        by_key = {"good": good, "bomb": bomb}
        handler = p.make_handler([], by_key, self.tmp / "catalog.db", self.TOKEN)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive(), "panel server thread leaked")
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self, path):
        req = request.Request(f"http://127.0.0.1:{self.port}{path}")
        try:
            with request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except error.HTTPError as e:
            return e.code, e.read()

    def test_oversized_tgs_is_rejected(self):
        code, body = self._get("/lottie/bomb")
        self.assertEqual(code, 500)
        self.assertLess(len(body), 1024)  # nothing of the bomb reached the client

    def test_normal_tgs_still_served_as_json(self):
        code, body = self._get("/lottie/good")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["w"], 512)

    def test_unknown_key_is_404(self):
        self.assertEqual(self._get("/lottie/nope")[0], 404)


# --------------------------------------------------------------------------- #
# logsetup: a bad retention value must not break logging at import
# --------------------------------------------------------------------------- #
class LogRetentionParsing(unittest.TestCase):
    PATH = ROOT / "emojikit" / "logsetup.py"

    def _fresh(self, value: str | None):
        env = {} if value is None else {"EMOJI_LOG_RETENTION_DAYS": value}
        with mock.patch.dict(os.environ, env, clear=False), \
                contextlib.redirect_stderr(io.StringIO()):
            if value is None:
                os.environ.pop("EMOJI_LOG_RETENTION_DAYS", None)
            return _load_standalone(self.PATH, "logsetup_probe")

    def test_garbage_value_falls_back_to_the_default(self):
        # A bare int() here raised ValueError at import: logging, and with it
        # every entry point that imports it, died before argparse could speak.
        mod = self._fresh("30 days")
        self.assertEqual(mod.LOG_RETENTION_DAYS, 30)
        self.assertTrue(callable(mod.setup_logging))

    def test_empty_value_falls_back_to_the_default(self):
        self.assertEqual(self._fresh("").LOG_RETENTION_DAYS, 30)

    def test_negative_value_is_clamped_to_disabled(self):
        mod = self._fresh("-5")
        self.assertEqual(mod.LOG_RETENTION_DAYS, 0)
        self.assertEqual(mod.prune_old_logs(mod.LOG_RETENTION_DAYS), 0)

    def test_valid_value_is_honoured(self):
        self.assertEqual(self._fresh("7").LOG_RETENTION_DAYS, 7)


# --------------------------------------------------------------------------- #
# coins/fetch_logos: a cached non-image must not be trusted
# --------------------------------------------------------------------------- #
HTML_ERROR = b"<html><body>429 Too Many Requests</body></html>"


def _png_bytes(color=(20, 120, 200, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (32, 32), color).save(buf, "PNG")
    return buf.getvalue()


def _noise_png_bytes(key: str, size: int = 64) -> bytes:
    """Deterministic per-key noise; two different keys never look alike.

    Flat colours are useless for image identity: a dHash compares neighbouring
    pixels, so every solid image hashes to zero and "is this sticker the one we
    uploaded?" would answer yes for any picture at all.
    """
    rnd = random.Random(key)
    img = Image.new("RGBA", (size, size))
    px = img.load()
    for x in range(size):
        for y in range(size):
            v = rnd.randrange(256)
            px[x, y] = (v, v, v, 255)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class FetchLogosCacheValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_standalone(ROOT / "coins" / "fetch_logos.py", "coins_fetch_logos")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.png_dir = self.tmp / "png"
        self.png_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_image_rejects_error_bodies_and_truncation(self):
        good = self.tmp / "good.png"
        good.write_bytes(_png_bytes())
        html = self.tmp / "html.png"
        html.write_bytes(HTML_ERROR)
        cut = self.tmp / "cut.png"
        cut.write_bytes(_png_bytes()[:60])
        blank = self.tmp / "blank.png"
        Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(blank)

        self.assertTrue(self.mod._valid_image(good))
        self.assertFalse(self.mod._valid_image(html))
        self.assertFalse(self.mod._valid_image(cut))
        self.assertFalse(self.mod._valid_image(blank))

    def test_bad_download_never_reaches_the_destination(self):
        dest = self.png_dir / "abc.png"
        with mock.patch.object(self.mod, "_get", lambda *a, **k: HTML_ERROR):
            self.assertFalse(self.mod.fetch_logo("http://x/abc.png", dest))
        self.assertFalse(dest.exists())
        self.assertEqual(list(self.png_dir.iterdir()), [])  # no .part left behind

    def test_corrupt_cached_png_is_redownloaded(self):
        dest = self.png_dir / "btc.png"
        dest.write_bytes(HTML_ERROR)          # what an earlier run cached
        asked: list[str] = []

        def fake_get(url, *, binary=False, retries=6):
            asked.append(url)
            if url.startswith(self.mod.API):
                return [{"symbol": "BTC", "name": "Bitcoin",
                         "image": "http://img.test/btc.png"}]
            return _png_bytes()

        with mock.patch.multiple(self.mod, _get=fake_get, MAX_PAGES=1,
                                 PAGE_DELAY=0, IMG_DELAY=0,
                                 PNG_DIR=self.png_dir, SVG_DIR=self.tmp / "svg",
                                 KEYWORDS_CSV=self.tmp / "keywords.csv"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.mod.main(), 0)

        self.assertIn("http://img.test/btc.png", asked)  # cache was not trusted
        self.assertTrue(self.mod._valid_image(dest))

    def test_good_cached_png_is_not_redownloaded(self):
        dest = self.png_dir / "btc.png"
        dest.write_bytes(_png_bytes())
        asked: list[str] = []

        def fake_get(url, *, binary=False, retries=6):
            asked.append(url)
            if url.startswith(self.mod.API):
                return [{"symbol": "BTC", "name": "Bitcoin",
                         "image": "http://img.test/btc.png"}]
            return _png_bytes()

        with mock.patch.multiple(self.mod, _get=fake_get, MAX_PAGES=1,
                                 PAGE_DELAY=0, IMG_DELAY=0,
                                 PNG_DIR=self.png_dir, SVG_DIR=self.tmp / "svg",
                                 KEYWORDS_CSV=self.tmp / "keywords.csv"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.mod.main()

        self.assertNotIn("http://img.test/btc.png", asked)

    def _keywords_rows(self) -> dict[str, dict]:
        with open(self.tmp / "keywords.csv", encoding="utf-8", newline="") as fh:
            return {r["ticker"]: r for r in csv.DictReader(fh)}

    def test_corrupt_leftover_png_is_not_advertised(self):
        """The final PNG_DIR sweep never validated what it advertised.

        These files are not on the download path at all -- they come from a
        previous run's pages -- so an error page cached as ``<ticker>.png``
        went straight into keywords.csv and from there into a pack.
        """
        (self.png_dir / "junk.png").write_bytes(HTML_ERROR)
        (self.png_dir / "cut.png").write_bytes(_png_bytes()[:60])
        (self.png_dir / "ok.png").write_bytes(_png_bytes())

        def fake_get(url, *, binary=False, retries=6):
            return []            # no market pages this run: only the sweep runs

        with mock.patch.multiple(self.mod, _get=fake_get, MAX_PAGES=1,
                                 PAGE_DELAY=0, IMG_DELAY=0,
                                 PNG_DIR=self.png_dir, SVG_DIR=self.tmp / "svg",
                                 KEYWORDS_CSV=self.tmp / "keywords.csv"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.mod.main(), 0)

        rows = self._keywords_rows()
        self.assertIn("ok", rows)
        self.assertNotIn("junk", rows)
        self.assertNotIn("cut", rows)


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
                bp.write_json_atomic(self.map, current)
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
                        bp.canonical_map_lock(), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(mod.main(), EXIT_FAILED)
                self.assertEqual(self.map.read_bytes(), before,
                                 f"{script} rewrote the map without the lock")


# --------------------------------------------------------------------------- #
# coins/verify_logos: --fix must be retry-safe, identity-checked and honest
#                     about failure
# --------------------------------------------------------------------------- #
OLD_CID, NEW_CID = "cid-old", "cid-new"


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class FakeDownload:
    """What ``Telegram.download_file`` expects back from ``session.get``."""

    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


class ReplaceSession:
    """requests.Session stand-in serving ONE sticker set, with file downloads.

    ``lose_reply`` models the dangerous case: Telegram APPLIES the replacement
    and the response is lost on the way back. A blind retry then re-sends a
    non-idempotent call against an ``old_sticker`` that no longer exists.

    ``lose_confirm`` models the worse one: the call succeeds and the
    POSTCONDITION read never comes back, so the run cannot learn the new id --
    while the canonical map still names the old one, which no longer exists.
    """

    def __init__(self, before: list[str], after: list[str], *,
                 lose_reply: bool = False, lose_confirm: bool = False,
                 source: Path | None = None, images: dict | None = None):
        self.stickers = [{"custom_emoji_id": c, "file_id": f"fid-{c}"}
                         for c in before]
        self.after = [{"custom_emoji_id": c, "file_id": f"fid-{c}"} for c in after]
        self.lose_reply = lose_reply
        self.lose_confirm = lose_confirm
        # Every sticker serves the CURRENT bytes of the prepared source unless
        # `images` overrides it for a file_id.
        self.source = source
        self.images = dict(images or {})
        self.replaced = False
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, url, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(data or {}), dict(files or {})))
        if method == "getStickerSet":
            if self.lose_confirm and self.replaced:
                raise requests.ConnectionError("connection reset by peer")
            return FakeResponse({"ok": True, "result": {
                "name": data["name"], "stickers": self.stickers}})
        if method == "replaceStickerInSet":
            self.replaced = True
            self.stickers = list(self.after)          # Telegram applied it...
            if self.lose_reply:                       # ...and the reply vanished
                raise requests.ConnectionError("connection reset by peer")
            return FakeResponse({"ok": True, "result": True})
        if method == "getFile":
            return FakeResponse({"ok": True, "result": {
                "file_path": f"live/{data['file_id']}"}})
        raise AssertionError(f"unexpected Bot API method: {method}")

    def get(self, url, timeout=None):
        file_id = url.rsplit("/", 1)[-1]
        body = self.images.get(file_id)
        if body is None and self.source is not None:
            body = self.source.read_bytes()
        return FakeDownload(body or b"")

    def method(self, name: str) -> list[tuple[str, dict, dict]]:
        return [c for c in self.calls if c[0] == name]


class VerifyLogosFix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_standalone(ROOT / "coins" / "verify_logos.py",
                                   "coins_verify_logos_probe")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.emoji = self.tmp / "emoji"
        self.emoji.mkdir()
        Image.new("RGBA", (48, 48), (10, 200, 40, 255)).save(self.emoji / "btc.png")
        self.src = self.emoji / "btc.png"
        # Two tickers share one custom emoji: BOTH must be repointed, and only
        # at an identity we can prove.
        self.map_path = self.tmp / "ticker_to_id.json"
        self.map_path.write_text(json.dumps(
            {"btc": OLD_CID, "wbtc": OLD_CID, "eth": "cid-eth"}), encoding="utf-8")
        self.sets = [{"name": "gvcryptoemoji1_by_bot", "index": 1}]
        self.patches = [
            mock.patch.object(self.mod, "ROOT", self.tmp),
            mock.patch.object(self.mod, "fetch_markets", lambda top: [
                {"symbol": "btc", "name": "Bitcoin", "image": "http://x/btc.png"}]),
            # Noise, not a flat colour: identity has to be provable at all.
            mock.patch.object(self.mod, "fetch_image",
                              lambda url: _noise_png_bytes("official-btc")),
            mock.patch.object(bp.time, "sleep", lambda *_a: None),
        ]
        for pt in self.patches:
            pt.start()

    def tearDown(self):
        for pt in reversed(self.patches):
            pt.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mapping(self) -> dict:
        return json.loads(self.map_path.read_text(encoding="utf-8"))

    def intent(self) -> dict | None:
        path = self.mod._intent_path()
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def _session(self, before, after, **kw):
        session = ReplaceSession(before, after, source=self.src, **kw)
        tg = bp.Telegram("unit-test-token")
        tg.s = session
        return tg, session

    def _fix(self, before, after, state_path=None, **kw):
        tg, session = self._session(before, after, **kw)
        ok = self.mod.fix_one(tg, 42, self.sets, self.map_path, self.emoji,
                              "btc", state_path)
        return ok, session

    def test_timeout_after_apply_is_verified_not_resent(self):
        ok, session = self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"],
                                lose_reply=True)
        self.assertTrue(ok)
        # Exactly one attempt: the applied_check saw the change live, so the
        # non-idempotent call was never repeated.
        self.assertEqual(len(session.method("replaceStickerInSet")), 1)
        self.assertEqual(self.mapping()["btc"], NEW_CID)

    def test_the_uploaded_body_is_bytes_a_retry_can_resend(self):
        _, session = self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"],
                               lose_reply=True)
        _, _, files = session.method("replaceStickerInSet")[0]
        _name, body, _mime = files["file0"]
        # An open handle is exhausted after attempt 1: every retry would have
        # uploaded an empty sticker.
        self.assertIsInstance(body, bytes)
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_every_entry_sharing_the_old_cid_is_repointed(self):
        ok, _ = self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"])
        self.assertTrue(ok)
        self.assertEqual(self.mapping(), {"btc": NEW_CID, "wbtc": NEW_CID,
                                          "eth": "cid-eth"})
        self.assertIsNone(self.intent(), "a settled replacement leaves no intent")

    def test_a_shifted_set_is_not_trusted_as_the_replacement(self):
        """Someone deleted an earlier sticker while we replaced ours.

        Position 1 now holds "c", an unrelated emoji. Reading live[pos] blindly
        repointed btc AND wbtc at it, silently and permanently.
        """
        before = self.mapping()
        ok, _ = self._fix(["a", OLD_CID, "c"], [NEW_CID, "c"])
        self.assertFalse(ok)
        self.assertEqual(self.mapping(), before)

    def test_an_unchanged_set_is_not_trusted_either(self):
        before = self.mapping()
        ok, _ = self._fix(["a", OLD_CID, "c"], ["a", OLD_CID, "c"])
        self.assertFalse(ok)
        self.assertEqual(self.mapping(), before)
        self.assertIsNone(self.intent(),
                          "nothing was applied, so nothing is pending")

    def test_a_replacement_whose_image_is_not_ours_is_refused(self):
        """The cid list is exactly what a clean replacement looks like.

        Structure is not identity: position 1 carries somebody else's art, and
        trusting the list shape repoints btc AND wbtc onto it forever.
        """
        before = self.mapping()
        ok, _ = self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"],
                          images={f"fid-{NEW_CID}": _noise_png_bytes("stranger")})
        self.assertFalse(ok)
        self.assertEqual(self.mapping(), before)

    def test_a_lost_confirmation_read_leaves_a_recoverable_intent(self):
        """Telegram applied the replacement; the postcondition read never came.

        Without a persisted intent the map keeps OLD_CID -- an id that is gone
        -- and no later run can find it, so the ticker is stranded for good.
        """
        before = self.mapping()
        ok, session = self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"],
                                lose_confirm=True)
        self.assertFalse(ok)
        self.assertEqual(self.mapping(), before, "nothing may be guessed here")
        intent = self.intent()
        self.assertIsNotNone(intent, "the pending replacement must be recorded")
        self.assertEqual(intent["operation"], "replace")
        self.assertEqual(intent["key"], "btc")
        self.assertEqual(intent["old_cid"], OLD_CID)
        self.assertEqual(intent["set_name"], session.calls[0][1]["name"])
        self.assertEqual(intent["set_index"], 1)
        self.assertEqual(intent["before"], ["a", OLD_CID, "c"])

    def test_the_next_run_recovers_the_replacement_from_the_intent(self):
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"], lose_confirm=True)
        # Restart: the pack is in its post-replacement state and readable again.
        tg, _ = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"])
        self.assertTrue(self.mod.reconcile_intent(tg, self.map_path))
        self.assertEqual(self.mapping(), {"btc": NEW_CID, "wbtc": NEW_CID,
                                          "eth": "cid-eth"})
        self.assertIsNone(self.intent(), "a recovered intent must be cleared")

    def test_recovery_refuses_when_the_live_image_is_not_ours(self):
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"], lose_confirm=True)
        before = self.mapping()
        tg, _ = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"],
                              images={f"fid-{NEW_CID}": _noise_png_bytes("stranger")})
        self.assertFalse(self.mod.reconcile_intent(tg, self.map_path))
        self.assertEqual(self.mapping(), before)
        self.assertIsNotNone(self.intent(),
                             "an unresolved intent must survive for review")

    def test_recovery_is_idempotent_after_the_map_was_already_repointed(self):
        """Crashed after the map write, before the intent was cleared."""
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"], lose_confirm=True)
        self.mod.repoint(self.map_path, OLD_CID, NEW_CID)
        tg, _ = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"])
        self.assertTrue(self.mod.reconcile_intent(tg, self.map_path))
        self.assertEqual(self.mapping(), {"btc": NEW_CID, "wbtc": NEW_CID,
                                          "eth": "cid-eth"})
        self.assertIsNone(self.intent())

    def test_an_unreadable_set_leaves_the_intent_for_the_run_after(self):
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"], lose_confirm=True)
        tg, session = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"])
        session.lose_confirm = session.replaced = True   # every read fails
        self.assertFalse(self.mod.reconcile_intent(tg, self.map_path))
        self.assertIsNotNone(self.intent())

    def test_an_intent_cannot_be_reconciled_into_a_different_map(self):
        """7: the intent file has one fixed path; --map is chosen per run.

        A crash under ``--map A`` and a restart under ``--map B`` repointed the
        pending replacement inside B -- a file that never held the old id --
        while A kept naming a sticker that no longer exists.
        """
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"], lose_confirm=True)
        other = self.tmp / "other_map.json"
        other.write_text(json.dumps({"btc": OLD_CID}), encoding="utf-8")
        tg, _ = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"])

        self.assertFalse(self.mod.reconcile_intent(tg, other))
        self.assertEqual(json.loads(other.read_text("utf-8")), {"btc": OLD_CID},
                         "a map this replacement never touched was rewritten")
        self.assertIsNotNone(self.intent(),
                             "the intent still belongs to the original map")
        # ...and it is still recoverable into the map it actually names.
        self.assertTrue(self.mod.reconcile_intent(tg, self.map_path))
        self.assertEqual(self.mapping()["btc"], NEW_CID)

    def test_an_intent_is_bound_to_its_state_file_too(self):
        state_a, state_b = self.tmp / "state_a.json", self.tmp / "state_b.json"
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"],
                  state_path=state_a, lose_confirm=True)
        tg, _ = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"])
        before = self.mapping()

        self.assertFalse(self.mod.reconcile_intent(tg, self.map_path, state_b))
        self.assertEqual(self.mapping(), before)
        self.assertTrue(self.mod.reconcile_intent(tg, self.map_path, state_a))
        self.assertEqual(self.mapping()["btc"], NEW_CID)

    def test_an_intent_with_no_recorded_target_is_refused(self):
        """An older intent cannot prove which map it belongs to: fail closed."""
        self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"], lose_confirm=True)
        path = self.mod._intent_path()
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy.pop("map_target"), legacy.pop("state_target")
        bp.write_json_atomic(path, legacy)
        tg, _ = self._session(["a", NEW_CID, "c"], ["a", NEW_CID, "c"])
        before = self.mapping()

        self.assertFalse(self.mod.reconcile_intent(tg, self.map_path))
        self.assertEqual(self.mapping(), before)
        self.assertIsNotNone(self.intent(), "an unresolved intent must survive")

    def test_a_map_writer_cannot_repoint_the_ticker_mid_replacement(self):
        """8: the id was chosen from the map with no lock held.

        alias_map / enhance_map / remap_ids --apply rewrite ticker_to_id.json
        under canonical_map_lock() alone, so one of them landing between the
        read and the repoint left --fix replacing the live sticker for an id
        the map no longer names: the sticker is destroyed and the repoint,
        which looks entries up by the OLD id, finds nothing to move.

        btc alone is on OLD_CID here so the race leaves nothing at all to
        update -- the honest outcomes are "the replacement is what the map now
        names" or "the run refused".
        """
        other = "cid-written-by-the-other-tool"
        self.map_path.write_text(json.dumps({"btc": OLD_CID}), encoding="utf-8")
        real = bp.canonical_map_lock

        @contextlib.contextmanager
        def racing():
            """The map-only writer gets in the instant this run takes the lock."""
            with real() as beat:
                mp = json.loads(self.map_path.read_text("utf-8"))
                mp["btc"] = other
                bp.write_json_atomic(self.map_path, mp)
                yield beat

        with mock.patch.object(self.mod, "canonical_map_lock", racing):
            ok, session = self._fix(["a", OLD_CID, "c"], ["a", NEW_CID, "c"])

        if session.method("replaceStickerInSet"):
            self.assertTrue(ok)
            self.assertEqual(self.mapping()["btc"], NEW_CID,
                             "a live sticker was replaced for an id the map "
                             "had already been repointed away from")
        else:
            self.assertFalse(ok)
            self.assertEqual(self.mapping(), {"btc": other})

    def test_a_busy_map_lock_stops_the_fix_before_it_touches_the_pack(self):
        """No live replacement may happen that cannot then be repointed.

        The lock has to be taken BEFORE the id is read, so a map editor already
        holding it stops this run while the packs are still untouched. Taking
        it only at the repoint meant the sticker was long gone by the time the
        run discovered it could not record what it had done.
        """
        before = self.mapping()
        tg, session = self._session(["a", OLD_CID, "c"], ["a", NEW_CID, "c"])
        with bp.canonical_map_lock(), self.assertRaises(bp.LockBusy):
            self.mod.fix_one(tg, 42, self.sets, self.map_path, self.emoji, "btc")
        self.assertEqual(session.method("replaceStickerInSet"), [],
                         "a sticker was replaced while the map was unwritable")
        self.assertEqual(self.mapping(), before)
        self.assertIsNone(self.intent())

    def test_fix_locks_on_the_pack_family_not_on_this_script(self):
        # --fix REPLACES stickers in the same gvcryptoemoji* sets the coin
        # fetchers append to. A lock named after this file was a different name
        # from theirs, so the exclusion it claimed never actually held.
        self.assertEqual(self.mod.SET_BASE, "gvcryptoemoji")
        self.assertEqual(self.mod.PACK_LOCK,
                         bp.pack_family_lock_path(self.mod.SET_BASE))

    def test_verified_new_cid_rules(self):
        v = self.mod.verified_new_cid
        self.assertEqual(v(["a", OLD_CID], ["a", NEW_CID], 1), NEW_CID)
        self.assertIsNone(v(["a", OLD_CID], [NEW_CID], 1))          # length drift
        self.assertIsNone(v(["a", OLD_CID], ["z", NEW_CID], 1))     # neighbour moved
        self.assertIsNone(v(["a", OLD_CID], ["a", "a"], 1))         # id already present
        self.assertIsNone(v(["a", OLD_CID], ["a", OLD_CID], 1))     # nothing changed


class VerifyLogosMainContracts(unittest.TestCase):
    """--fix exit code and configuration errors."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_standalone(ROOT / "coins" / "verify_logos.py",
                                   "coins_verify_logos_main")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.emoji = self.tmp / "emoji"
        self.emoji.mkdir()
        (self.tmp / "state.json").write_text(json.dumps(
            {"sets": [{"name": "gvcryptoemoji1_by_bot", "index": 1}]}),
            encoding="utf-8")
        (self.tmp / "map.json").write_text(json.dumps({"sol": OLD_CID}),
                                           encoding="utf-8")
        self.patches = [
            mock.patch.object(self.mod, "setup_logging", lambda *a, **k: None),
            mock.patch.object(self.mod, "load_env", lambda *a, **k: None),
            # ROOT anchors the in-flight intent file: keep it out of the repo.
            mock.patch.object(self.mod, "ROOT", self.tmp),
            mock.patch.object(self.mod, "PACK_LOCK", self.tmp / "pack.lock"),
            # The requested ticker is simply not in the market data: fix_one
            # returns False without touching Telegram.
            mock.patch.object(self.mod, "fetch_markets", lambda top: []),
            mock.patch.object(self.mod.time, "sleep", lambda *_a: None),
        ]
        for pt in self.patches:
            pt.start()

    def tearDown(self):
        for pt in reversed(self.patches):
            pt.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _main(self, env: dict, *extra) -> int:
        argv = ["verify_logos.py", "--emoji-dir", str(self.emoji),
                "--map", str(self.tmp / "map.json"),
                "--state", str(self.tmp / "state.json"), *extra]
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stderr(io.StringIO()):
            return self.mod.main()

    def test_the_set_list_is_read_after_the_pack_lock_not_before(self):
        """A rebuild finishing during the wait invalidates the pre-lock snapshot.

        --state is read to build the set list, then the run waits for
        PACK_LOCK. A rebuild that completes in that window deletes the old packs
        and writes a new set list, and the map already points at the new family
        -- so acting on the snapshot searches packs that no longer exist and
        reports the ticker as missing instead of repairing it.
        """
        state = self.tmp / "state.json"
        state.write_text(json.dumps(
            {"sets": [{"name": "old_pack_1", "index": 1}]}), encoding="utf-8")
        (self.tmp / "map.json").write_text(json.dumps({"btc": OLD_CID}),
                                           encoding="utf-8")
        seen: list[list[str]] = []

        real_lock = self.mod.exclusive_lock

        @contextlib.contextmanager
        def rebuild_finishes_while_we_wait(path, **kw):
            # The rebuild lands its new state exactly while this run blocks.
            state.write_text(json.dumps(
                {"sets": [{"name": "rebuilt_pack_1", "index": 1}]}),
                encoding="utf-8")
            with real_lock(path, **kw) as beat:
                yield beat

        def record(tg, uid, sets, *a, **kw):
            seen.append([s["name"] for s in sets])
            return False

        with mock.patch.object(self.mod, "exclusive_lock",
                               rebuild_finishes_while_we_wait), \
                mock.patch.object(self.mod, "fix_one", record), \
                mock.patch.object(self.mod, "reconcile_intent",
                                  lambda *a, **k: True):
            self._main({"TELEGRAM_BOT_TOKEN": "t", "PACK_OWNER_USER_ID": "7"},
                       "--fix", "--only", "btc")

        self.assertEqual(seen, [["rebuilt_pack_1"]],
                         "the fix ran against the set list captured BEFORE the "
                         "lock; those packs no longer exist")

    def test_a_requested_fix_that_failed_exits_nonzero(self):
        code = self._main({"TELEGRAM_BOT_TOKEN": "t", "PACK_OWNER_USER_ID": "7"},
                          "--fix", "--only", "sol")
        # Nothing was repaired although a repair was explicitly requested.
        self.assertEqual(code, EXIT_FAILED)

    def test_missing_owner_id_is_a_usage_error_not_a_traceback(self):
        env = {"TELEGRAM_BOT_TOKEN": "t"}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PACK_OWNER_USER_ID", None)
            self.assertEqual(
                self._main(env, "--fix", "--only", "sol"), EXIT_USAGE)

    def test_non_numeric_owner_id_is_a_usage_error(self):
        code = self._main({"TELEGRAM_BOT_TOKEN": "t",
                           "PACK_OWNER_USER_ID": "not-a-number"},
                          "--fix", "--only", "sol")
        self.assertEqual(code, EXIT_USAGE)

    def test_missing_token_is_a_usage_error(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            self.assertEqual(
                self._main({"PACK_OWNER_USER_ID": "7"}, "--fix", "--only", "sol"),
                EXIT_USAGE)

    def test_unreadable_state_file_is_a_usage_error(self):
        (self.tmp / "state.json").write_text("{not json", encoding="utf-8")
        code = self._main({"TELEGRAM_BOT_TOKEN": "t", "PACK_OWNER_USER_ID": "7"},
                          "--fix", "--only", "sol")
        self.assertEqual(code, EXIT_USAGE)

    def test_an_unresolved_intent_blocks_every_replacement(self):
        """The next fix would overwrite the only record of the pending one."""
        attempted: list[tuple] = []
        with mock.patch.object(self.mod, "reconcile_intent", lambda *a: False), \
                mock.patch.object(self.mod, "fix_one",
                                  lambda *a: attempted.append(a) or True):
            code = self._main({"TELEGRAM_BOT_TOKEN": "t",
                               "PACK_OWNER_USER_ID": "7"},
                              "--fix", "--only", "sol")
        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(attempted, [], "no pack may be mutated first")

    def test_a_clear_intent_lets_the_run_proceed(self):
        seen: list[tuple] = []
        with mock.patch.object(self.mod, "reconcile_intent", lambda *a: True), \
                mock.patch.object(self.mod, "fix_one",
                                  lambda *a: seen.append(a) or True):
            code = self._main({"TELEGRAM_BOT_TOKEN": "t",
                               "PACK_OWNER_USER_ID": "7"},
                              "--fix", "--only", "sol")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
