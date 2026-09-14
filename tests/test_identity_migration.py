"""R03/R04/R05: a key migration moves every reference, or it does not run.

Three defects, one subject.

**R03** -- the first round updated `items`, `seen_files` and `publications` and
stopped. The owner's real `publish_<base>.json` was left naming 51 keys the
catalog no longer had, and that list is what `reconcile_set()` attributes live
stickers by: the next publish would have read an untouched pack as reordered or
replaced. The frozen plans were stale too, the derived `phash` was never
refreshed where a key happened not to move, and the archived filenames -- which
embed `key[:12]` -- were left to a manual follow-up command.

**R04** -- the backup was `shutil.copy2` of the `.db` alone. This catalog runs
in WAL, where committed rows live in `-wal` until a checkpoint, so with a writer
still open the "backup" opened as a database with `no such table: items` while
the original held the row.

**R05** -- a row whose file was missing was counted as unchanged, so a catalog
nobody could read printed "Every video key already matches" and exited 0.

The recompute is injected in most cases: what is under test is the bookkeeping,
and encoding real video for every branch would buy nothing and cost minutes. The
cases that are ABOUT decoding live in `test_video_identity_fidelity.py`.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit import collection_migrate as cm  # noqa: E402
from emojikit import packstate  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
import identity_repair as ir  # noqa: E402


class _Catalog:
    """A catalog with the three tables that store a content key."""

    SCHEMA = (
        "CREATE TABLE items(content_key TEXT PRIMARY KEY, file_path TEXT,"
        " format TEXT, phash INTEGER);"
        "CREATE TABLE publications(base TEXT, content_key TEXT,"
        " set_name TEXT, custom_emoji_id TEXT);"
        "CREATE TABLE seen_files(file_unique_id TEXT PRIMARY KEY,"
        " content_key TEXT);")

    def __init__(self, path: Path, *, wal: bool = True):
        self.path = path
        con = sqlite3.connect(path)
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
        con.executescript(self.SCHEMA)
        con.commit()
        con.close()

    def add(self, key, path, fmt="video", phash=None, cid=None, fuid=None):
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO items VALUES(?,?,?,?)",
                    (key, str(path), fmt, phash))
        if cid:
            con.execute("INSERT INTO publications VALUES(?,?,?,?)",
                        ("base", key, "set1", cid))
        if fuid:
            con.execute("INSERT INTO seen_files VALUES(?,?)", (fuid, key))
        con.commit()
        con.close()

    def col(self, table, column="content_key"):
        con = sqlite3.connect(self.path)
        try:
            return [r[0] for r in con.execute(f"SELECT {column} FROM {table}")]
        finally:
            con.close()


class MigrationCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.cat = _Catalog(self.data / "catalog.db")

    def media(self, name: str) -> Path:
        p = self.data / name
        p.write_bytes(b"\x1a\x45\xdf\xa3" + name.encode())
        return p

    def fake_fingerprint(self, mapping: dict[str, tuple[str, int | None]]):
        """Pretend the corrected decoder returns these (key, phash) by filename."""
        by_bytes = {(self.data / name).read_bytes(): result
                    for name, result in mapping.items() if (self.data / name).is_file()}
        def fp(path, fmt):
            name = Path(path).name
            result = mapping.get(name) or by_bytes.get(Path(path).read_bytes())
            if result is None:
                raise cm.MediaError(f"undecodable: {name}")
            return result
        return mock.patch.object(cm.identity, "fingerprint", fp)

    def state(self, base: str, doc: dict) -> Path:
        p = self.data / f"publish_{base}.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        return p

    def plan(self, base: str, doc: dict) -> Path:
        p = self.data / f"publish_plan_{base}.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        return p


class EveryDurableReferenceMoves(MigrationCase):
    def test_the_state_file_is_rewritten_not_just_the_tables(self):
        """The defect that reached the owner's real data."""
        m = self.media("clip.webm")
        self.cat.add("v:old", m, cid="cid-1", fuid="F-1")
        st = self.state("pk", {"base": "pk", "sets": [
            {"fmt": "mixed", "index": 1, "name": "pk1", "title": "Pack 1",
             "live": 1, "keys": ["v:old"]}], "sent": [], "skipped": ["v:old"],
            "sent_full": []})
        self.plan("pk", {"mixed": ["v:old"]})

        with self.fake_fingerprint({"clip.webm": ("v:new", 5)}):
            code = ir.migrate(self.data, apply=True)

        self.assertEqual(code, ir.EXIT_OK)
        self.assertEqual(self.cat.col("items"), ["v:new"])
        self.assertEqual(self.cat.col("publications"), ["v:new"])
        self.assertEqual(self.cat.col("seen_files"), ["v:new"])
        doc = json.loads(st.read_text(encoding="utf-8"))
        self.assertEqual(doc["sets"][0]["keys"], ["v:new"])
        self.assertEqual(doc["skipped"], ["v:new"],
                         "skipped names keys too")
        self.assertEqual(json.loads((self.data / "publish_plan_pk.json")
                                    .read_text(encoding="utf-8")),
                         {"mixed": ["v:new"]})

    def test_multiple_bases_all_move(self):
        m = self.media("clip.webm")
        self.cat.add("v:old", m)
        for base in ("alpha", "beta"):
            self.plan(base, {"mixed": ["v:old"]})
        with self.fake_fingerprint({"clip.webm": ("v:new", 1)}):
            ir.migrate(self.data, apply=True)
        for base in ("alpha", "beta"):
            self.assertEqual(json.loads((self.data / f"publish_plan_{base}.json")
                                        .read_text(encoding="utf-8")),
                             {"mixed": ["v:new"]})

    def test_the_telegram_identifiers_are_preserved(self):
        m = self.media("clip.webm")
        self.cat.add("v:old", m, cid="cid-keepme", fuid="F-1")
        with self.fake_fingerprint({"clip.webm": ("v:new", 1)}):
            ir.migrate(self.data, apply=True)
        con = sqlite3.connect(self.data / "catalog.db")
        try:
            row = con.execute("SELECT set_name, custom_emoji_id FROM publications"
                              ).fetchone()
        finally:
            con.close()
        self.assertEqual(row, ("set1", "cid-keepme"))

    def test_a_hash_only_correction_is_applied_even_when_the_key_holds(self):
        """R03's quieter half: the derived hash moves with the decoder."""
        m = self.media("clip.webm")
        self.cat.add("v:same", m, phash=123)
        with self.fake_fingerprint({"clip.webm": ("v:same", 0)}):
            code = ir.migrate(self.data, apply=True)
        self.assertEqual(code, ir.EXIT_OK)
        self.assertEqual(self.cat.col("items", "phash"), [0])

    def test_an_archived_file_is_renamed_to_match_its_new_key(self):
        """The name embeds key[:12]; leaving it is the manual step R03 forbids."""
        archived = self.data / "007_video_0123456789ab.webm"
        archived.write_bytes(b"\x1a\x45\xdf\xa3archived")
        self.cat.add("v:0123456789abcdef", archived)
        with self.fake_fingerprint(
                {"007_video_0123456789ab.webm": ("v:fedcba9876543210", 1)}):
            ir.migrate(self.data, apply=True)
        want = self.data / "007_video_fedcba987654.webm"
        self.assertTrue(want.is_file(), "the archived file kept the old key")
        self.assertFalse(archived.is_file())
        self.assertEqual(self.cat.col("items", "file_path"), [str(want)])

    def test_a_second_migration_is_a_verified_no_op(self):
        m = self.media("clip.webm")
        self.cat.add("v:old", m)
        self.plan("pk", {"mixed": ["v:old"]})
        with self.fake_fingerprint({"clip.webm": ("v:new", 1)}):
            self.assertEqual(ir.migrate(self.data, apply=True), ir.EXIT_OK)
            self.assertEqual(ir.migrate(self.data, apply=True), ir.EXIT_OK)
        self.assertEqual(self.cat.col("items"), ["v:new"])

    def test_the_migration_takes_the_pack_family_lock(self):
        """A migration-only lock nobody else honours would be decoration."""
        m = self.media("clip.webm")
        self.cat.add("v:old", m)
        self.plan("pk", {"mixed": ["v:old"]})
        held = packstate.pack_family_lock_path("pk")
        with packstate.exclusive_lock(held):
            done = threading.Event()
            err: list[Exception] = []

            def run():
                try:
                    with self.fake_fingerprint({"clip.webm": ("v:new", 1)}):
                        ir.migrate(self.data, apply=True)
                except Exception as exc:            # noqa: BLE001
                    err.append(exc)
                finally:
                    done.set()

            t = threading.Thread(target=run, daemon=True)
            t.start()
            finished = done.wait(timeout=8)
        t.join(timeout=20)
        self.assertTrue(finished or err, "the migration ignored the family lock")
        if finished and not err:
            self.fail("the migration ran while the family lock was held")


class NothingIsAppliedFromAnIncompletePicture(MigrationCase):
    def test_a_missing_file_is_not_reported_as_unchanged(self):
        """R05's reproduction: this printed a clean success and exited 0."""
        self.cat.add("v:gone", self.data / "absent.webm")
        code = ir.migrate(self.data, apply=True)
        self.assertEqual(code, ir.EXIT_FAILED)
        self.assertEqual(self.cat.col("items"), ["v:gone"], "it mutated anyway")

    def test_report_mode_says_incomplete_rather_than_clean(self):
        self.cat.add("v:gone", self.data / "absent.webm")
        self.assertEqual(ir.report(self.data), ir.EXIT_FAILED)

    def test_some_missing_still_blocks_the_whole_migration(self):
        m = self.media("clip.webm")
        self.cat.add("v:ok", m)
        self.cat.add("v:gone", self.data / "absent.webm")
        with self.fake_fingerprint({"clip.webm": ("v:new", 1)}):
            self.assertEqual(ir.migrate(self.data, apply=True), ir.EXIT_FAILED)
        self.assertEqual(sorted(self.cat.col("items")), ["v:gone", "v:ok"])

    def test_all_undecodable_blocks_it_too(self):
        self.cat.add("v:a", self.media("a.webm"))
        self.cat.add("v:b", self.media("b.webm"))
        with self.fake_fingerprint({}):          # every call raises
            self.assertEqual(ir.migrate(self.data, apply=True), ir.EXIT_FAILED)
        self.assertEqual(sorted(self.cat.col("items")), ["v:a", "v:b"])

    def test_a_collision_refuses_instead_of_merging(self):
        a, b = self.media("a.webm"), self.media("b.webm")
        self.cat.add("v:a", a, cid="cid-a")
        self.cat.add("v:b", b, cid="cid-b")
        with self.fake_fingerprint({"a.webm": ("v:same", 1),
                                    "b.webm": ("v:same", 1)}):
            self.assertEqual(ir.migrate(self.data, apply=True), ir.EXIT_FAILED)
        self.assertEqual(sorted(self.cat.col("items")), ["v:a", "v:b"])

    def test_colliding_with_a_row_that_is_not_moving_also_refuses(self):
        self.cat.add("v:taken", self.media("b.webm"), fmt="static")
        self.cat.add("v:old", self.media("a.webm"))
        with self.fake_fingerprint({"a.webm": ("v:taken", 1)}):
            self.assertEqual(ir.migrate(self.data, apply=True), ir.EXIT_FAILED)
        self.assertEqual(sorted(self.cat.col("items")), ["v:old", "v:taken"])

    def test_an_empty_catalog_is_clean_not_pending(self):
        self.assertEqual(ir.report(self.data), ir.EXIT_OK)

    def test_a_clean_catalog_reports_clean(self):
        m = self.media("clip.webm")
        self.cat.add("v:same", m, phash=7)
        with self.fake_fingerprint({"clip.webm": ("v:same", 7)}):
            self.assertEqual(ir.report(self.data), ir.EXIT_OK)

    def test_a_pending_catalog_reports_stale_and_writes_nothing(self):
        m = self.media("clip.webm")
        before = (self.data / "catalog.db").read_bytes()
        self.cat.add("v:old", m)
        with self.fake_fingerprint({"clip.webm": ("v:new", 1)}):
            self.assertEqual(ir.report(self.data), ir.EXIT_STALE)
        self.assertEqual(self.cat.col("items"), ["v:old"])
        self.assertNotEqual(before, b"")      # the fixture really wrote a db

    def test_only_obsolete_plan_entries_are_informational(self):
        """The publisher intentionally skips absent frozen-plan candidates."""
        m = self.media("clip.webm")
        self.cat.add("v:same", m, phash=1)
        self.plan("pk", {"mixed": ["v:ghost"]})
        with self.fake_fingerprint({"clip.webm": ("v:same", 1)}):
            self.assertEqual(ir.report(self.data), ir.EXIT_OK)


class APreexistingPartialMigrationIsRecoverable(MigrationCase):
    """The real case: a migration that ran BEFORE journals existed.

    It moved the table rows and kept no record of the map, so the state files
    it left behind cannot be repaired by surveying -- the catalog already holds
    the new keys, so a fresh survey reports nothing pending while the state
    still names the old ones. The backup that round DID write is the missing
    half. Pairing requires shared immutable FUID/CID provenance; a path or
    archive position alone cannot distinguish migration from replacement.
    """

    def _preexisting(self):
        """A catalog already migrated, with its state file left behind."""
        archived = self.data / "007_video_aaaaaaaaaaaa.webm"
        archived.write_bytes(b"\x1a\x45\xdf\xa3one")
        plain = self.media("loose.webm")
        backup = self.data / "catalog.before-video-identity-1.db"
        old = _Catalog(backup)
        old.add("v:aaaaaaaaaaaaaaaa", archived, cid="CID-archive", fuid="FUID-archive")
        old.add("v:cccccccccccccccc", plain, cid="CID-loose", fuid="FUID-loose")

        # ... and the live catalog, already moved and already renamed.
        renamed = self.data / "007_video_bbbbbbbbbbbb.webm"
        archived.rename(renamed)
        self.cat.add("v:bbbbbbbbbbbbbbbb", renamed, cid="CID-archive", fuid="FUID-archive")
        self.cat.add("v:dddddddddddddddd", plain, cid="CID-loose", fuid="FUID-loose")
        self.plan("pk", {"mixed": ["v:aaaaaaaaaaaaaaaa", "v:cccccccccccccccc"]})
        return backup

    def test_the_map_is_recovered_from_the_backup(self):
        backup = self._preexisting()
        with self.fake_fingerprint({"007_video_bbbbbbbbbbbb.webm":
                                    ("v:bbbbbbbbbbbbbbbb", 1),
                                    "loose.webm": ("v:dddddddddddddddd", 2)}):
            got = cm.recover_key_map(self.data, backup)
        self.assertEqual(got, {"v:aaaaaaaaaaaaaaaa": "v:bbbbbbbbbbbbbbbb",
                               "v:cccccccccccccccc": "v:dddddddddddddddd"})

    def test_applying_it_repairs_the_state_file(self):
        backup = self._preexisting()
        with self.fake_fingerprint({"007_video_bbbbbbbbbbbb.webm":
                                    ("v:bbbbbbbbbbbbbbbb", 1),
                                    "loose.webm": ("v:dddddddddddddddd", 2)}):
            code = ir.migrate(self.data, apply=True, from_backup=backup)
        self.assertEqual(code, ir.EXIT_OK)
        self.assertEqual(json.loads((self.data / "publish_plan_pk.json")
                                    .read_text(encoding="utf-8")),
                         {"mixed": ["v:bbbbbbbbbbbbbbbb", "v:dddddddddddddddd"]})

    def test_a_key_absent_from_the_backup_is_left_alone(self):
        """Stale entries that predate the backup are not this map's business,
        and must not be guessed at or deleted."""
        backup = self._preexisting()
        self.plan("old", {"mixed": ["v:ghostghostghost"]})
        with self.fake_fingerprint({"007_video_bbbbbbbbbbbb.webm":
                                    ("v:bbbbbbbbbbbbbbbb", 1),
                                    "loose.webm": ("v:dddddddddddddddd", 2)}):
            code = ir.migrate(self.data, apply=True, from_backup=backup)
        self.assertEqual(code, ir.EXIT_OK, "pre-existing noise failed the run")
        self.assertEqual(json.loads((self.data / "publish_plan_old.json")
                                    .read_text(encoding="utf-8")),
                         {"mixed": ["v:ghostghostghost"]})

    def test_an_ambiguous_pairing_is_not_guessed(self):
        backup = self.data / "catalog.before-video-identity-1.db"
        old = _Catalog(backup)
        shared = self.media("same.webm")
        old.add("v:oldoldoldoldold1", shared, cid="ambiguous-CID")
        self.cat.add("v:new1", shared, cid="ambiguous-CID")
        self.cat.add("v:new2", shared, cid="ambiguous-CID")
        self.assertEqual(cm.recover_key_map(self.data, backup), {})


class TheBackupIsWalSafe(MigrationCase):
    def test_a_snapshot_taken_with_an_open_wal_holds_the_committed_rows(self):
        """The R04 reproduction: copy2 produced `no such table: items`."""
        db = self.data / "catalog.db"
        writer = sqlite3.connect(db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO items VALUES('v:wal','/x','video',NULL)")
        writer.commit()                       # committed, still in -wal

        dest = cm.backup_catalog(db)
        con = sqlite3.connect(dest)
        try:
            rows = [r[0] for r in con.execute("SELECT content_key FROM items")]
        finally:
            con.close()
        self.assertIn("v:wal", rows)

    def test_a_rollback_journal_catalog_backs_up_too(self):
        db = self.data / "delete.db"
        _Catalog(db, wal=False)
        con = sqlite3.connect(db)
        con.execute("INSERT INTO items VALUES('v:one','/x','video',NULL)")
        con.commit()
        con.close()
        dest = cm.backup_catalog(db)
        con = sqlite3.connect(dest)
        try:
            self.assertEqual([r[0] for r in con.execute(
                "SELECT content_key FROM items")], ["v:one"])
        finally:
            con.close()

    def test_two_attempts_in_the_same_second_do_not_share_a_file(self):
        db = self.data / "catalog.db"
        first, second = cm.backup_catalog(db), cm.backup_catalog(db)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file() and second.is_file())

    def test_an_unverifiable_backup_stops_before_any_change(self):
        m = self.media("clip.webm")
        self.cat.add("v:old", m)
        with mock.patch.object(cm, "_verify_backup",
                               side_effect=RuntimeError("torn snapshot")):
            with self.fake_fingerprint({"clip.webm": ("v:new", 1)}):
                with self.assertRaises(RuntimeError):
                    ir.migrate(self.data, apply=True)
        self.assertEqual(self.cat.col("items"), ["v:old"])

    def test_the_restored_copy_equals_the_pre_migration_catalog(self):
        m = self.media("clip.webm")
        self.cat.add("v:old", m, cid="cid-1", fuid="F-1")
        before = {t: sorted(self.cat.col(t)) for t, _c in cm.KEY_REFERENCES}
        with self.fake_fingerprint({"clip.webm": ("v:new", 1)}):
            ir.migrate(self.data, apply=True)
        backups = sorted(self.data.glob("catalog.before-video-identity-*.db"))
        self.assertEqual(len(backups), 1)
        restored = _Catalog.__new__(_Catalog)
        restored.path = backups[0]
        self.assertEqual({t: sorted(restored.col(t)) for t, _c in cm.KEY_REFERENCES},
                         before)


class AnInterruptedMigrationIsResumable(MigrationCase):
    """Crash injection between each durable stage.

    JSON replacement and a SQL COMMIT are not one transaction, so the design
    does not claim atomicity -- it claims every stage is idempotent and the
    journal records how far the run got. These prove both.
    """

    def setup_pending(self):
        m = self.media("clip.webm")
        self.cat.add("v:old", m, cid="cid-1", fuid="F-1")
        self.plan("pk", {"mixed": ["v:old"]})
        return m

    def crash_after(self, stage: str):
        real = cm._write_journal

        def boom(data_dir, doc):
            real(data_dir, doc)
            if doc.get("stage") == stage:
                raise KeyboardInterrupt(f"crash after {stage}")
        return mock.patch.object(cm, "_write_journal", boom)

    def _resume_and_assert_complete(self):
        with self.fake_fingerprint({"clip.webm": ("v:new", 1)}):
            self.assertEqual(ir.migrate(self.data, apply=True), ir.EXIT_OK)
        self.assertEqual(self.cat.col("items"), ["v:new"])
        self.assertEqual(json.loads((self.data / "publish_plan_pk.json")
                                    .read_text(encoding="utf-8")),
                         {"mixed": ["v:new"]})
        self.assertIsNone(cm.read_journal(self.data), "the journal outlived the run")

    def test_a_crash_after_the_backup_resumes(self):
        self.setup_pending()
        with self.crash_after("backup"), self.fake_fingerprint(
                {"clip.webm": ("v:new", 1)}):
            with self.assertRaises(KeyboardInterrupt):
                ir.migrate(self.data, apply=True)
        self.assertIsNotNone(cm.read_journal(self.data))
        self._resume_and_assert_complete()

    def test_a_crash_after_the_database_stage_resumes(self):
        self.setup_pending()
        with self.crash_after("database"), self.fake_fingerprint(
                {"clip.webm": ("v:new", 1)}):
            with self.assertRaises(KeyboardInterrupt):
                ir.migrate(self.data, apply=True)
        # The tables moved; the plan did not. That is the half-state the
        # journal exists to make visible.
        self.assertEqual(self.cat.col("items"), ["v:new"])
        self.assertEqual(json.loads((self.data / "publish_plan_pk.json")
                                    .read_text(encoding="utf-8")),
                         {"mixed": ["v:old"]})
        self._resume_and_assert_complete()

    def test_a_crash_after_the_state_stage_resumes(self):
        self.setup_pending()
        with self.crash_after("state"), self.fake_fingerprint(
                {"clip.webm": ("v:new", 1)}):
            with self.assertRaises(KeyboardInterrupt):
                ir.migrate(self.data, apply=True)
        self._resume_and_assert_complete()

    def test_report_names_an_interrupted_migration(self):
        self.setup_pending()
        with self.crash_after("database"), self.fake_fingerprint(
                {"clip.webm": ("v:new", 1)}):
            with self.assertRaises(KeyboardInterrupt):
                ir.migrate(self.data, apply=True)
        with self.fake_fingerprint({"clip.webm": ("v:new", 1)}):
            self.assertEqual(ir.report(self.data), ir.EXIT_STALE)

    def test_an_unreadable_journal_is_not_deleted_blindly(self):
        cm.journal_path(self.data).write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            cm.read_journal(self.data)
        self.assertTrue(cm.journal_path(self.data).is_file())


if __name__ == "__main__":
    unittest.main()
