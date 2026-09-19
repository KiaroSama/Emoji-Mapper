"""Real BUSY/LOCKED retries must not leave migration or restore hung forever."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from emojikit import collection_migrate as cm
from emojikit import sqlite_snapshot as snapshot
from tests.test_identity_migration import _Catalog



# Claimed by the `windows-safety` CI job: lock contention and busy waits are platform behaviour.
# tests/test_ci_coverage.py enforces the match both ways.
RUNS_ON_NATIVE_WINDOWS = True

class BoundedSnapshot(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "catalog.db"
        _Catalog(self.db, wal=False)

    def connect(self, db):
        con = sqlite3.connect(db)
        self.addCleanup(con.close)
        return con

    def test_exclusive_source_lock_expires_and_removes_partial_backup(self):
        holder = self.connect(self.db)
        holder.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        with mock.patch.object(snapshot, "BACKUP_TIMEOUT", 0.15), \
                self.assertRaises(snapshot.BackupTimeout):
            cm.backup_catalog(self.db)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(list(self.db.parent.glob("catalog.before*")), [])
        holder.rollback()
        backup = cm.backup_catalog(self.db)
        self.assertEqual(self.connect(backup).execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_locked_restore_destination_keeps_original_rows_and_can_retry(self):
        source = self.connect(":memory:")
        source.execute("CREATE TABLE sentinel(value)")
        source.execute("INSERT INTO sentinel VALUES ('snapshot')")
        source.commit()
        holder = self.connect(self.db)
        holder.execute("BEGIN EXCLUSIVE")
        destination = self.connect(self.db)
        destination.execute("PRAGMA busy_timeout=731")
        with self.assertRaises(snapshot.BackupTimeout):
            snapshot.backup(source, destination, timeout=0.15)
        self.assertEqual(destination.execute("PRAGMA busy_timeout").fetchone()[0], 731)
        holder.rollback()
        self.assertIn("items", {r[0] for r in destination.execute("SELECT name FROM sqlite_master")})
        snapshot.backup(source, destination)
        self.assertEqual(destination.execute("SELECT value FROM sentinel").fetchone()[0], "snapshot")

    def test_committed_wal_rows_are_in_the_reopened_snapshot(self):
        con = self.connect(self.db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("INSERT INTO items VALUES ('v:one','clip.webm','video',5)")
        con.commit()
        self.assertTrue(Path(str(self.db) + "-wal").is_file())
        backup = cm.backup_catalog(self.db)
        self.assertEqual(self.connect(backup).execute("SELECT content_key FROM items").fetchall(), [("v:one",)])
        second = cm.backup_catalog(self.db)
        self.assertNotEqual(backup, second)

    def test_bad_budgets_refuse_before_writing(self):
        src, dst = self.connect(":memory:"), self.connect(":memory:")
        for budget in (0, -1, float("nan"), float("inf")):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                snapshot.backup(src, dst, timeout=budget)
