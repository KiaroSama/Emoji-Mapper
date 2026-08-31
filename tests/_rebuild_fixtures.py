"""Shared fakes for the coins/rebuild_dedup test modules.

`test_rebuild_dedup_state` (the pack-mutation walk) and
`test_rebuild_dedup_map` (the canonical map phase) both need the same fake
Telegram and the same temp-directory redirection. One copy, because a
duplicated fake drifts apart from the thing it stands in for.

Not named `test_*` on purpose -- `unittest discover -p "test_*.py"` would
otherwise try to run it as a test module.
"""

from __future__ import annotations

import io
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import telegram_api as tg_api  # noqa: E402
import packstate as ps  # noqa: E402
from coins import rebuild_dedup as rd  # noqa: E402


def _image(key: str) -> Image.Image:
    """Deterministic per-key noise; two different keys never look alike.

    Flat colours are useless for identity: a dHash compares neighbouring
    pixels, so every solid image hashes to the same value and "is this sticker
    our upload?" would always answer yes.
    """
    rnd = random.Random(key)
    img = Image.new("RGBA", (100, 100))
    px = img.load()
    for x in range(100):
        for y in range(100):
            v = rnd.randrange(256)
            px[x, y] = (v, v, v, 255)
    return img


def _png_bytes(key: str) -> bytes:
    buf = io.BytesIO()
    _image(key).save(buf, "PNG")
    return buf.getvalue()


def _png(path: Path, key: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _image(key or path.stem).save(path, "PNG")


class FakeTelegram:
    """Records mutations; every live answer is scripted per test.

    Sets carry real image bytes, because resume decides what landed by
    comparing sticker CONTENT with the PNG that was sent.
    """

    def __init__(self, live: dict[str, int] | None = None):
        self.images: dict[str, list[bytes]] = {}   # set name -> sticker images
        self.live: dict[str, int] = {}             # set name -> live count
        self.unknown: set[str] = set()        # sets whose live state is unreadable
        self.missing: set[str] = set()        # sets that are definitively gone
        self.add_calls: list[tuple] = []
        self.create_calls: list[tuple] = []
        self.deleted: list[str] = []
        self.messages: list[str] = []
        self.message_retries: list[int | None] = []
        self.message_previews: list[bool | None] = []
        self.add_error: BaseException | None = None
        self.create_error: BaseException | None = None
        self.delete_error: BaseException | None = None
        for name, count in (live or {}).items():
            for i in range(count):
                self.append(name, _png_bytes(f"{name}-seed{i}"))

    # --- fake wire ------------------------------------------------------ #
    def append(self, name: str, data: bytes) -> None:
        """Put a sticker into a set outside our add path (a manual edit)."""
        self.images.setdefault(name, []).append(data)
        self.live[name] = len(self.images[name])

    # --- live state ---------------------------------------------------- #
    def probe_set_state(self, name: str):
        if name in self.unknown:
            return tg_api.SetState.UNKNOWN, None
        if name in self.missing or name not in self.live:
            return tg_api.SetState.MISSING, None
        return tg_api.SetState.EXISTS, {
            "stickers": [{"custom_emoji_id": f"{name}-{i}",
                          "file_id": f"{name}#{i}",
                          "file_unique_id": f"{name}#{i}"}
                         for i in range(self.live[name])]}

    def probe_sticker_set(self, name: str):
        state, sset = self.probe_set_state(name)
        return state is not tg_api.SetState.UNKNOWN, sset

    def live_count_strict(self, name: str) -> int:
        state, sset = self.probe_set_state(name)
        if state is tg_api.SetState.UNKNOWN:
            raise tg_api.LiveStateUnknown(f"live state of {name} is unknown")
        return len(sset.get("stickers", [])) if sset else 0

    def download_file(self, file_id: str, dest: Path) -> Path:
        name, _, index = str(file_id).rpartition("#")
        Path(dest).write_bytes(self.images[name][int(index)])
        return Path(dest)

    # The REAL comparator, not a stand-in: identity is the thing under test, so
    # a fake that answers it would be testing itself. It only needs
    # download_file, which this class provides, and returns None (unverifiable)
    # for anything it cannot read -- exactly like it does against Telegram.
    _sticker_matches = tg_api.Telegram._sticker_matches

    # --- mutations ------------------------------------------------------ #
    def add_sticker(self, user_id, name, png, emoji, kw, *, expected_before=None):
        self.add_calls.append((name, png.stem))
        if self.add_error:
            raise self.add_error
        self.append(name, Path(png).read_bytes())

    def create_set(self, user_id, name, title, png, emoji, kw):
        self.create_calls.append((name, png.stem))
        if self.create_error:
            raise self.create_error
        self.images[name] = []
        self.append(name, Path(png).read_bytes())

    # The REAL implementation, bound onto the fake rather than reimplemented.
    # It is what decides `retries=2` and the preview flag, and a fake copy of
    # it would keep passing after the real one changed -- which is exactly the
    # bug the retry test exists to catch.
    send_message = tg_api.Telegram.send_message

    def _call(self, method, *, data=None, **kw):
        if method == "deleteStickerSet":
            self.deleted.append(data["name"])
            if self.delete_error:
                raise self.delete_error
            self.live.pop(data["name"], None)
            self.images.pop(data["name"], None)
            self.missing.add(data["name"])
            return {}
        if method == "sendMessage":
            self.messages.append(data["text"])
            self.message_retries.append(kw.get("retries"))
            self.message_previews.append(data.get("disable_web_page_preview"))
            return {}
        if method == "getStickerSet":
            # Same contract as the real client: a missing set is an error here,
            # not an empty set.
            _, sset = self.probe_set_state(data["name"])
            if sset is None:
                raise RuntimeError("getStickerSet failed: STICKERSET_INVALID")
            return sset
        raise AssertionError(f"unexpected API call {method}")

    @property
    def mutations(self) -> int:
        return len(self.add_calls) + len(self.create_calls)


class RebuildCase(unittest.TestCase):
    """Redirects every module path at a temp directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.emoji = self.dir / "emoji"
        self.state = self.dir / "state.json"
        self.plan = self.dir / "plan.json"
        self.old_state = self.dir / "old_state.json"
        self.groups = self.dir / "shared_logo_groups.json"
        self.inv = self.dir / "inventory.md"
        self.inv.write_text("ticker: aaa\n", encoding="utf-8")
        # Every path the module writes to must point INSIDE the temp directory:
        # build_plan() rewrites the plan AND the shared-logo report, and those
        # are tracked project files.
        patches = [
            mock.patch.object(rd, "ROOT", self.dir),   # candidate map file
            mock.patch.object(rd, "EMOJI", self.emoji),
            mock.patch.object(rd, "STATE", self.state),
            mock.patch.object(rd, "PLAN", self.plan),
            mock.patch.object(rd, "OLD_STATE", self.old_state),
            mock.patch.object(rd, "GROUPS_REPORT", self.groups),
            mock.patch.object(rd, "INV", self.inv),
            mock.patch.object(rd, "OUT_INV", self.dir / "inventory.filled.md"),
            mock.patch.object(rd, "TICKER_IDS", self.dir / "ticker_to_id.json"),
            mock.patch.object(rd, "BACKUP_IDS", self.dir / "ticker_to_id.bak.json"),
            mock.patch.object(rd, "KEYWORDS_CSV", self.dir / "keywords.csv"),
            mock.patch.object(rd, "LOCK", self.dir / "state.json.lock"),
            mock.patch.object(rd, "USER_ID", 42),
            mock.patch.object(rd.time, "sleep", lambda s: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.tmp_cleanup = self.addCleanup(self.tmp.cleanup)

    def write_plan(self, reps: list[str]) -> None:
        ps.write_json_atomic(self.plan, [
            {"rep": r, "tickers": [r], "kw": r, "hash": r} for r in reps])
        for r in reps:
            _png(self.emoji / f"{r}.png")

    def write_state(self, **kw) -> dict:
        state = {"sets": [], "sent": [], "deleted_old": True, "final_sent": False,
                 "order": [], "cursor": 0, "in_flight": None}
        state.update(kw)
        ps.write_json_atomic(self.state, state)
        return state

    def saved(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def run_build(self, tg) -> object:
        """Run build(); return its exit code, or None if it did not stop.

        Asserting on the code last keeps the FIRST failure about what was
        mutated, which is the defect -- not about the exit code.
        """
        try:
            rd.build(tg, "bot")
        except SystemExit as exc:
            return exc.code
        return None
