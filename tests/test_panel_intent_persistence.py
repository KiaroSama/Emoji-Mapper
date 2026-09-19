"""Actual HTTP/SQLite persistence, scoped intent and the maintenance boundary."""
from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import error, request
from unittest import mock

from PIL import Image

from emojikit import identity, panel
from emojikit.catalog import Catalog
from emojikit.maintenance import maintenance
from emojikit.packstate import LockBusy
from emojikit.panel_plan import PLAN_NAME, merge_plan


class PanelIntentPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "catalog.db"
        self.keys = []
        self.plan = self.root / PLAN_NAME
        with Catalog(self.db) as cat:
            for i in range(5):
                path = self.root / f"{i}.png"
                Image.new("RGBA", (100, 100), (40 * i, 40, 80, 255)).save(path)
                key = identity.content_key(path, "static")
                self.keys.append(key)
                cat.add(content_key=key, fmt="static", file_path=path)
                if i < 4:
                    cat.mark_uploaded(key, f"CID{i}", base="fixture",
                                      set_name="one" if i < 2 else "two")
            cat.set_order(self.keys)
            cat.set_meta("order_seeded", "1")
            view, images, _ = panel.build_view(cat, "", False, {"one": 1, "two": 2})
        handler = panel.make_handler(view, images, self.db, "fixture-token",
                                     keep_sets={"one": 1, "two": 2})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def call(self, path="/", body=None):
        req = request.Request(f"http://127.0.0.1:{self.server.server_port}{path}",
                              data=None if body is None else json.dumps(body).encode(),
                              headers={"Content-Type": "application/json",
                                       "X-Panel-Token": "fixture-token"})
        try:
            with request.urlopen(req, timeout=5) as response:
                return response.status, response.read().decode()
        except error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def page(self):
        status, body = self.call()
        self.assertEqual(status, 200, body)
        return json.loads(re.search(r'<script[^>]*id="items-data"[^>]*>(.*?)</script>',
                                   body, re.S)[1])

    def save(self, *, targets=None, known=None, excluded=(), include_packs=True):
        body = {"excluded": list(excluded), "known": self.keys if known is None else known}
        if include_packs:
            body["packs"] = targets if targets is not None else [[self.keys[0], 2]]
        return self.call("/api/save", body)

    def load_plan(self):
        return json.loads(self.plan.read_text(encoding="utf-8"))

    def test_reload_then_save_preserves_intent_without_redefining_live_membership(self):
        self.assertEqual(self.save()[0], 200)
        for _ in range(2):
            page = self.page()
            item = next(card for card in page if card["key"] == self.keys[0])
            self.assertEqual(item["pack"], 2)
            self.assertEqual(self.save(targets=[[v["key"], v["pack"]]
                                               for v in page if "pack" in v])[0], 200)
            self.assertEqual(self.load_plan()["moves"], [
                {"key": self.keys[0], "label": item["label"], "from_pack": 1, "to_pack": 2}])

    def test_unpublished_target_survives_reload_without_inventing_a_live_source(self):
        self.assertEqual(self.save(targets=[[self.keys[4], 2]])[0], 200)
        fresh = {card["key"]: card for card in self.page()}
        self.assertEqual(fresh[self.keys[4]]["pack"], 2)
        self.assertEqual(self.load_plan()["moves"], [])

    def test_partial_scope_keeps_other_tabs_moves_holds_and_targets(self):
        self.assertEqual(self.save(excluded=[self.keys[2]])[0], 200)
        before = self.load_plan()
        self.assertEqual(self.save(known=[self.keys[1]], targets=[[self.keys[1], 1]])[0], 200)
        after = self.load_plan()
        self.assertEqual(after["moves"], before["moves"])
        self.assertEqual(after["held"], before["held"])
        self.assertEqual(dict(after["targets"])[self.keys[0]], 2)
        self.assertIn(self.keys[2], after["excluded"])

    def test_legacy_save_does_not_clear_target_intent(self):
        self.assertEqual(self.save()[0], 200)
        self.assertEqual(self.save(include_packs=False, excluded=[self.keys[1]])[0], 200)
        self.assertEqual(dict(self.load_plan()["targets"])[self.keys[0]], 2)
        self.assertIn(self.keys[1], self.load_plan()["excluded"])

    def test_invalid_targets_refuse_without_mutation(self):
        self.save()
        before = self.plan.read_bytes()
        invalid = ([[self.keys[0], -1]], [[self.keys[0], 0]], [[self.keys[0], True]],
                   [[self.keys[0], 1.5]], [[self.keys[0], 2**54]],
                   [[self.keys[0], 1], [self.keys[0], 2]], [["s:unknown", 1]])
        for targets in invalid:
            with self.subTest(targets=targets):
                self.assertEqual(self.save(targets=targets, excluded=[self.keys[1]])[0], 400)
                self.assertEqual(self.plan.read_bytes(), before)
                with Catalog(self.db) as cat:
                    self.assertTrue(cat.get(self.keys[1]).included)
        self.assertEqual(self.save(known=[self.keys[1]], targets=[[self.keys[0], 2]])[0], 400)

    def test_bad_saved_plan_is_preserved_instead_of_overwritten(self):
        for text in ("null", "{broken", '{"version":99}',
                     '{"version":1,"moves":[],"held":[],"targets":[["x",-1]]}'):
            with self.subTest(text=text):
                self.plan.write_text(text, encoding="utf-8")
                self.assertEqual(self.save(excluded=[self.keys[1]])[0], 409)
                self.assertEqual(self.call()[0], 409)
                self.assertEqual(self.plan.read_text(), text)
                with Catalog(self.db) as cat:
                    self.assertTrue(cat.get(self.keys[1]).included)

    def test_old_delta_only_plan_still_loads_and_upgrades_without_losing_moves(self):
        old = {"version": 1, "held": [], "moves": [
            {"key": self.keys[0], "label": "first", "from_pack": 1, "to_pack": 2}]}
        self.plan.write_text(json.dumps(old))
        self.assertEqual(self.page()[0]["pack"], 2)
        self.save(known=[self.keys[1]], targets=[[self.keys[1], 1]])
        self.assertEqual(self.load_plan()["moves"], old["moves"])

    def test_plan_write_keeps_catalog_lease_and_blocks_maintenance(self):
        real = panel.write_plan
        observed = []

        def guarded(*args):
            def contender():
                try:
                    with maintenance(self.root):
                        observed.append("entered")
                except LockBusy:
                    observed.append("blocked")
            process = threading.Thread(target=contender)
            process.start()
            process.join(2)
            self.assertFalse(process.is_alive())
            return real(*args)

        with mock.patch.object(panel, "write_plan", guarded):
            self.assertEqual(self.save()[0], 200)
        self.assertEqual(observed, ["blocked"])

    def test_plan_write_failure_is_not_acknowledged_and_exact_retry_completes(self):
        with mock.patch.object(panel, "write_plan", side_effect=OSError("disk full")):
            status, response = self.save(excluded=[self.keys[1]])
            self.assertEqual(status, 503)
            self.assertNotEqual(json.loads(response).get("ok"), True)
        self.assertEqual(self.save(excluded=[self.keys[1]])[0], 200)
        self.assertIn(self.keys[1], self.load_plan()["excluded"])
        self.assertEqual(dict(self.load_plan()["targets"])[self.keys[0]], 2)

    def test_stale_server_view_after_rekey_cannot_acknowledge_old_keys(self):
        with Catalog(self.db) as cat:
            cat.db.execute("UPDATE items SET content_key='s:changed' WHERE content_key=?", (self.keys[0],))
            cat.db.commit()
        self.assertEqual(self.save()[0], 409)
        self.assertFalse(self.plan.exists())
        self.assertEqual(self.call("/api/order", {"order": self.keys})[0], 409)

    def test_actual_video_plan_survives_migration_reload_and_restore(self):
        from emojikit import collection_migrate as cm
        from scripts import identity_repair as ir
        from tests.test_video_collision_ingest import RED, encode

        media = encode(self.root, "legacy-video", [RED] * 3)
        old = "v:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        with Catalog(self.db) as cat:
            cat.db.execute("UPDATE items SET content_key=?, format='video', file_path=? "
                           "WHERE content_key=?", (old, str(media), self.keys[4]))
            cat.db.commit()
            cat.mark_uploaded(old, "VIDEO-CID", base="fixture", set_name="one")
        self.keys[4] = old
        self.page()
        self.assertEqual(self.save(targets=[[old, 2]])[0], 200)
        before = self.plan.read_bytes()
        self.assertEqual(ir.migrate(self.root, apply=True), 0)
        with Catalog(self.db) as cat:
            video = next(item for item in cat.all_items() if item.fmt == "video")
            self.assertNotEqual(video.content_key, old)
            self.assertEqual(cat.custom_emoji_id_for("fixture", video.content_key), "VIDEO-CID")
        page = {card["key"]: card for card in self.page()}
        self.assertEqual(page[video.content_key]["pack"], 2)
        self.assertEqual(dict(self.load_plan()["targets"]), {video.content_key: 2})
        self.assertEqual(ir.migrate(self.root, apply=True), 0)
        bundle = next(self.root.glob("*.rollback.json"))
        cm.restore_migration(self.root, bundle, apply=True)
        self.assertEqual(json.loads(self.plan.read_bytes()), json.loads(before))
        self.assertEqual({card["key"]: card for card in self.page()}[old]["pack"], 2)

    def test_capacity_counts_the_logo_for_each_target_pack(self):
        view = [{"key": "__logo__", "isLogo": True, "included": True},
                {"key": "a", "included": True, "pack": 1},
                {"key": "b", "included": True, "pack": 1}]
        plan = merge_plan(None, view, {"a": 1, "b": 1}, {"a", "b"}, 2)
        self.assertEqual(plan["counts"], {"1": 2})
        self.assertEqual(plan["over_capacity"], {"1": 3})
