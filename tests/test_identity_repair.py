"""The migration that moves catalog rows when a content key's definition changes.

Correcting the video decoder (F12/F13) changes what a video's content key IS,
and that key is the project's primary identity: the row in `items`, the foreign
key in `publications` and `seen_files`, and the first twelve characters of every
archived filename. Shipping the decoder without moving the rows leaves a catalog
that no longer dedups its own media.

The properties that matter are the refusals, so those are what these pin: a
collision must stop the migration rather than merge two rows (merging deletes
media, which is the very defect the decoder fix exists to prevent), an
undecodable file must stop it rather than half-convert, and `--apply` must be
required before anything is written.

No ffmpeg here: the recompute is injected. What is under test is the bookkeeping
around it, and making these encode real video would buy nothing and cost
minutes.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._panel_fixtures import ROOT

sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "scripts"))
import identity_repair as ir


class _Catalog:
    """A catalog with the three tables that store a content key."""

    def __init__(self, path: Path):
        self.path = path
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE items(content_key TEXT PRIMARY KEY, file_path TEXT,"
            " format TEXT, phash INTEGER);"
            "CREATE TABLE publications(base TEXT, content_key TEXT,"
            " set_name TEXT, custom_emoji_id TEXT);"
            "CREATE TABLE seen_files(file_unique_id TEXT PRIMARY KEY,"
            " content_key TEXT);")
        con.commit()
        con.close()

    def add(self, key, path, fmt="video", phash=None, cid=None, fuid=None):
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO items VALUES(?,?,?,?)", (key, str(path), fmt, phash))
        if cid:
            con.execute("INSERT INTO publications VALUES(?,?,?,?)",
                        ("base", key, "set1", cid))
        if fuid:
            con.execute("INSERT INTO seen_files VALUES(?,?)", (fuid, key))
        con.commit()
        con.close()

    def rows(self, table, column="content_key"):
        con = sqlite3.connect(self.path)
        try:
            return [r[0] for r in con.execute(f"SELECT {column} FROM {table}")]
        finally:
            con.close()

    def publication_of(self, key):
        con = sqlite3.connect(self.path)
        try:
            row = con.execute(
                "SELECT set_name, custom_emoji_id FROM publications "
                "WHERE content_key=?", (key,)).fetchone()
            return row
        finally:
            con.close()


class MigrationBookkeeping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.cat = _Catalog(self.data / "catalog.db")
        self.media = self.data / "clip.webm"
        self.media.write_bytes(b"\x1a\x45\xdf\xa3video")
        self.media2 = self.data / "clip2.webm"
        self.media2.write_bytes(b"\x1a\x45\xdf\xa3other")

    def tearDown(self):
        self.tmp.cleanup()

    def fake_keys(self, mapping):
        """Pretend the corrected decoder returns these keys, by file name."""
        def compute(path, fmt):
            name = Path(path).name
            if name not in mapping:
                raise RuntimeError(f"undecodable: {name}")
            return mapping[name]
        return mock.patch.object(ir.identity, "content_key", compute)

    def test_apply_is_required_before_anything_is_written(self):
        self.cat.add("v:old", self.media, cid="cid-1", fuid="F-1")
        with self.fake_keys({"clip.webm": "v:new"}):
            code = ir.migrate(self.data, apply=False)
        self.assertEqual(code, ir.EXIT_STALE)
        self.assertEqual(self.cat.rows("items"), ["v:old"])

    def test_every_reference_moves_together(self):
        """A publication left pointing at a key that no longer exists is worse
        than not migrating at all."""
        self.cat.add("v:old", self.media, cid="cid-1", fuid="F-1")
        with self.fake_keys({"clip.webm": "v:new"}):
            code = ir.migrate(self.data, apply=True)
        self.assertEqual(code, ir.EXIT_OK)
        self.assertEqual(self.cat.rows("items"), ["v:new"])
        self.assertEqual(self.cat.rows("publications"), ["v:new"])
        self.assertEqual(self.cat.rows("seen_files"), ["v:new"])

    def test_the_telegram_identifiers_are_preserved(self):
        """The custom_emoji_id is what every bot inventory already holds. A
        migration that moved a key and dropped the id would be a silent
        republish of the whole pack."""
        self.cat.add("v:old", self.media, cid="cid-keepme", fuid="F-1")
        with self.fake_keys({"clip.webm": "v:new"}):
            ir.migrate(self.data, apply=True)
        self.assertEqual(self.cat.publication_of("v:new"), ("set1", "cid-keepme"))

    def test_a_backup_is_written_before_any_change(self):
        self.cat.add("v:old", self.media, cid="cid-1")
        with self.fake_keys({"clip.webm": "v:new"}):
            ir.migrate(self.data, apply=True)
        backups = list(self.data.glob("catalog.before-video-identity-*.db"))
        self.assertEqual(len(backups), 1)
        con = sqlite3.connect(backups[0])
        try:
            self.assertEqual([r[0] for r in con.execute(
                "SELECT content_key FROM items")], ["v:old"])
        finally:
            con.close()

    def test_a_collision_refuses_instead_of_merging(self):
        """Two rows onto one key means one of them loses its media -- exactly
        the failure the decoder fix exists to stop."""
        self.cat.add("v:a", self.media, cid="cid-a")
        self.cat.add("v:b", self.media2, cid="cid-b")
        with self.fake_keys({"clip.webm": "v:same", "clip2.webm": "v:same"}):
            code = ir.migrate(self.data, apply=True)
        self.assertEqual(code, ir.EXIT_FAILED)
        self.assertEqual(sorted(self.cat.rows("items")), ["v:a", "v:b"])

    def test_colliding_with_a_row_that_is_not_moving_also_refuses(self):
        self.cat.add("v:taken", self.media2, fmt="static")
        self.cat.add("v:old", self.media, cid="cid-1")
        with self.fake_keys({"clip.webm": "v:taken"}):
            code = ir.migrate(self.data, apply=True)
        self.assertEqual(code, ir.EXIT_FAILED)
        self.assertEqual(sorted(self.cat.rows("items")), ["v:old", "v:taken"])

    def test_an_undecodable_file_stops_the_whole_migration(self):
        """Half a conversion is a catalog where some keys describe the file and
        some do not, with nothing recording which is which."""
        self.cat.add("v:a", self.media, cid="cid-a")
        self.cat.add("v:b", self.media2, cid="cid-b")
        with self.fake_keys({"clip.webm": "v:new"}):     # clip2 raises
            code = ir.migrate(self.data, apply=True)
        self.assertEqual(code, ir.EXIT_FAILED)
        self.assertEqual(sorted(self.cat.rows("items")), ["v:a", "v:b"])

    def test_an_unchanged_catalog_is_a_no_op(self):
        self.cat.add("v:same", self.media, cid="cid-1")
        with self.fake_keys({"clip.webm": "v:same"}):
            code = ir.migrate(self.data, apply=True)
        self.assertEqual(code, ir.EXIT_OK)
        self.assertEqual(list(self.data.glob("catalog.before-*.db")), [])

    def test_missing_media_is_skipped_not_guessed_at(self):
        """A row whose file is gone cannot have its key recomputed. It keeps
        the key it has rather than being invented or dropped."""
        self.cat.add("v:gone", self.data / "absent.webm", cid="cid-1")
        self.cat.add("v:old", self.media, cid="cid-2")
        with self.fake_keys({"clip.webm": "v:new"}):
            code = ir.migrate(self.data, apply=True)
        self.assertEqual(code, ir.EXIT_OK)
        self.assertEqual(sorted(self.cat.rows("items")), ["v:gone", "v:new"])


class TheReportChangesNothing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.cat = _Catalog(self.data / "catalog.db")
        self.media = self.data / "clip.webm"
        self.media.write_bytes(b"\x1a\x45\xdf\xa3video")

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_never_writes(self):
        self.cat.add("v:old", self.media, cid="cid-1", fuid="F-1")
        before = (self.data / "catalog.db").read_bytes()
        def compute(path, fmt):
            return "v:new"
        with mock.patch.object(ir.identity, "content_key", compute):
            code = ir.report(self.data)
        self.assertEqual(code, ir.EXIT_STALE, "pending work must not exit 0")
        self.assertEqual((self.data / "catalog.db").read_bytes(), before)
        self.assertEqual(list(self.data.glob("catalog.before-*.db")), [])

    def test_a_catalog_already_in_step_reports_clean(self):
        self.cat.add("v:same", self.media)
        def compute(path, fmt):
            return "v:same"
        with mock.patch.object(ir.identity, "content_key", compute):
            self.assertEqual(ir.report(self.data), ir.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
