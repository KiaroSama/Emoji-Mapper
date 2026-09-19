"""Legacy recovery must prove row identity independently of archive positions."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from unittest import mock

from emojikit import collection_migrate as cm
from scripts import identity_repair as ir
from tests.test_identity_migration import MigrationCase, _Catalog



# Claimed by the `windows-safety` CI job: provenance records canonical native paths.
# tests/test_ci_coverage.py enforces the match both ways.
RUNS_ON_NATIVE_WINDOWS = True

class LegacyRecoveryRequiresProvenance(MigrationCase):
    def snapshots(self, *, same_path=False, old_ids=("OLD-CID", "OLD-FUID"),
                  current_ids=("OTHER-CID", "OTHER-FUID")):
        old_path = self.data / "001_video_aaaaaaaaaaaa.webm"
        current_path = old_path if same_path else self.data / "001_video_bbbbbbbbbbbb.webm"
        current_path.write_bytes(b"\x1a\x45\xdf\xa3different current artwork")
        backup = self.data / "catalog.old.db"
        old = _Catalog(backup)
        old.add("v:aaaaaaaaaaaaaaaa", old_path, cid=old_ids[0], fuid=old_ids[1])
        self.cat.add("v:bbbbbbbbbbbbbbbb", current_path, phash=7,
                     cid=current_ids[0], fuid=current_ids[1])
        self.state("audit", {"sets": [{"keys": ["v:aaaaaaaaaaaaaaaa"]}]})
        return backup, current_path

    def cli(self, backup):
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(ir, "setup_logging"):
            return ir.main(["migrate-video-keys", "--apply", "--from-backup", str(backup),
                            "--data-dir", str(self.data)])

    def assert_refused(self, backup, current):
        before = current.read_bytes()
        with self.fake_fingerprint({current.name: ("v:bbbbbbbbbbbbbbbb", 7)}):
            self.assertEqual(self.cli(backup), ir.EXIT_FAILED)
        self.assertEqual(current.read_bytes(), before)
        state = json.loads((self.data / "publish_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(state["sets"][0]["keys"], ["v:aaaaaaaaaaaaaaaa"])
        self.assertEqual(self.cat.col("items"), ["v:bbbbbbbbbbbbbbbb"])
        self.assertFalse(cm.journal_path(self.data).exists())

    def test_equal_path_with_different_immutable_ids_refuses(self):
        backup, current = self.snapshots(same_path=True)
        self.assert_refused(backup, current)

    def test_same_archive_slot_with_different_immutable_ids_refuses(self):
        backup, current = self.snapshots()
        self.assert_refused(backup, current)

    def test_path_only_without_immutable_provenance_refuses(self):
        backup, current = self.snapshots(old_ids=(None, None), current_ids=(None, None))
        self.assert_refused(backup, current)

    def test_shared_identifiers_recover_across_different_paths_and_positions(self):
        backup, current = self.snapshots(current_ids=("OLD-CID", "OLD-FUID"))
        renamed = current.with_name("unrelated-new-position.webm")
        current.rename(renamed)
        with contextlib.closing(sqlite3.connect(self.cat.path)) as con, con:
            con.execute("UPDATE items SET file_path=?", (str(renamed),))
        with self.fake_fingerprint({renamed.name: ("v:bbbbbbbbbbbbbbbb", 7)}):
            self.assertEqual(self.cli(backup), ir.EXIT_OK)
        state = json.loads((self.data / "publish_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(state["sets"][0]["keys"], ["v:bbbbbbbbbbbbbbbb"])
        self.assertEqual(self.cat.col("publications", "custom_emoji_id"), ["OLD-CID"])
        self.assertEqual(self.cat.col("seen_files", "file_unique_id"), ["OLD-FUID"])

    def test_conflicting_shared_fuid_and_cid_refuse(self):
        backup, current = self.snapshots(current_ids=("OTHER-CID", "OLD-FUID"))
        other = self.media("different-rival.webm")
        self.cat.add("v:cccccccccccccccc", other, phash=9, cid="OLD-CID", fuid="RIVAL-FUID")
        with self.fake_fingerprint({current.name: ("v:bbbbbbbbbbbbbbbb", 7),
                                    other.name: ("v:cccccccccccccccc", 9)}):
            self.assertEqual(cm.recover_key_map(self.data, backup), {})
            self.assertEqual(self.cli(backup), ir.EXIT_FAILED)

    def test_shared_cid_bound_to_multiple_current_rows_refuses(self):
        backup, current = self.snapshots(current_ids=("OLD-CID", "OLD-FUID"))
        other = self.media("same-cid-rival.webm")
        self.cat.add("v:cccccccccccccccc", other, phash=9, cid="OLD-CID", fuid="RIVAL-FUID")
        with self.fake_fingerprint({current.name: ("v:bbbbbbbbbbbbbbbb", 7),
                                    other.name: ("v:cccccccccccccccc", 9)}):
            self.assertEqual(cm.recover_key_map(self.data, backup), {})
            self.assertEqual(self.cli(backup), ir.EXIT_FAILED)

    def test_shared_cid_bound_to_multiple_backup_rows_refuses(self):
        backup, current = self.snapshots(current_ids=("OLD-CID", "OLD-FUID"))
        old = _Catalog.__new__(_Catalog)
        old.path = backup
        old.add("v:cccccccccccccccc", self.data / "other-old-file.webm",
                cid="OLD-CID", fuid="OTHER-OLD-FUID")
        self.assert_refused(backup, current)

    def test_shared_fuid_alone_can_prove_the_mapping(self):
        backup, current = self.snapshots(old_ids=(None, "SHARED-FUID"),
                                         current_ids=(None, "SHARED-FUID"))
        with self.fake_fingerprint({current.name: ("v:bbbbbbbbbbbbbbbb", 7)}):
            self.assertEqual(self.cli(backup), ir.EXIT_OK)
        self.assertEqual(self.cat.col("seen_files", "file_unique_id"), ["SHARED-FUID"])

    def test_shared_publication_cid_alone_can_prove_the_mapping(self):
        backup, current = self.snapshots(old_ids=("SHARED-CID", None),
                                         current_ids=("SHARED-CID", None))
        with self.fake_fingerprint({current.name: ("v:bbbbbbbbbbbbbbbb", 7)}):
            self.assertEqual(self.cli(backup), ir.EXIT_OK)
        self.assertEqual(self.cat.col("publications", "custom_emoji_id"), ["SHARED-CID"])
