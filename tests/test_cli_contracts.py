"""Entry-point contracts: exit codes, argument validation, resource bounds.

Each test here locks down a failure that a run reported as success (or an
unbounded read that a hostile input could turn into an OOM). They exercise the
real entry points with deterministic fakes -- no network, no sleeping, no
writes outside a temp directory.
"""

from __future__ import annotations

import contextlib
import gzip
import importlib.util
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


if __name__ == "__main__":
    unittest.main()
