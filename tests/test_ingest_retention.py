"""A refused complete download survives both CLI scratch-directory cleanups."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from emojikit import fetch_emoji_ids
from emojikit import fetch_pack
from emojikit import identity, media
from emojikit.catalog import Catalog
from emojikit.ingest import store_media
from tests.test_video_collision_ingest import FakeTelegram, RED, TEST_ROOT, encode


class RefusedDownloadRetention(unittest.TestCase):
    def test_both_cli_cleanups_preserve_complete_input_beside_occupied_targets(self):
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="a01-retention-", dir=TEST_ROOT) as directory:
            root = Path(directory)
            clip = encode(root, "clip", [RED] * 3)
            tg = FakeTelegram({"12345": clip})
            tg.get_me = lambda: {"username": "fixture_bot"}
            real_reencode = media.reencode_in_place
            for module, args in ((fetch_pack, ["fixture"]), (fetch_emoji_ids, ["--id", "12345"])):
                with self.subTest(route=module.__name__):
                    data = root / module.__name__
                    incoming = []
                    occupied = {}

                    def prepare_collision(path, fmt, incoming=incoming, module=module,
                                          data=data, occupied=occupied):
                        result = real_reencode(path, fmt)
                        payload = path.read_bytes()
                        incoming.append(payload)
                        key = identity.content_key(path, fmt)
                        base = module._media_path(data, fmt, key, ".webm")
                        alternate = base.with_name(identity.collision_key(path, key).replace(":", "_") + ".webm")
                        base.parent.mkdir(parents=True, exist_ok=True)
                        occupied[base] = b"existing unrelated bytes"
                        occupied[alternate] = payload[:len(payload) // 2]
                        for destination, sentinel in occupied.items():
                            destination.write_bytes(sentinel)
                        return result

                    with mock.patch.object(module, "Telegram", return_value=tg), \
                            mock.patch.object(module, "os", SimpleNamespace(path=os.path, environ={"TEST_AUTH": "fixture"})), \
                            mock.patch.object(module, "load_env"), mock.patch.object(module, "setup_logging"), \
                            mock.patch.object(media, "reencode_in_place", side_effect=prepare_collision), \
                            contextlib.redirect_stdout(io.StringIO()) as output:
                        result = module.main(args + ["--data-dir", str(data), "--repaintable", "keep", "--token-env", "TEST_AUTH"])
                    self.assertEqual(result, 4)
                    self.assertIn("failed=1", output.getvalue())
                    self.assertFalse(list((data / "tmp").rglob("*.dl")), "the CLI scratch cleanup did not run")
                    for path, sentinel in occupied.items():
                        self.assertEqual(path.read_bytes(), sentinel, "an occupied destination was overwritten")
                    retained = [p for p in (data / "media").rglob("*.webm")
                                if p not in occupied and p.read_bytes() == incoming[0]]
                    self.assertEqual(len(retained), 1, "complete refused input vanished during CLI cleanup")
                    record = json.loads(retained[0].with_suffix(".json").read_text(encoding="utf-8"))
                    self.assertEqual(record["sha256"], hashlib.sha256(incoming[0]).hexdigest())
                    self.assertEqual(record["size"], len(incoming[0]))
                    self.assertEqual(record["provenance"]["file_unique_id"], "fuid-12345")
                    with tempfile.TemporaryDirectory(dir=data / "tmp") as scratch:
                        retry = Path(scratch) / "retry.dl"
                        retry.write_bytes(incoming[0])
                        with self.assertRaisesRegex(media.MediaError, "retained at"):
                            store_media(retry, next(iter(occupied)), "video", record["content_key"])
                    self.assertEqual(list(retained[0].parent.glob("*.webm")), retained,
                                     "the same refused bytes accumulated redundant quarantine copies")
                    with Catalog(data / "catalog.db") as cat:
                        self.assertEqual(cat.all_items(), [])
                        self.assertIsNone(cat.seen_file_unique_id("fuid-12345"))


if __name__ == "__main__":
    unittest.main()
