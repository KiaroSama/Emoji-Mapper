"""coins/fetch_logos: nothing on disk is an image until it has been opened.

The download path and the PNG_DIR sweep both hand files onward -- to the next
run's cache and to keywords.csv, which is what a pack is built from. An error
page saved as ``<ticker>.png`` looks exactly like a logo to both of them, so
every one of these tests is about a file that was trusted for its NAME.

The HTTP layer underneath is stubbed out here; its own contracts live in
`test_coin_http`.
"""

from __future__ import annotations

import contextlib
import csv
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from tests._cli_fixtures import _load_standalone  # noqa: E402

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
            self.assertEqual(self.mod.main([]), 0)

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
            self.mod.main([])

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
            self.assertEqual(self.mod.main([]), 0)

        rows = self._keywords_rows()
        self.assertIn("ok", rows)
        self.assertNotIn("junk", rows)
        self.assertNotIn("cut", rows)


if __name__ == "__main__":
    unittest.main()
