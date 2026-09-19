"""A curation plan is live state, not an expendable migration sidecar."""
import json
from unittest import mock

from emojikit import collection_migrate as cm
from emojikit import state_artifacts as artifacts
from scripts import identity_repair as ir
from tests.test_identity_migration import MigrationCase



# Claimed by the `windows-safety` CI job: symlink and path refusals differ on Windows.
# tests/test_ci_coverage.py enforces the match both ways.
RUNS_ON_NATIVE_WINDOWS = True

class CurationPlanMigration(MigrationCase):
    def setup_plan(self):
        src = self.media("007_video_aaaaaaaaaaaa.webm")
        old, new = "v:aaaaaaaaaaaaaaaa", "v:bbbbbbbbbbbbbbbb"
        self.cat.add(old, src, phash=0, cid="CID", fuid="FUID")
        path = self.data / artifacts.PACK_PLAN_NAME
        doc = {"version": 1, "known": [old], "excluded": [old], "targets": [[old, 2]],
               "moves": [{"key": old, "label": old, "from_pack": 1, "to_pack": 2}],
               "held": [{"key": old, "label": "v:free-text", "from_pack": 1}]}
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path, doc, old, new

    def test_migrate_restore_and_repeat_cover_all_plan_references_not_labels(self):
        path, before, old, new = self.setup_plan()
        with self.fake_fingerprint({"007_video_aaaaaaaaaaaa.webm": (new, 9)}):
            self.assertEqual(ir.migrate(self.data, apply=True), 0)
            after = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(artifacts.plan_keys(after), {new})
            self.assertEqual(after["moves"][0]["label"], old)
            self.assertEqual(after["held"][0]["label"], "v:free-text")
            self.assertEqual(ir.report(self.data), 0)
            count = len(list(self.data.glob("*.rollback.json")))
            self.assertEqual(ir.migrate(self.data, apply=True), 0)
            self.assertEqual(len(list(self.data.glob("*.rollback.json"))), count)
        manifest = next(self.data.glob("*.rollback.json"))
        self.assertIn(path.name, json.loads(manifest.read_text())["states"])
        cm.restore_migration(self.data, manifest, apply=True)
        self.assertEqual(json.loads(path.read_text()), before)
        self.assertEqual(self.cat.col("items"), [old])
        cm.restore_migration(self.data, manifest, apply=True)
        self.assertEqual(json.loads(path.read_text()), before)

    def test_plan_rewrite_interruption_replays_and_retains_the_journal(self):
        path, _, _, new = self.setup_plan()
        real = cm._apply_state

        def interrupted(*args, **kwargs):
            real(*args, **kwargs)
            raise KeyboardInterrupt("after curation plan replacement")

        with self.fake_fingerprint({"007_video_aaaaaaaaaaaa.webm": (new, 9)}):
            with mock.patch.object(cm, "_apply_state", interrupted), self.assertRaises(KeyboardInterrupt):
                ir.migrate(self.data, apply=True)
            self.assertTrue(cm.journal_path(self.data).exists())
            self.assertEqual(ir.migrate(self.data, apply=True), 0)
        self.assertEqual(artifacts.plan_keys(json.loads(path.read_text())), {new})
        self.assertFalse(cm.journal_path(self.data).exists())

    def test_later_plan_edit_blocks_rollback_without_overwriting_it(self):
        path, _, _, new = self.setup_plan()
        with self.fake_fingerprint({"007_video_aaaaaaaaaaaa.webm": (new, 9)}):
            ir.migrate(self.data, apply=True)
        manifest = next(self.data.glob("*.rollback.json"))
        edited = json.loads(path.read_text())
        edited["moves"][0]["to_pack"] = 3
        path.write_text(json.dumps(edited), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "state changed"):
            cm.restore_migration(self.data, manifest, apply=True)
        self.assertEqual(json.loads(path.read_text()), edited)
        self.assertEqual(self.cat.col("items"), [new])

    def test_missing_plan_reference_is_incomplete_even_without_a_video_change(self):
        path, doc, old, _ = self.setup_plan()
        doc["targets"] = [["v:missing", 2]]
        path.write_text(json.dumps(doc), encoding="utf-8")
        with self.fake_fingerprint({"007_video_aaaaaaaaaaaa.webm": (old, 0)}):
            self.assertNotEqual(ir.report(self.data), 0)
            with self.assertRaisesRegex(RuntimeError, "required state"):
                cm.apply_migration(self.data)
        self.assertFalse(list(self.data.glob("*.rollback.json")))
        self.assertFalse(cm.journal_path(self.data).exists())

    def test_invalid_plan_is_refused_before_a_snapshot_or_mutation(self):
        path, _, old, new = self.setup_plan()
        for text in ("null", '{"version":999}', '{"version":1,"moves":[],"held":{},"targets":[]}'):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with self.fake_fingerprint({"007_video_aaaaaaaaaaaa.webm": (new, 9)}), \
                        self.assertRaises((RuntimeError, ValueError)):
                    cm.apply_migration(self.data)
                self.assertEqual(self.cat.col("items"), [old])
                self.assertFalse(list(self.data.glob("*.rollback.json")))
