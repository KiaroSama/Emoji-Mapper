"""Shared fakes for the coin-provider test modules.

Leading underscore is load-bearing: ``discover -p "test_*.py"`` must not
collect this as a suite. Nothing here owns a ``test_*`` method, so
importing it into several modules cannot inflate the count.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import telegram_api as tg_api  # noqa: E402

SET = "gvcryptoemoji1_by_bot"


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _gradient(reverse: bool = False) -> Image.Image:
    """Opaque left-to-right (or right-to-left) grey ramp.

    Two ramps of opposite direction are perceptually as far apart as a dHash can
    report, which is what makes the content match in the tests unambiguous.
    """
    img = Image.new("RGBA", (100, 100))
    px = img.load()
    for x in range(100):
        v = (99 - x) * 2 if reverse else x * 2
        for y in range(100):
            px[x, y] = (v, v, v, 255)
    return img


class FakeTelegram:
    """Deterministic stand-in for build_pack.Telegram."""

    def __init__(self, existing: int = 2):
        self.sets: dict[str, list[dict]] = {SET: []}
        self.blobs: dict[str, bytes] = {}
        self.adds: list[tuple[str, str, int | None]] = []
        self.creates: list[str] = []
        self.fail_add: Exception | None = None
        self.after_add = None          # hook: simulates a concurrent writer
        self.unreadable: set[str] = set()   # sets Telegram will not talk about
        self._n = 0
        for _ in range(existing):
            self.append(SET, _png_bytes(_gradient(reverse=True)))

    # ----- fake wire ------------------------------------------------------ #
    def append(self, name: str, data: bytes) -> dict:
        self._n += 1
        st = {"custom_emoji_id": f"c{self._n}", "file_id": f"f{self._n}",
              "file_unique_id": f"u{self._n}"}
        self.blobs[st["file_id"]] = data
        self.sets.setdefault(name, []).append(st)
        return st

    # ----- Telegram surface used by publish_logos ------------------------- #
    def get_me(self) -> dict:
        return {"username": "bot"}

    def get_sticker_set(self, name: str) -> dict:
        if name in self.unreadable:
            raise RuntimeError("getStickerSet failed after 5 attempts")
        if name not in self.sets:
            raise RuntimeError("getStickerSet failed: STICKERSET_INVALID")
        return {"stickers": [dict(s) for s in self.sets[name]]}

    def probe_set_state(self, name: str):
        if name in self.unreadable:
            return tg_api.SetState.UNKNOWN, None
        if name not in self.sets:
            return tg_api.SetState.MISSING, None
        return tg_api.SetState.EXISTS, {"stickers": [dict(s) for s in self.sets[name]]}

    def add_sticker(self, user_id, name, png, emoji, keywords, *,
                    expected_before=None):
        self.adds.append((name, Path(png).stem, expected_before))
        if self.fail_add:
            raise self.fail_add
        self.append(name, Path(png).read_bytes())
        if self.after_add:
            self.after_add(self, name)

    def create_set(self, user_id, name, title, png, emoji, keywords):
        self.creates.append(name)
        self.sets.setdefault(name, [])
        self.append(name, Path(png).read_bytes())

    def download_file(self, file_id: str, dest: Path) -> Path:
        Path(dest).write_bytes(self.blobs[file_id])
        return dest
