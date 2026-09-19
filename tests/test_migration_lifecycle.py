"""A03-A06: ownership, evidenced replay, and application-level rollback.

Only the decoder is faked here. SQLite, files, CLI dispatch and native locks
remain real; one tiny fixture covers the cross-resource failure boundaries.
"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib import error, request

from emojikit import collection_migrate as cm
from emojikit import packstate
from emojikit.collection_state import _lock_path
from tests.test_identity_migration import MigrationCase
from scripts import identity_repair as ir
from emojikit import migration_bundle as bundle
from emojikit.catalog import Catalog
from emojikit.maintenance import maintenance


class MigrationCannotCertifyDamage(MigrationCase):
    def pending(self):
        src = self.media("007_video_aaaaaaaaaaaa.webm")
        self.cat.add("v:aaaaaaaaaaaaaaaa", src, cid="CID", fuid="FUID")
        self.state("audit", {"sets": [{"keys": ["v:aaaaaaaaaaaaaaaa"]}]})
        self.plan("audit", {"mixed": ["v:aaaaaaaaaaaaaaaa"]})
        return src, src.with_name("007_video_bbbbbbbbbbbb.webm")

    def decode(self):
        return self.fake_fingerprint({
            "007_video_aaaaaaaaaaaa.webm": ("v:bbbbbbbbbbbbbbbb", 7),
            "007_video_bbbbbbbbbbbb.webm": ("v:bbbbbbbbbbbbbbbb", 7),
        })

    def cli(self, *args):
        with mock.patch.object(ir, "setup_logging"), contextlib.redirect_stdout(io.StringIO()):
            return ir.main([*args, "--data-dir", str(self.data)])

    def test_actual_publisher_lock_prevents_migration(self):
        src, dest = self.pending()
        with packstate.exclusive_lock(_lock_path(self.data, "audit")), self.decode():
            with self.assertRaises(packstate.LockBusy):
                cm.apply_migration(self.data, cm.survey(self.data))
        self.assertTrue(src.is_file())
        self.assertFalse(dest.exists())
        self.assertEqual(self.cat.col("items"), ["v:aaaaaaaaaaaaaaaa"])

    def test_unrelated_rename_destination_is_never_overwritten(self):
        src, dest = self.pending()
        dest.write_bytes(b"unrelated sentinel")
        with self.decode():
            try:
                result = self.cli("migrate-video-keys", "--apply")
            except RuntimeError:
                result = ir.EXIT_FAILED
        self.assertEqual(result, ir.EXIT_FAILED)
        self.assertEqual(dest.read_bytes(), b"unrelated sentinel")
        self.assertTrue(src.is_file())
        self.assertEqual(self.cat.col("items"), ["v:aaaaaaaaaaaaaaaa"])

    def test_required_stale_manifest_cannot_be_called_plan_noise(self):
        media = self.media("clip.webm")
        self.cat.add("v:correct", media, phash=7)
        self.state("audit", {"sets": [{"keys": ["v:lost"]}]})
        with self.fake_fingerprint({"clip.webm": ("v:correct", 7)}):
            self.assertEqual(self.cli("migrate-video-keys", "--apply"), ir.EXIT_FAILED)
            self.assertNotEqual(self.cli("report"), ir.EXIT_OK)
        self.assertEqual(json.loads((self.data / "publish_audit.json").read_text(
            encoding="utf-8"))["sets"][0]["keys"], ["v:lost"])

    def test_late_missing_media_retains_journal_and_refuses_success(self):
        src, dest = self.pending()
        apply_files = cm._apply_files

        def remove_after_apply(*args, **kwargs):
            result = apply_files(*args, **kwargs)
            dest.unlink()
            return result

        with self.decode(), mock.patch.object(cm, "_apply_files", remove_after_apply):
            try:
                result = self.cli("migrate-video-keys", "--apply")
            except RuntimeError:
                result = ir.EXIT_FAILED
        self.assertEqual(result, ir.EXIT_FAILED)
        self.assertTrue(cm.journal_path(self.data).is_file())
        self.assertFalse(src.exists())

    def test_completed_migration_restores_files_state_and_identifiers(self):
        src, dest = self.pending()
        before = bundle.signature(self.data / "catalog.db")
        original = src.read_bytes()
        with self.decode():
            self.assertEqual(self.cli("migrate-video-keys", "--apply"), ir.EXIT_OK)
        manifest = next(self.data.glob("*.rollback.json"))
        self.assertEqual(self.cli("restore", "--bundle", str(manifest), "--apply"), ir.EXIT_OK)
        self.assertEqual(bundle.signature(self.data / "catalog.db"), before)
        self.assertEqual(src.read_bytes(), original)
        self.assertFalse(dest.exists())
        self.assertEqual(self.cat.col("publications", "custom_emoji_id"), ["CID"])
        self.assertEqual(self.cat.col("seen_files", "file_unique_id"), ["FUID"])
        self.assertEqual(json.loads((self.data / "publish_audit.json").read_text(
            encoding="utf-8"))["sets"][0]["keys"], ["v:aaaaaaaaaaaaaaaa"])
        self.assertEqual(self.cli("restore", "--bundle", str(manifest), "--apply"), ir.EXIT_OK)

    def test_restore_preserves_an_identical_preexisting_destination(self):
        src, dest = self.pending()
        dest.write_bytes(src.read_bytes())
        with self.decode():
            self.assertEqual(self.cli("migrate-video-keys", "--apply"), ir.EXIT_OK)
        manifest = next(self.data.glob("*.rollback.json"))
        self.assertEqual(self.cli("restore", "--bundle", str(manifest), "--apply"), ir.EXIT_OK)
        self.assertEqual(src.read_bytes(), dest.read_bytes())

    def test_restore_refuses_later_catalog_or_file_changes(self):
        src, dest = self.pending()
        with self.decode():
            self.assertEqual(self.cli("migrate-video-keys", "--apply"), ir.EXIT_OK)
        manifest = next(self.data.glob("*.rollback.json"))
        with contextlib.closing(sqlite3.connect(self.data / "catalog.db")) as con, con:
            con.execute("UPDATE publications SET custom_emoji_id='LATER'")
        self.assertEqual(self.cli("restore", "--bundle", str(manifest), "--apply"), ir.EXIT_FAILED)
        self.assertEqual(self.cat.col("publications", "custom_emoji_id"), ["LATER"])
        self.assertTrue(dest.is_file())
        self.assertFalse(src.exists())

    def test_journal_bound_to_another_directory_refuses_without_mutation(self):
        src, dest = self.pending()
        original = cm._write_journal

        def stop(data, doc):
            original(data, doc)
            raise KeyboardInterrupt()

        with self.decode(), mock.patch.object(cm, "_write_journal", stop), self.assertRaises(KeyboardInterrupt):
            self.cli("migrate-video-keys", "--apply")
        doc = cm.read_journal(self.data)
        doc["data_dir"] = str(self.data / "elsewhere")
        packstate.write_json_atomic(cm.journal_path(self.data), doc)
        self.assertEqual(self.cli("migrate-video-keys", "--apply"), ir.EXIT_FAILED)
        self.assertTrue(src.is_file())
        self.assertFalse(dest.exists())

    def test_restore_after_a_move_before_database_path_commit(self):
        src, dest = self.pending()
        move = bundle.move_file

        def stop(*args, **kwargs):
            move(*args, **kwargs)
            raise KeyboardInterrupt()

        with self.decode(), mock.patch.object(bundle, "move_file", stop), self.assertRaises(KeyboardInterrupt):
            self.cli("migrate-video-keys", "--apply")
        self.assertFalse(src.exists())
        self.assertEqual(self.cat.col("items", "file_path"), [str(src)])
        manifest = next(self.data.glob("*.rollback.json"))
        self.assertEqual(self.cli("restore", "--bundle", str(manifest), "--apply"), ir.EXIT_OK)
        self.assertTrue(src.is_file())
        self.assertFalse(dest.exists())
        self.assertEqual(self.cat.col("items"), ["v:aaaaaaaaaaaaaaaa"])

    def test_existing_link_after_interruption_is_replayed_without_losing_bytes(self):
        src, dest = self.pending()
        original = src.read_bytes()
        link = bundle.os.link

        def stop(*args, **kwargs):
            link(*args, **kwargs)
            raise KeyboardInterrupt()

        with self.decode(), mock.patch.object(bundle.os, "link", stop), self.assertRaises(KeyboardInterrupt):
            self.cli("migrate-video-keys", "--apply")
        self.assertEqual(src.read_bytes(), dest.read_bytes())
        with self.decode():
            self.assertEqual(self.cli("migrate-video-keys", "--apply"), ir.EXIT_OK)
        self.assertFalse(src.exists())
        # Migration stores resolved paths. Windows TEMP may use RUNNER~1 while
        # resolve() records its long runneradmin spelling; these name one file.
        paths = self.cat.col("items", "file_path")
        self.assertEqual(paths, [str(dest.resolve())])
        self.assertTrue(Path(paths[0]).samefile(dest))
        self.assertEqual(Path(paths[0]).read_bytes(), original)

    def test_partial_legacy_migration_requires_full_backup_coverage(self):
        src, dest = self.pending()
        original_backup = cm.backup_catalog(self.data / "catalog.db")
        cm._apply_database(self.data / "catalog.db", {"v:aaaaaaaaaaaaaaaa": "v:bbbbbbbbbbbbbbbb"},
                           {"v:bbbbbbbbbbbbbbbb": 7})
        insufficient = cm.backup_catalog(self.data / "catalog.db")
        with self.decode():
            self.assertEqual(self.cli("migrate-video-keys", "--apply", "--from-backup", str(insufficient)),
                             ir.EXIT_FAILED)
            self.assertTrue(src.is_file())
            self.assertEqual(self.cli("migrate-video-keys", "--apply", "--from-backup", str(original_backup)),
                             ir.EXIT_OK)
            self.assertEqual(self.cli("report"), ir.EXIT_OK)
        self.assertTrue(dest.is_file())
        self.assertFalse(src.exists())

    def test_late_decode_failure_cannot_retire_the_journal(self):
        self.pending()
        apply_files = cm._apply_files

        def lose_decoder(*args):
            result = apply_files(*args)
            cm.identity.fingerprint = mock.Mock(side_effect=cm.MediaError("decoder lost"))
            return result

        with self.decode(), mock.patch.object(cm, "_apply_files", lose_decoder):
            self.assertEqual(self.cli("migrate-video-keys", "--apply"), ir.EXIT_FAILED)
        self.assertTrue(cm.journal_path(self.data).is_file())

    def test_orphaned_identifiers_prevent_a_clean_report_or_noop(self):
        self.cat.add("v:correct", self.media("clip.webm"), phash=7)
        with contextlib.closing(sqlite3.connect(self.data / "catalog.db")) as con, con:
            con.execute("INSERT INTO seen_files VALUES('FOREIGN', 'v:absent')")
        with self.fake_fingerprint({"clip.webm": ("v:correct", 7)}):
            self.assertEqual(self.cli("report"), ir.EXIT_FAILED)
            self.assertEqual(self.cli("migrate-video-keys", "--apply"), ir.EXIT_FAILED)

    def test_cli_recovers_after_each_state_replacement_and_journal_retirement(self):
        for point in ("database-before", "database-after", "publish_audit.json",
                      "publish_plan_audit.json", "verified"):
            with self.subTest(point=point), tempfile.TemporaryDirectory(dir=self.data) as raw:
                previous_data, previous_cat = self.data, self.cat
                self.data = Path(raw)
                self.cat = type(previous_cat)(self.data / "catalog.db")
                try:
                    src, dest = self.pending()
                    original_write = packstate.write_json_atomic
                    original_database = cm._apply_database

                    def fail_write(path, doc, original_write=original_write, point=point):
                        original_write(path, doc)
                        if Path(path).name == point or (point == "verified" and
                                Path(path).name == cm.JOURNAL_NAME and doc.get("stage") == "verified"):
                            raise KeyboardInterrupt()

                    def fail_database(*args, point=point, original_database=original_database, **kwargs):
                        if point == "database-before":
                            raise KeyboardInterrupt()
                        result = original_database(*args, **kwargs)
                        if point == "database-after":
                            raise KeyboardInterrupt()
                        return result

                    with self.decode(), mock.patch.object(packstate, "write_json_atomic", fail_write), \
                            mock.patch.object(cm, "_apply_database", fail_database), \
                            self.assertRaises(KeyboardInterrupt):
                        self.cli("migrate-video-keys", "--apply")
                    with self.decode():
                        self.assertEqual(self.cli("migrate-video-keys", "--apply"), ir.EXIT_OK)
                        self.assertEqual(self.cli("report"), ir.EXIT_OK)
                    self.assertTrue(dest.is_file())
                    self.assertFalse(src.exists())
                finally:
                    self.data, self.cat = previous_data, previous_cat

    def test_a_second_catalog_connection_cannot_write_during_migration(self):
        self.pending()
        apply_files = cm._apply_files

        def attempt_writer(*args):
            with self.assertRaises(packstate.LockBusy):
                with Catalog(self.data / "catalog.db") as cat:
                    cat.mark_uploaded("v:aaaaaaaaaaaaaaaa", "WRONG", base="newfamily")
            return apply_files(*args)

        with self.decode(), mock.patch.object(cm, "_apply_files", attempt_writer):
            self.assertEqual(self.cli("migrate-video-keys", "--apply"), ir.EXIT_OK)
        self.assertEqual(self.cat.col("publications", "custom_emoji_id"), ["CID"])

    def test_native_death_after_second_move_replays_via_cli(self):
        for i in range(3):
            key = "v:" + f"{i:02x}" * 16
            path = self.data / f"{i:03d}_video_{key[2:14]}.webm"
            path.write_bytes(b"\x1a\x45\xdf\xa3fixture" + bytes([i]))
            self.cat.add(key, path, cid=f"CID{i}", fuid=f"FUID{i}")
        args = [sys.executable, "-m", "tests._migration_process"]
        death = subprocess.run([*args, "die-after-second-move", str(self.data)],
                               capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(death.returncode, 73, death.stdout + death.stderr)
        self.assertTrue(cm.journal_path(self.data).is_file())
        replay = subprocess.run([*args, "migrate", str(self.data)],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(replay.returncode, ir.EXIT_OK, replay.stdout + replay.stderr)
        self.assertFalse(cm.journal_path(self.data).exists())
        self.assertEqual(sorted(self.cat.col("items")), ["v:" + f"{i:02x}" * 16 for i in (16, 17, 18)])
        self.assertTrue(all(Path(path).is_file() for path in self.cat.col("items", "file_path")))


class ActualWriterOwnership(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        with Catalog(self.data / "catalog.db"):
            pass

    def test_first_family_publisher_and_catalog_exclude_migration_in_real_children(self):
        for kind in ("hold-publisher", "hold-catalog", "hold-archive", "hold-panel", "hold-ingest"):
            with self.subTest(kind=kind):
                child = subprocess.Popen([sys.executable, "-m", "tests._migration_process", kind,
                                          str(self.data)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, encoding="utf-8")
                ready = threading.Event()
                lines = []

                def read_ready(lines=lines, child=child, ready=ready):
                    lines.append(child.stdout.readline())
                    ready.set()

                thread = threading.Thread(target=read_ready, daemon=True)
                thread.start()
                try:
                    self.assertTrue(ready.wait(10), "writer did not reach its owned entry point")
                    self.assertEqual(lines, ["READY\n"])
                    with self.assertRaises(packstate.LockBusy):
                        cm.apply_migration(self.data)
                    self.assertFalse(cm.journal_path(self.data).exists())
                finally:
                    try:
                        _, stderr = child.communicate("release\n", timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.communicate(timeout=5)
                        raise
                    thread.join(timeout=5)
                self.assertEqual(child.returncode, 0, stderr)

    def test_pending_journal_blocks_ingest_archive_publisher_and_panel_save(self):
        from emojikit import add_media
        from emojikit import build_collection
        from emojikit import pack_archive
        from emojikit import panel
        from PIL import Image

        image = self.data / "incoming.png"
        Image.new("RGBA", (100, 100), "red").save(image)
        cm.journal_path(self.data).write_text("{}", encoding="utf-8")
        with self.assertRaises(packstate.LockBusy):
            add_media.main([str(image), "--data-dir", str(self.data)])
        self.assertNotEqual(build_collection.main(["--base", "newfamily", "--title", "Fixture",
            "--dry-run", "--no-brand-logo", "--data-dir", str(self.data)]), 0)
        with mock.patch.object(pack_archive, "CATALOG", self.data / "catalog.db"), \
                self.assertRaises(packstate.LockBusy):
            pack_archive.sync(None)
        handler = panel.make_handler([], {}, self.data / "catalog.db", "fixture-token")
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            req = request.Request(f"http://127.0.0.1:{server.server_port}/api/save",
                                  data=b'{"excluded":[],"known":[]}',
                                  headers={"Content-Type": "application/json", "X-Panel-Token": "fixture-token"})
            with self.assertRaises(error.HTTPError) as caught:
                request.urlopen(req, timeout=5)
            self.assertEqual(caught.exception.code, 503)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertTrue(image.is_file())
        with contextlib.closing(sqlite3.connect(self.data / "catalog.db")) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)

    def test_canonical_alias_and_second_migration_use_the_same_native_lock(self):
        ready, release = threading.Event(), threading.Event()

        def own():
            with maintenance(self.data):
                ready.set()
                release.wait(10)

        thread = threading.Thread(target=own, daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(5))
            alias = self.data / "nested" / ".."
            (self.data / "nested").mkdir()
            with self.assertRaises(packstate.LockBusy):
                cm.apply_migration(alias)
            with self.assertRaises(packstate.LockBusy):
                Catalog(self.data / "catalog.db")
        finally:
            release.set()
            thread.join(timeout=5)

    def test_real_catalog_order_and_unchanged_pack_resume_survive_migration_and_restore(self):
        from emojikit.collection_reconcile import reconcile_set
        from tests._migration_process import _decode
        keys = ["v:" + f"{i:02x}" * 16 for i in range(3)]
        with Catalog(self.data / "catalog.db") as cat:
            for i, key in enumerate(keys):
                path = self.data / f"{i:03d}_video_{key[2:14]}.webm"
                path.write_bytes(b"\x1a\x45\xdf\xa3fixture" + bytes([i]))
                cat.add(content_key=key, fmt="video", file_path=path, phash=i)
                cat.mark_uploaded(key, f"CID{i}", base="audit", set_name="audit1")
                cat.record_file_unique_id(f"FUID{i}", key)
            cat.set_order(keys[::-1])
            cat.set_inclusion({keys[1]})
        rec = {"name": "audit1", "index": 1, "fmt": "video", "live": 3, "keys": keys[::-1]}
        state_path = self.data / "publish_audit.json"
        packstate.write_json_atomic(state_path, {"base": "audit", "sets": [rec]})
        packstate.write_json_atomic(self.data / "publish_plan_audit.json", {"video": keys[::-1]})

        class Service:
            def get_sticker_set(self, name):
                return {"stickers": [{"file_unique_id": f"FUID{i}", "custom_emoji_id": f"CID{i}"}
                                     for i in (2, 1, 0)]}

        with mock.patch.object(cm.identity, "fingerprint", _decode), mock.patch.object(ir, "setup_logging"):
            self.assertEqual(ir.main(["migrate-video-keys", "--apply", "--data-dir", str(self.data)]), 0)
        with Catalog(self.data / "catalog.db") as cat:
            migrated_rec = json.loads(state_path.read_text(encoding="utf-8"))["sets"][0]
            self.assertEqual(reconcile_set(Service(), cat, migrated_rec, self.data, "audit"), 3)
            self.assertEqual([item.included for item in cat.all_items()], [True, False, True])
        manifest = next(self.data.glob("*.rollback.json"))
        with mock.patch.object(ir, "setup_logging"):
            self.assertEqual(ir.main(["restore", "--apply", "--bundle", str(manifest),
                                      "--data-dir", str(self.data)]), 0)
        with Catalog(self.data / "catalog.db") as cat:
            self.assertEqual([item.content_key for item in cat.all_items()], keys[::-1])
            self.assertEqual([item.included for item in cat.all_items()], [True, False, True])
            self.assertEqual(reconcile_set(Service(), cat, rec, self.data, "audit"), 3)
