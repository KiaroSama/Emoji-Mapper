"""Regression tests: duplicate emoji must never be uploaded twice or
downloaded twice.

Covers the three duplicate mechanisms:
  1. In-run: a network failure AFTER Telegram applied addStickerToSet must not
     re-send the call (``Telegram._call`` verified retry + bytes payloads).
  2. Cross-run: an upload that was applied but never recorded (crash or
     ambiguous network failure) must be attributed back from the LIVE set on
     the next publish instead of being uploaded again
     (``build_collection.reconcile_set``).
  3. Download side: publishing records each uploaded copy's file_unique_id,
     so a later fetch of our own pack is caught by the fast pre-dedup and
     never downloaded again.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from PIL import Image  # noqa: E402

import build_pack  # noqa: E402
import build_collection as bc  # noqa: E402
from build_pack import AmbiguousUploadError, Telegram  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402


def _make_png(path: Path, color=(200, 30, 30, 255)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    for x in range(20, 80):
        for y in range(20, 80):
            im.putpixel((x, y), color)
    im.save(path, "PNG")


# --------------------------------------------------------------------------- #
# 1. Telegram._call verified retry (network failure after server-side apply)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeServer:
    """requests.Session stand-in: the API server actually APPLIES an
    addStickerToSet before the network 'fails', so a blind client retry
    would duplicate the sticker."""

    def __init__(self, fail_first_add="after_apply"):
        self.sets: dict[str, list[dict]] = {"pack1": [{"i": 0}]}
        self.add_calls = 0
        self.fail_first_add = fail_first_add
        self.probe_fails = False
        self.last_files = None

    def post(self, url, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        if method == "getStickerSet":
            if self.probe_fails:
                raise requests.ConnectionError("probe network down")
            name = data["name"]
            if name in self.sets:
                return _Resp({"ok": True, "result": {"stickers": list(self.sets[name])}})
            return _Resp({"ok": False, "description": "STICKERSET_INVALID"})
        if method == "addStickerToSet":
            self.add_calls += 1
            self.last_files = files
            if self.add_calls == 1 and self.fail_first_add == "after_apply":
                self.sets[data["name"]].append({"i": len(self.sets[data["name"]])})
                raise requests.ReadTimeout("timeout after server applied the add")
            if self.add_calls == 1 and self.fail_first_add == "before_apply":
                raise requests.ConnectionError("connection dropped before apply")
            self.sets[data["name"]].append({"i": len(self.sets[data["name"]])})
            return _Resp({"ok": True, "result": True})
        raise AssertionError(f"unexpected method {method}")


@mock.patch("build_pack.time.sleep", lambda s: None)
class VerifiedRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.png = Path(self.tmp.name) / "e.png"
        _make_png(self.png)

    def tearDown(self):
        self.tmp.cleanup()

    def _tg(self, server) -> Telegram:
        tg = Telegram("TESTTOKEN")
        tg.s = server
        return tg

    def test_applied_then_network_error_is_not_resent(self):
        srv = _FakeServer(fail_first_add="after_apply")
        tg = self._tg(srv)
        tg.add_sticker(1, "pack1", self.png, "😀", "kw", expected_before=1)
        self.assertEqual(srv.add_calls, 1)              # never re-sent
        self.assertEqual(len(srv.sets["pack1"]), 2)     # exactly one new sticker

    def test_not_applied_network_error_is_retried_with_full_body(self):
        srv = _FakeServer(fail_first_add="before_apply")
        tg = self._tg(srv)
        tg.add_sticker(1, "pack1", self.png, "😀", "kw", expected_before=1)
        self.assertEqual(srv.add_calls, 2)              # safe retry happened
        self.assertEqual(len(srv.sets["pack1"]), 2)
        # bytes payload: the retried request re-sends the FULL file body (an
        # open file handle would be exhausted after the first attempt).
        body = srv.last_files["file0"][1]
        self.assertIsInstance(body, bytes)
        self.assertEqual(body, self.png.read_bytes())

    def test_unknown_live_state_raises_ambiguous(self):
        srv = _FakeServer(fail_first_add="after_apply")
        srv.probe_fails = True
        tg = self._tg(srv)
        with self.assertRaises(AmbiguousUploadError):
            tg.add_sticker(1, "pack1", self.png, "😀", "kw", expected_before=1)
        self.assertEqual(srv.add_calls, 1)              # ambiguity never re-sends

    def test_without_expected_before_keeps_legacy_retry(self):
        srv = _FakeServer(fail_first_add="before_apply")
        tg = self._tg(srv)
        tg.add_sticker(1, "pack1", self.png, "😀", "kw")  # no expected_before
        self.assertEqual(srv.add_calls, 2)


# --------------------------------------------------------------------------- #
# 2 + 3. publish_format: cross-run reconcile, ambiguous adds, fuid recording
# --------------------------------------------------------------------------- #
class FakeTelegram:
    """In-memory Telegram: live sets whose stickers carry file_unique_id and
    custom_emoji_id, like the real API. ``fuid_for`` maps an uploaded file
    path stem to the fuid its live copy gets."""

    def __init__(self, fuid_for=None, ambiguous_add_keys=()):
        self.sets: dict[str, list[dict]] = {}
        self.fuid_for = dict(fuid_for or {})
        self.ambiguous_add_keys = set(ambiguous_add_keys)
        self.add_calls: list[str] = []
        self.messages: list[str] = []

    def get_me(self):
        return {"username": "GodVerifyEmojiMapperbot"}

    def _sticker(self, name, path, fmt, emojis):
        i = len(self.sets[name])
        stem = Path(path).stem
        return {"emojis": list(emojis), "fmt": fmt,
                "custom_emoji_id": f"{name}-{i}",
                "file_unique_id": self.fuid_for.get(stem, f"FU-{stem}")}

    def create_emoji_set(self, user_id, name, title, path, fmt, emojis, keywords):
        self.sets[name] = []
        self.sets[name].append(self._sticker(name, path, fmt, emojis))

    def add_emoji(self, user_id, name, path, fmt, emojis, keywords, *,
                  expected_before=None):
        stem = Path(path).stem
        self.add_calls.append(stem)
        self.sets[name].append(self._sticker(name, path, fmt, emojis))
        if stem in self.ambiguous_add_keys:
            self.ambiguous_add_keys.discard(stem)
            raise AmbiguousUploadError("addStickerToSet: simulated ambiguity")

    def get_sticker_set(self, name):
        if name not in self.sets:
            raise RuntimeError("getStickerSet failed: STICKERSET_INVALID")
        return {"stickers": list(self.sets[name])}

    def send_message(self, user_id, text):
        self.messages.append(text)


class PublishDedupTest(unittest.TestCase):
    def _seed_catalog(self, data: Path, n=2):
        """n static items ingested with known source fuids SRC-item<i>."""
        keys = []
        with Catalog(data / "catalog.db") as cat:
            for i in range(n):
                p = data / "media" / "static" / f"item{i}.png"
                _make_png(p, color=(10, 40 * (i + 1) % 255, 200, 255))
                key = f"s:item{i:030d}"
                cat.add(content_key=key, fmt="static", file_path=p,
                        emojis=["😀"], keywords=[f"item{i}"],
                        file_unique_id=f"SRC-item{i}")
                keys.append(key)
        return keys

    def _publish(self, tg, data, keys, state):
        with Catalog(data / "catalog.db") as cat:
            bc.publish_format(tg, cat, fmt="static", plan_keys=keys,
                              base="pk", title="Pack", user_id=1,
                              default_emoji="😀", per_set=200, data_dir=data,
                              state=state, bot="GodVerifyEmojiMapperbot",
                              logo=None)

    def test_unrecorded_live_upload_is_reconciled_not_reuploaded(self):
        # Previous run uploaded item0 (it is LIVE, with the fuid of the source
        # sticker recorded in seen_files) but crashed before recording it.
        with tempfile.TemporaryDirectory() as t:
            data = Path(t)
            keys = self._seed_catalog(data)
            set_name = "pks1_by_GodVerifyEmojiMapperbot"
            tg = FakeTelegram(fuid_for={"item0": "SRC-item0"})
            tg.sets[set_name] = [
                {"emojis": ["😀"], "fmt": "static", "custom_emoji_id": "cid-old-0",
                 "file_unique_id": "SRC-item0"}]
            state = {"base": "pk", "sent": [], "sets": [
                {"fmt": "static", "index": 1, "name": set_name, "title": "Pack 1",
                 "live": 1, "logo": False, "keys": []}]}
            self._publish(tg, data, keys, state)
            live = tg.sets[set_name]
            self.assertEqual(len(live), 2)                      # item0 NOT re-uploaded
            self.assertEqual(tg.add_calls, ["item1"])           # only item1 was sent
            self.assertEqual(state["sets"][0]["keys"], keys)    # attributed + appended
            with Catalog(data / "catalog.db") as cat:
                self.assertTrue(cat.get(keys[0]).uploaded)
                self.assertEqual(cat.get(keys[0]).custom_emoji_id, "cid-old-0")

    def test_ambiguous_add_is_reconciled_in_run_without_duplicate(self):
        # The add for item1 IS applied by Telegram but the response is lost:
        # publish must reconcile, mark it uploaded, and never re-send it.
        with tempfile.TemporaryDirectory() as t:
            data = Path(t)
            keys = self._seed_catalog(data)
            tg = FakeTelegram(fuid_for={"item0": "UP-item0", "item1": "SRC-item1"},
                              ambiguous_add_keys={"item1"})
            state = {"base": "pk", "sets": [], "sent": []}
            self._publish(tg, data, keys, state)
            set_name = "pks1_by_GodVerifyEmojiMapperbot"
            live = tg.sets[set_name]
            self.assertEqual(len(live), 2)                     # item1 exactly once
            self.assertEqual(tg.add_calls, ["item1"])          # one send, no retry
            self.assertEqual(state["sets"][0]["keys"], keys)
            with Catalog(data / "catalog.db") as cat:
                self.assertTrue(cat.get(keys[1]).uploaded)

    def test_occupied_set_is_adopted_after_lost_state(self):
        # State was lost but the set (with item0 inside) still exists live:
        # publishing again must adopt it, not fail or duplicate item0.
        with tempfile.TemporaryDirectory() as t:
            data = Path(t)
            keys = self._seed_catalog(data)
            set_name = "pks1_by_GodVerifyEmojiMapperbot"

            class OccupiedTG(FakeTelegram):
                def create_emoji_set(self, user_id, name, title, path, fmt,
                                     emojis, keywords):
                    if name in self.sets:
                        raise RuntimeError("createNewStickerSet failed: sticker "
                                           "set name is already occupied")
                    super().create_emoji_set(user_id, name, title, path, fmt,
                                             emojis, keywords)

            tg = OccupiedTG(fuid_for={"item1": "UP-item1"})
            tg.sets[set_name] = [
                {"emojis": ["😀"], "fmt": "static", "custom_emoji_id": "cid-a",
                 "file_unique_id": "SRC-item0"}]
            state = {"base": "pk", "sets": [], "sent": []}
            self._publish(tg, data, keys, state)
            live = tg.sets[set_name]
            self.assertEqual(len(live), 2)                     # item0 kept, item1 added
            self.assertEqual(tg.add_calls, ["item1"])
            self.assertEqual(state["sets"][0]["keys"], keys)
            with Catalog(data / "catalog.db") as cat:
                self.assertTrue(cat.get(keys[0]).uploaded)

    def test_publish_records_uploaded_fuids_so_fetch_skips_download(self):
        # After publishing, the live copies' file_unique_ids must be in
        # seen_files: fetching our own pack then hits the fast pre-dedup
        # (fetch_pack/fetch_emoji_ids consult seen_file_unique_id first).
        with tempfile.TemporaryDirectory() as t:
            data = Path(t)
            keys = self._seed_catalog(data)
            tg = FakeTelegram(fuid_for={"item0": "UP-item0", "item1": "UP-item1"})
            state = {"base": "pk", "sets": [], "sent": []}
            self._publish(tg, data, keys, state)
            with Catalog(data / "catalog.db") as cat:
                self.assertEqual(cat.seen_file_unique_id("UP-item0"), keys[0])
                self.assertEqual(cat.seen_file_unique_id("UP-item1"), keys[1])
                # source fuids from ingest are still known too
                self.assertEqual(cat.seen_file_unique_id("SRC-item0"), keys[0])


if __name__ == "__main__":
    unittest.main()
