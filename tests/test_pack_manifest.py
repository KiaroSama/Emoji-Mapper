"""The pack roster: what it records, and when it admits to being stale.

`packs/` only earns its keep by being right, so these pin the three things that
would make it quietly wrong: the brand logo losing its id again, a source id
being confused with one of our own, and a changed input not registering as
stale.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pack_gallery
import pack_manifest as pm


def _sticker(cid, emoji="\U0001f600", *, animated=False, video=False):
    return {"custom_emoji_id": cid, "emoji": emoji,
            "is_animated": animated, "is_video": video}


class _FakeTG:
    def __init__(self, stickers):
        self._s = {"stickers": stickers}

    def get_sticker_set(self, _name):
        return self._s


class TheRosterDescribesTheLiveSet(unittest.TestCase):
    def test_the_brand_logo_keeps_its_id_and_is_emoji_zero(self):
        """The older collection/manifests left this cell EMPTY.

        The logo has no catalog row -- it is inserted at publish time, not
        ingested -- so anything that walks the catalog cannot name it. Reading
        the live set can, and the owner asked for that id specifically.
        """
        doc = pm.build_pack(_FakeTG([_sticker("111"), _sticker("222")]),
                            {"name": "s", "index": 1, "logo": True}, "general", {}, set())
        logo = doc["emoji"][0]
        self.assertEqual(logo["custom_emoji_id"], "111")
        self.assertEqual(logo["index"], 0, "the owner counts the logo as emoji 0")
        self.assertEqual(logo["slot"], 1, "Telegram counts it as slot 1")
        self.assertEqual(logo["role"], "brand-logo")

    def test_a_coin_pack_has_no_logo_row(self):
        """The coin bot is exempt from the brand logo, so emoji 0 is a real coin
        and calling it a logo would be a lie in 29 of the 34 packs."""
        doc = pm.build_pack(_FakeTG([_sticker("111")]), {"name": "c", "index": 1},
                            "coins", {"111": ["btc"]}, set())
        self.assertEqual(doc["emoji"][0]["role"], "emoji")
        self.assertEqual(doc["emoji"][0]["name"], "btc")

    def test_our_own_id_is_not_reported_as_a_source(self):
        """An item's keywords can hold our OWN id too, because the bot
        inventories were repointed at our packs and that write-back lands in the
        same field. Only ids that are NOT live in our packs came from elsewhere;
        reporting one of ours as the original would invent provenance."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "catalog.db"
            import sqlite3
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE items(content_key TEXT, keywords TEXT, emojis TEXT, format TEXT);"
                "CREATE TABLE publications(content_key TEXT, custom_emoji_id TEXT);")
            con.execute("INSERT INTO items VALUES(?,?,?,?)",
                        ("k", json.dumps(["premium-id:999", "premium-id:555", "lock"]),
                         "[]", "static"))
            con.execute("INSERT INTO publications VALUES(?,?)", ("k", "555"))
            con.commit()
            con.close()
            with patch.object(pm, "CATALOG", db):
                prov = pm.general_provenance(live_ids={"555"})
        self.assertEqual(prov["555"]["source_emoji_ids"], ["999"])
        self.assertEqual(prov["555"]["name"], "lock")

    def test_one_ticker_per_line_is_not_enough(self):
        """Shared-logo coins point several tickers at one sticker; collapsing
        them to the first would drop real information from the record."""
        with tempfile.TemporaryDirectory() as tmp:
            m = Path(tmp) / "t.json"
            m.write_text(json.dumps({"btc": "1", "xbt": "1", "eth": "2"}), encoding="utf-8")
            with patch.object(pm, "TICKER_MAP", m):
                prov = pm.coin_provenance()
        self.assertEqual(prov["1"], ["btc", "xbt"])
        self.assertEqual(prov["2"], ["eth"])


class TheRosterRemembersRetiredIds(unittest.TestCase):
    """`publications` is keyed (base, content_key), so a replace OVERWRITES the
    id it had. The roster is the only place the dead id can survive, and a dead
    id is exactly what a stale bot inventory still points at."""

    def _doc(self, cid, prior=None):
        return pm.build_pack(_FakeTG([_sticker("logo"), _sticker(cid)]),
                             {"name": "s", "index": 1, "logo": True}, "general",
                             {cid: {"content_key": "k", "name": "lock",
                                    "source_emoji_ids": ["999"]}}, set(), prior)

    def test_a_changed_id_pushes_the_old_one_into_the_history(self):
        first = self._doc("111")
        prior = {e["history_key"]: {"id": e["custom_emoji_id"],
                                    "history": e["previous_custom_emoji_ids"]}
                 for e in first["emoji"]}
        second = self._doc("222", prior)
        row = second["emoji"][1]
        # All three the owner asked for, and in that order.
        self.assertEqual(row["source_emoji_ids"], ["999"], "the original pack")
        self.assertEqual(row["previous_custom_emoji_ids"], ["111"], "ours, retired")
        self.assertEqual(row["custom_emoji_id"], "222", "ours, now")

    def test_an_unchanged_id_invents_no_history(self):
        """A reorder keeps the id (setStickerPositionInSet does not mint one),
        so a roster that logged a change on every refresh would be noise."""
        prior = {"ck:k": {"id": "111", "history": []}}
        self.assertEqual(self._doc("111", prior)["emoji"][1]["previous_custom_emoji_ids"], [])

    def test_the_trail_keeps_growing_and_never_repeats(self):
        prior = {"ck:k": {"id": "222", "history": ["111"]}}
        self.assertEqual(self._doc("333", prior)["emoji"][1]["previous_custom_emoji_ids"],
                         ["111", "222"])
        again = {"ck:k": {"id": "333", "history": ["111", "222"]}}
        self.assertEqual(self._doc("333", again)["emoji"][1]["previous_custom_emoji_ids"],
                         ["111", "222"])

    def test_history_follows_the_emoji_not_the_id_or_the_slot(self):
        """Keyed by content_key on purpose: a replace changes the id and a
        reorder changes the slot, so either as the key would lose the trail at
        the exact moment it starts to matter."""
        doc = self._doc("111")
        self.assertEqual(doc["emoji"][1]["history_key"], "ck:k")
        self.assertTrue(doc["emoji"][0]["history_key"].startswith("logo:"),
                        "the logo has no catalog row, so it is keyed by its set")

    def test_the_page_shows_the_retired_id_and_lets_it_be_copied(self):
        doc = self._doc("222", {"ck:k": {"id": "111", "history": []}})
        with tempfile.TemporaryDirectory() as tmp:
            page = pack_gallery.render(doc, lambda _e: None, Path(tmp) / "t")
        self.assertIn('data-id="111"', page, "the retired id must be copyable too")
        self.assertIn("this pack, before", page)


class StalenessIsNotOptimistic(unittest.TestCase):
    """The hook is a GATE, so a wrong 'fresh' is the expensive answer."""

    def test_a_missing_roster_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(pm, "OUT_DIR", Path(tmp)):
                stale, why = pm.check_stale()
        self.assertTrue(stale)
        self.assertIn("index.json", why)

    def test_a_changed_input_is_stale_and_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, cat = Path(tmp) / "packs", Path(tmp) / "catalog.db"
            out.mkdir()
            cat.write_bytes(b"one")
            with patch.object(pm, "OUT_DIR", out), patch.object(pm, "CATALOG", cat):
                (out / "index.json").write_text(
                    json.dumps({"inputs": pm.fingerprint()}), encoding="utf-8")
                self.assertEqual(pm.check_stale()[0], False, "just written, so fresh")
                cat.write_bytes(b"two but longer")          # a new emoji, a new id...
                stale, why = pm.check_stale()
        self.assertTrue(stale, "a changed catalog must re-arm the gate")
        self.assertIn("catalog.db", why, "the message has to name what moved")

    def test_an_unreadable_roster_is_stale_not_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "packs"
            out.mkdir()
            (out / "index.json").write_text("{not json", encoding="utf-8")
            with patch.object(pm, "OUT_DIR", out):
                self.assertTrue(pm.check_stale()[0])


class ThePageCarriesTheDataToo(unittest.TestCase):
    def test_the_html_repeats_the_roster_for_machines(self):
        """A parser handed the page must not have to scrape the markup."""
        doc = pm.build_pack(_FakeTG([_sticker("111", "✅")]),
                            {"name": "s", "index": 1, "logo": True}, "general", {}, set())
        with tempfile.TemporaryDirectory() as tmp:
            page = pack_gallery.render(doc, lambda _e: None, Path(tmp) / "thumbs")
        self.assertIn('<script type="application/json" id="roster">', page)
        blob = page.split('id="roster">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(blob)["emoji"][0]["custom_emoji_id"], "111")
        # and the same facts on the card, for a reader that walks the DOM
        self.assertIn('data-custom-emoji-id="111"', page)
        self.assertIn('data-index="0"', page)

    def test_a_missing_art_file_leaves_a_hole_not_an_exception(self):
        """6600 emoji will always include one file this machine cannot open."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(pack_gallery.thumb_uri("1", Path(tmp) / "nope.png",
                                                     "static", Path(tmp)))

    def test_both_ids_are_copyable_and_the_glyph_is_shown(self):
        """The owner asked for all three: the current id, the id it had in its
        original pack, and the glyph -- which Telegram never displays, so this
        grid is the only place it can be checked against the art."""
        doc = pm.build_pack(
            _FakeTG([_sticker("111", "✅"), _sticker("222", "🔒")]),
            {"name": "s", "index": 1, "logo": True}, "general",
            {"222": {"name": "lock", "source_emoji_ids": ["999"]}}, set())
        with tempfile.TemporaryDirectory() as tmp:
            page = pack_gallery.render(doc, lambda _e: None, Path(tmp) / "t")
        self.assertIn('data-id="222"', page, "our id must be copyable")
        self.assertIn('data-id="999"', page, "the original-pack id must be too")
        self.assertIn('class="glyph"', page)
        self.assertIn("🔒", page, "the glyph itself has to reach the page")

    def test_the_page_offers_nothing_that_changes_anything(self):
        """It mirrors the curate panel on purpose, so the read-only half has to
        be provable: a control that looks live but saves nothing is worse than
        no control."""
        doc = pm.build_pack(_FakeTG([_sticker("111")]),
                            {"name": "s", "index": 1, "logo": True}, "general", {}, set())
        with tempfile.TemporaryDirectory() as tmp:
            page = pack_gallery.render(doc, lambda _e: None, Path(tmp) / "t")
        # The MARKUP, not the stylesheet: the CSS comment names the very
        # attributes this page leaves out, and matching prose would fail on a
        # page that is in fact correct.
        body = page.split("</style>", 1)[1]
        for mutating in ("draggable", "/api/save", "/api/order", "class=\"tick\"",
                         "Save selection", "contenteditable"):
            self.assertNotIn(mutating, body, f"the roster page must not offer {mutating}")

    def test_the_view_only_controls_survive_the_read_only_stripping(self):
        """Stripping "everything that mutates" once took these with it.

        Top/Bottom, the backdrop cycle and the animation switch change no data;
        they are how a grid of a thousand emoji is read at all, and the backdrop
        is what makes a black or a white emoji visible.
        """
        doc = pm.build_pack(_FakeTG([_sticker("111")]),
                            {"name": "s", "index": 1, "logo": True}, "general", {}, set())
        with tempfile.TemporaryDirectory() as tmp:
            page = pack_gallery.render(doc, lambda _e: None, Path(tmp) / "t")
        for control in ('id="top"', 'id="bot"', 'id="bg"', 'id="anim"'):
            self.assertIn(control, page, f"the roster page needs {control}")
        self.assertIn("bg-checker", page, "and the backdrop it starts on")

    def test_an_animated_card_starts_frozen(self):
        """The page inlines both frames' worth of data, so the ONLY thing
        stopping 150 simultaneous decodes is that the card starts on its still
        and the observer opts it in."""
        page_src = Path("pack_gallery.py").read_text(encoding="utf-8")
        self.assertIn('data-still="{still}"', page_src)
        self.assertIn("src=\"{still}\"", page_src, "an animated card must START on the still")
        self.assertIn("freezeAll()", page_src)
        self.assertIn("visibilitychange", page_src, "a hidden tab must stop decoding too")

    def test_the_zero_note_matches_the_family(self):
        gen = pm._zero_note({"family": "general"})
        coin = pm._zero_note({"family": "coins"})
        self.assertIn("brand logo", gen)
        self.assertIn("no brand logo", coin)


if __name__ == "__main__":
    unittest.main()
