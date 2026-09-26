"""The archive: when a pack earns one, and when it has stopped being true.

Two real failures shaped these. A publish round left 347 files inside the
project because nothing checked; and a pack that was still being filled got
archived, which is worse -- the filename carries the emoji's SLOT, so the folder
started lying the moment the pack was reordered.
"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from emojikit import pack_archive as pa

KEY = "s:" + "a" * 32
ARCHIVED_NAME = "002_static_" + "a" * 12 + ".webp"


class _Env:
    """A project + archive pair, wired into the module under test.

    ``archived`` puts the media where a synced pack would have it; otherwise it
    stays in the project, which is its own kind of stale.
    """

    def __init__(self, stack, *, live, cid="1", folder=True, meta=True,
                 ids=None, archived=True):
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.archive = tmp / "archive"
        self.folder = self.archive / "Pack One"
        self.folder.mkdir(parents=True)

        media = (self.folder / ARCHIVED_NAME) if archived else (tmp / "collection" / "media" / "x.webp")
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"x")

        db = tmp / "catalog.db"
        con = sqlite3.connect(db)
        con.executescript(
            "CREATE TABLE items(content_key TEXT, file_path TEXT, format TEXT, keywords TEXT);"
            "CREATE TABLE publications(base TEXT, content_key TEXT, set_name TEXT, "
            "custom_emoji_id TEXT);")
        con.execute("INSERT INTO items VALUES(?,?,?,?)",
                    (KEY, str(media), "static", json.dumps(["lock"])))
        con.execute("INSERT INTO publications VALUES(?,?,?,?)", (pa._base(), KEY, "set1", cid))
        con.commit()
        con.close()

        state = tmp / "state.json"
        state.write_text(json.dumps({"sets": [
            {"name": "set1", "title": "Pack One", "index": 1, "live": live, "fmt": "mixed"}]}),
            encoding="utf-8")

        if meta:
            (self.folder / pa.LOGO_NAME).write_bytes(b"logo")
            (self.folder / "_history.md").write_text("x", encoding="utf-8")
            (self.folder / "_manifest.md").write_text("x", encoding="utf-8")
            (self.folder / "_history.json").write_text(
                json.dumps({"emoji": [{"premium_id": i} for i in (ids or [cid])]}),
                encoding="utf-8")
        if not folder:
            for p in sorted(self.folder.rglob("*"), reverse=True):
                p.unlink()
            self.folder.rmdir()

        for p in (patch.object(pa, "CATALOG", db), patch.object(pa, "_state_file", lambda: state),
                  patch.object(pa, "archive_root", lambda: self.archive)):
            stack.enter_context(p)


class OnlyAFinishedPackIsArchived(unittest.TestCase):
    def test_a_pack_still_being_filled_must_own_no_folder(self):
        """The filename carries the SLOT. A pack under capacity can still be
        reordered, and then every name in its folder is wrong -- so the archive
        waits rather than recording a number that is about to change."""
        with ExitStack() as stack:
            _Env(stack, live=146)
            stale, why = pa.check()
        self.assertTrue(stale)
        self.assertIn("not full", why[0])

    def test_a_full_pack_that_was_never_archived_is_stale(self):
        with ExitStack() as stack:
            _Env(stack, live=pa.PER_SET, folder=False, archived=False)
            stale, why = pa.check()
        self.assertTrue(stale)
        self.assertIn("never archived", why[0])

    def test_a_half_full_pack_with_no_folder_is_simply_fine(self):
        """The common case: pack 5 sits at 146 and must raise nothing at all."""
        with ExitStack() as stack:
            _Env(stack, live=146, folder=False, archived=False)
            self.assertEqual(pa.check(), (False, []))

    def test_a_synced_full_pack_is_fresh(self):
        with ExitStack() as stack:
            _Env(stack, live=pa.PER_SET)
            self.assertEqual(pa.check(), (False, []))


class TheArchiveHasToFollowThePack(unittest.TestCase):
    def test_a_replaced_id_makes_the_archive_stale(self):
        """A recolour mints a NEW custom_emoji_id. The files are still right, so
        only the recorded ids can reveal it -- and a stale id is exactly what a
        bot inventory copied out of `_manifest.md` would keep sending."""
        with ExitStack() as stack:
            _Env(stack, live=pa.PER_SET, cid="NEW", ids=["OLD"])
            stale, why = pa.check()
        self.assertTrue(stale)
        self.assertEqual(len(why), 1, why)
        self.assertIn("id(s) added/changed", why[0])

    def test_media_still_inside_the_project_is_stale(self):
        """The whole point: the project must not keep a copy of what shipped."""
        with ExitStack() as stack:
            _Env(stack, live=pa.PER_SET, archived=False)
            stale, why = pa.check()
        self.assertTrue(stale)
        self.assertTrue(any("still in the project" in w for w in why), why)

    def test_a_missing_metadata_file_is_stale(self):
        with ExitStack() as stack:
            _Env(stack, live=pa.PER_SET, meta=False)
            stale, why = pa.check()
        self.assertTrue(stale)
        self.assertTrue(any(pa.LOGO_NAME in w for w in why), why)


class TheNameCarriesTheSlot(unittest.TestCase):
    def test_the_filename_matches_the_convention_already_on_disk(self):
        """594 files were archived under this shape before any code existed; a
        different one would orphan every last of them."""
        n = pa.archive_name(7, "animated", "a:169f06002d30a194aa2a4d6ab7cabd87", ".tgs")
        self.assertEqual(n, "007_animated_169f06002d30.tgs")
        self.assertTrue(pa.NAME_RE.match(n))

    def _rows(self):
        live = [{"custom_emoji_id": "logo-cid", "emoji": "✅"},
                {"custom_emoji_id": "1", "emoji": "\U0001f512"}]
        items = {KEY: {"path": Path("x.webp"), "fmt": "static",
                       "cid": "1", "set": "set1", "keywords": ["lock"]}}
        return items, pa._rows({"name": "set1"}, live, items, {"1": KEY})

    def test_the_logo_leads_and_carries_no_id(self):
        """It is inserted at publish time and has no catalog row, so it can only
        be recognised by sitting at slot 1 -- and it must not be given one."""
        _items, rows = self._rows()
        self.assertEqual(rows[0]["file"], pa.LOGO_NAME)
        self.assertIsNone(rows[0]["premium_id"])
        self.assertEqual(rows[1]["position"], 2)
        self.assertEqual(rows[1]["file"], ARCHIVED_NAME)

    def test_the_two_human_files_count_the_logo_differently_on_purpose(self):
        """`_history.md` mirrors the live pack, logo included; `_manifest.md` is
        the name -> id lookup, and the logo has no id to look up."""
        items, rows = self._rows()
        rec = {"name": "set1", "title": "Pack One", "fmt": "mixed"}
        hist = pa.render_history_md(rec, rows)
        man = pa.render_manifest_md(rec, rows, items)
        self.assertIn("Emoji in the pack: **2**", hist)
        self.assertIn("|  1 emoji", man)
        self.assertIn(pa.LOGO_NAME, hist)
        self.assertNotIn(pa.LOGO_NAME, man)
        self.assertIn("lock", man)


if __name__ == "__main__":
    unittest.main()
