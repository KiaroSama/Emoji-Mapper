"""Shared fakes for the build_collection test modules.

Leading underscore is load-bearing: ``unittest discover -p "test_*.py"`` would
otherwise try to run this as a suite. ``FakeTG`` carries no test methods and no
base class, so importing it into several modules cannot inflate the count --
unlike a fixture that owns ``test_*`` methods, which multiplies with every
importer.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import telegram_api as tg_api  # noqa: E402
from telegram_api import (SetState)  # noqa: E402

def _make_png(path: Path, color=(200, 30, 30, 255)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    for x in range(20, 80):
        for y in range(20, 80):
            im.putpixel((x, y), color)
    im.save(path, "PNG")


class FakeTG:
    """In-memory Telegram with the tri-state probe the real client exposes."""

    def __init__(self, sets=None, unknown=(), fail_after=None):
        self.sets: dict[str, list[dict]] = dict(sets or {})
        self.unknown = set(unknown)          # names whose live state is unknown
        self.fail_after = fail_after         # uploads accepted before it breaks
        self.uploaded: list[str] = []
        self.sent: list[str] = []
        # file_unique_id -> the bytes that were uploaded for it, so a later
        # download can prove the live sticker is the one we sent.
        self.bodies: dict[str, bytes] = {}
        # Which stickers were actually fetched: content attribution is the
        # expensive route, so its bound has to be assertable.
        self.downloaded: list[str] = []
        # file_unique_ids whose fetch FAILS -- "I could not look", as distinct
        # from "I looked and it is not ours".
        self.undownloadable: set[str] = set()
        # preflight: every file it validated, and the names it must refuse
        self.checked: list[str] = []
        self.refuse: set[str] = set()

    # ----- preflight probe (uploadStickerFile: validates, touches no set) --- #
    def check_uploadable(self, user_id, path, fmt):
        self.checked.append(path.name)
        if path.name in self.refuse:
            raise tg_api.BotApiError(
                "uploadStickerFile failed: Bad Request: wrong file type")

    def probe_set_state(self, name):
        if name in self.unknown:
            return SetState.UNKNOWN, None
        if name in self.sets:
            return SetState.EXISTS, {"stickers": list(self.sets[name])}
        return SetState.MISSING, None

    def get_sticker_set(self, name):
        if name not in self.sets:
            raise RuntimeError("getStickerSet failed: STICKERSET_INVALID")
        return {"stickers": list(self.sets[name])}

    # ----- publishing side (the uploaded copy gets its own file_unique_id) --- #
    def get_me(self):
        return {"username": "YourEmojiBot"}

    # Telegram re-encodes on upload, so the live copy's file_unique_id is a
    # FRESH one the catalog has never seen (``fuid_prefix``). The fixture
    # pre-records "UP-item<i>", so a fake that returns those ids would hide the
    # very window these tests are about.
    fuid_prefix = "UP-"

    def _new(self, name: str, path) -> dict:
        if self.fail_after is not None and len(self.uploaded) >= self.fail_after:
            raise RuntimeError("BAD_REQUEST: STICKER_PNG_DIMENSIONS")
        stem = Path(path).stem
        self.uploaded.append(stem)
        st = _sticker(f"{self.fuid_prefix}{stem}",
                      f"{name}-{len(self.sets.get(name, []))}")
        # Remember what was uploaded: the publisher now PROVES the new sticker
        # is ours by downloading it and hashing its content, so a fake that
        # cannot serve the bytes back makes every upload "unidentifiable".
        self.bodies[st["file_unique_id"]] = Path(path).read_bytes()
        return st

    def download_file(self, file_id, dest):
        """Serve the sticker's bytes, as Telegram would.

        A sticker nobody uploaded through this fake is still a REAL, fetchable
        image -- it just is not in our catalog. Raising here instead would make
        "someone else's sticker" and "the download failed" the same event, and
        those are the two cases the publisher must tell apart: the first is a
        proven negative, the second is no answer at all. Use ``undownloadable``
        for the genuine failure.
        """
        fuid = str(file_id)[2:] if str(file_id).startswith("f-") else str(file_id)
        self.downloaded.append(fuid)
        if fuid in self.undownloadable:
            raise RuntimeError(f"download failed: {file_id}")
        body = self.bodies.get(fuid)
        if body is None:
            # Deterministic per-fuid art so two foreign stickers never collide
            # onto one content key, which would read as a duplicate of ours.
            seed = sum(fuid.encode()) % 200
            foreign = Path(dest).with_suffix(".foreign.png")
            _make_png(foreign, color=(seed, 255 - seed, 90, 255))
            body = foreign.read_bytes()
            foreign.unlink(missing_ok=True)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(body)
        return dest

    def create_emoji_set(self, user_id, name, title, path, fmt, emojis, keywords):
        self.sets[name] = [self._new(name, path)]

    def add_emoji(self, user_id, name, path, fmt, emojis, keywords, *,
                  expected_before=None):
        self.sets[name].append(self._new(name, path))

    def send_message(self, chat_id, text, *, disable_preview=False):
        self.sent.append(text)


def _sticker(fuid: str, cid: str) -> dict:
    # file_id is what _resolve_sticker_key needs before it will download at all.
    return {"file_unique_id": fuid, "custom_emoji_id": cid, "file_id": f"f-{fuid}"}

import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402
import build_collection as bc  # noqa: E402
from emojikit import identity  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402


SET = "pks1_by_YourEmojiBot"
SET2 = "pks2_by_YourEmojiBot"


def _main(*argv: str) -> int:
    """Run build_collection.main without touching .env or the log directory."""
    with mock.patch.object(bc, "load_env", lambda: None), \
            mock.patch.object(bc, "setup_logging", lambda *a, **k: None):
        return bc.main(list(argv))


class DownloadingTG(FakeTG):
    """FakeTG that also serves downloads, so CONTENT attribution really runs."""

    def __init__(self, *a, error: str | None = None, **kw):
        super().__init__(*a, **kw)
        self.error = error
        self.downloads = 0

    def download_file(self, file_id, dest):
        self.downloads += 1
        if self.error:
            raise RuntimeError(self.error)
        Path(dest).write_bytes(b"whatever telegram returned")
        return dest


class _CatalogFixture(unittest.TestCase):
    """Two static items whose PUBLISHED copies are known as UP-item<i>."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.keys = []
        with Catalog(self.data / "catalog.db") as cat:
            for i in range(2):
                p = self.data / "media" / "static" / f"item{i}.png"
                _make_png(p, color=(10, 60 * (i + 1), 200, 255))
                # The REAL content key, not a synthetic one: publishing now
                # attributes a live sticker by downloading it and hashing the
                # pixels, so a made-up key could never match and every upload
                # would look unidentifiable.
                key = identity.content_key(p, "static")
                cat.add(content_key=key, fmt="static", file_path=p,
                        emojis=["\U0001F600"], keywords=[f"item{i}"])
                cat.record_file_unique_id(f"UP-item{i}", key)
                self.keys.append(key)

    def tearDown(self):
        self.tmp.cleanup()

    def _state_set(self, keys=None, live=2, logo=False) -> dict:
        return {"fmt": "static", "index": 1, "name": SET, "title": "Pack 1",
                "live": live, "logo": logo,
                "keys": list(self.keys if keys is None else keys)}

    def _add_item(self, i: int) -> str:
        """Catalogue one more static item, as a later curate pass would."""
        p = self.data / "media" / "static" / f"item{i}.png"
        _make_png(p, color=(10, 30 * (i + 1), 90, 255))
        key = identity.content_key(p, "static")
        with Catalog(self.data / "catalog.db") as cat:
            cat.add(content_key=key, fmt="static", file_path=p,
                    emojis=["\U0001F600"], keywords=[f"item{i}"])
            cat.record_file_unique_id(f"UP-item{i}", key)
        return key

    def _read_back(self, *keys: str) -> None:
        """Pretend a previous run already stored these keys' custom_emoji_ids.

        That is the moment a position's live identity becomes known, and it is
        what makes an unknown identity there proof of a replacement.
        """
        with Catalog(self.data / "catalog.db") as cat:
            for i, key in enumerate(keys):
                cat.mark_uploaded(key, f"c{i}", base="pk", set_name=SET)
