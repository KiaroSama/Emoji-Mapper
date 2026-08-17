"""The non-coin entry points: exit codes, argument validation, resource bounds.

`fetch_pack`, `make_emoji_pngs`, `panel` and `emojikit.logsetup`. Each test here
locks down a failure that a run reported as success (or an unbounded read that a
hostile input could turn into an OOM). They exercise the real entry points with
deterministic fakes -- no network, no sleeping, no writes outside a temp
directory.

The coin tools' own contracts live in `test_coin_cli_args`, `test_coin_http`,
`test_coin_logo_cache`, `test_coin_ticker_map` and `test_verify_logos`.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
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

from PIL import Image  # noqa: E402

import fetch_pack  # noqa: E402
import make_emoji_pngs as m  # noqa: E402
import panel as p  # noqa: E402
from build_pack import EXIT_FAILED, EXIT_OK, EXIT_USAGE  # noqa: E402
from emojikit.media import TGS_MAX_UNPACKED  # noqa: E402
from tests._cli_fixtures import DeadTelegram, _load_standalone  # noqa: E402

RED = (240, 20, 20, 255)
EMPTY_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"></svg>'


# --------------------------------------------------------------------------- #
# fetch_pack: pack-level failures and --limit validation
# --------------------------------------------------------------------------- #
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


if __name__ == "__main__":
    unittest.main()
