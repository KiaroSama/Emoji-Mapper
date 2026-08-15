"""Regression tests: ``fetch_pack --limit N`` must deliver N NEW items.

The option is documented as a cap on new catalog items. Counting already-known
stickers against it meant that a re-run with ``--limit 1`` over a pack whose
first sticker was already ingested downloaded nothing at all and stopped -- so
the option could never make progress through a partially-fetched pack.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import fetch_pack  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402


def _png_bytes(i: int) -> bytes:
    """A distinct 100x100 PNG per index, so each yields its own content key."""
    im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    for x in range(i, i + 30):
        for y in range(i, i + 30):
            im.putpixel((x, y), (10 * i % 256, 60, 200, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class FakeTelegram:
    """In-memory Bot API: one pack of static stickers, no network.

    ``file_id`` doubles as the index of the PNG body a download replays.
    """

    def __init__(self, n: int):
        self.stickers = [{"file_id": str(i), "file_unique_id": f"FU-{i}",
                          "emoji": "😀"} for i in range(n)]
        self.downloaded: list[str] = []

    def get_sticker_set(self, name):
        return {"title": name, "stickers": list(self.stickers)}

    def download_file(self, file_id, dest: Path):
        self.downloaded.append(file_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_png_bytes(int(file_id)))


class FetchPackLimitTest(unittest.TestCase):
    def _fetch(self, tg: FakeTelegram, data: Path, limit: int,
               known: tuple[int, ...] = ()) -> dict[str, int]:
        """Ingest with ``known`` sticker indexes already in the catalog."""
        tmp = data / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        with Catalog(data / "catalog.db") as cat:
            for i in known:
                p = data / "media" / "static" / f"seed{i}.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(_png_bytes(i))
                cat.add(content_key=f"s:seed{i:030d}", fmt="static", file_path=p,
                        file_unique_id=tg.stickers[i]["file_unique_id"])
            return fetch_pack.fetch_one(tg, cat, "pack", data, tmp, limit)

    def test_known_leading_stickers_do_not_consume_the_limit(self):
        # The bug: first sticker already in the catalog + --limit 1 == zero work.
        with tempfile.TemporaryDirectory() as t:
            tg = FakeTelegram(4)
            counts = self._fetch(tg, Path(t), limit=1, known=(0, 1))
            self.assertEqual(counts["new"], 1)          # one genuinely NEW item
            self.assertEqual(counts["dedup"], 2)        # the two known ones
            self.assertEqual(tg.downloaded, ["2"])      # scanned past 0 and 1

    def test_limit_still_stops_the_loop(self):
        with tempfile.TemporaryDirectory() as t:
            tg = FakeTelegram(5)
            counts = self._fetch(tg, Path(t), limit=2)
            self.assertEqual(counts["new"], 2)
            self.assertEqual(tg.downloaded, ["0", "1"])  # sticker 2 never fetched

    def test_zero_limit_fetches_everything(self):
        with tempfile.TemporaryDirectory() as t:
            tg = FakeTelegram(3)
            counts = self._fetch(tg, Path(t), limit=0)
            self.assertEqual(counts["new"], 3)
            self.assertEqual(tg.downloaded, ["0", "1", "2"])


if __name__ == "__main__":
    unittest.main()
