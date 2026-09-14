"""Publisher contracts that are NOT about build_collection's state machine.

Split out of ``test_build_collection_state.py`` because these derive from plain
``unittest.TestCase`` and never touch ``_CatalogFixture`` -- pack naming, the
blank-video guard, and the announcement contract shared by all three
publishers. That last one reaches into ``coins.rebuild_dedup``, so it was never
"collection state" at all.

It also repairs a real defect the split exposed: the original module had
``if __name__ == "__main__": unittest.main()`` sitting ABOVE its last two
classes, so running the file directly collected 93 of its 102 tests and still
printed OK. Discover was unaffected, which is why it stayed invisible.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit import build_collection as bc  # noqa: E402
from emojikit import collection_state as cs  # noqa: E402
from emojikit import build_pack as bp  # noqa: E402
from emojikit import announce  # noqa: E402
from emojikit.announce import (announce_packs)  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402

from tests._bc_fixtures import FakeTG, _make_png  # noqa: E402

# --------------------------------------------------------------------------- #
# M-04: a video is blank only if EVERY sampled frame is
# --------------------------------------------------------------------------- #
def _frame(visible: int) -> bytes:
    px = bytearray(bc._FRAME_BYTES)
    for i in range(visible):
        px[i * 4 + 3] = 255
    return bytes(px)


def _fake_popen(raw: bytes, *, hang: bool = False):
    """A Popen stand-in recording the wall limit each child was given.

    Patched at ``subprocess.Popen`` on purpose: both the old bare
    ``subprocess.run`` and the bounded ``media._run`` go through it, so the
    recorded timeout is a fair comparison between them.
    """
    seen: list = []

    class _P:
        def __init__(self, cmd, stdout=None, stderr=None, **kw):
            self.args, self.returncode = cmd, 0

        def communicate(self, input=None, timeout=None):
            seen.append(timeout)
            if hang:
                raise subprocess.TimeoutExpired(self.args, timeout or 0)
            return raw, b""

        def poll(self):
            return self.returncode

        def kill(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    return _P, seen


class VideoBlankCheck(unittest.TestCase):
    def _probe(self, raw: bytes, *, hang: bool = False):
        popen, seen = _fake_popen(raw, hang=hang)
        with mock.patch("subprocess.Popen", popen), \
                mock.patch.object(bc.media, "ffmpeg_path", lambda: "ffmpeg"):
            return bc._media_ok(Path("clip.webm"), "video"), seen

    def _media_ok(self, raw: bytes) -> bool:
        return self._probe(raw)[0]

    # ----- H-09: the probe is bounded like every other ffmpeg child ------- #
    def test_the_probe_runs_under_a_finite_wall_limit(self):
        ok, seen = self._probe(_frame(900) * 2)
        self.assertTrue(ok)
        self.assertTrue(seen, "ffmpeg was never started")
        self.assertTrue(all(t and t > 0 for t in seen),
                        f"unbounded ffmpeg child: timeouts={seen}")

    def test_a_hanging_ffmpeg_is_killed_and_lets_the_upload_decide(self):
        self.assertTrue(self._probe(b"", hang=True)[0])

    def test_fade_in_video_is_accepted(self):
        raw = _frame(0) + _frame(0) + _frame(500) + _frame(900)
        self.assertTrue(self._media_ok(raw))

    def test_fully_blank_video_is_still_rejected(self):
        self.assertFalse(self._media_ok(_frame(0) * 4))

    def test_a_handful_of_stray_pixels_is_still_blank(self):
        self.assertFalse(self._media_ok(_frame(3) * 3))

    def test_undecodable_output_lets_the_upload_decide(self):
        self.assertTrue(self._media_ok(b""))


class PackTitlesAreOneSequence(unittest.TestCase):
    """Titles read "<title> 1, 2, 3" across every format, not per format.

    They used to be "<title> Animated 1" / "<title> Static 1" -- three separate
    sequences, so two packs both called "1". The number now counts every set
    already created, which also makes it resumable: a restarted run continues
    the count instead of restarting it.
    """

    def test_the_number_counts_all_formats_not_just_this_one(self):
        src = Path(bc.__file__).read_text(encoding="utf-8")
        self.assertIn("len(state['sets']) + 1", src)
        self.assertNotIn("FMT_WORD", src,
                         "a format word in the title reintroduces the split")

    def test_the_announcement_uses_the_recorded_title(self):
        """Rebuilding the title at announce time is how it drifts from the set.

        Asserts the title is READ FROM A RECORD, not which variable holds that
        record: it was `fmt_sets[-1]` until `--into-pack` made the set being
        filled something other than the last one. What must never come back is
        a title rebuilt from the base and a counter.
        """
        src = Path(bc.__file__).read_text(encoding="utf-8")
        block = src[src.index("if in_set >= per_set:"):]
        block = block[:block.index("time.sleep")]
        self.assertRegex(block, r'\w+\["title"\]',
                         "the announcement must read the recorded title")
        for rebuilt in ("f\"{base}", "set_title =", "FMT_TAG"):
            self.assertNotIn(rebuilt, block,
                             "the title must not be reconstructed here")


class MixedPublishesOneFamily(unittest.TestCase):
    """--mixed puts every format in one family, in the panel's order.

    The per-format split was a choice this tool made before Bot API 7.2 allowed
    mixed sets, and it costs the curation: the panel's order runs ACROSS
    formats, so splitting regroups a hand-arranged pack into format blocks and
    throws the arrangement away.
    """

    def test_the_base_name_accepts_telegram_s_actual_rule(self):
        # Underscores are legal in a set name; this used to reject them, which
        # refused a perfectly valid name like GodVerify_Emoji_Packs.
        self.assertEqual(bc.valid_base("GodVerify_Emoji_Packs"),
                         "GodVerify_Emoji_Packs")
        self.assertEqual(bc.valid_base("mypack"), "mypack")
        for bad in ("bad__two", "_lead", "9start", "trail_", "has space", ""):
            with self.assertRaises(SystemExit, msg=bad):
                bc.valid_base(bad)

    def test_a_base_too_long_for_the_64_char_name_is_refused_up_front(self):
        """Not as a Bot API error after the plan is frozen and uploads began."""
        bc.check_name_length("short", "GodVerifyEmojiMapperbot")      # fits
        with self.assertRaises(SystemExit):
            bc.check_name_length("x" * 50, "GodVerifyEmojiMapperbot")

    def test_mixed_orders_across_formats_and_drops_the_format_letter(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        with Catalog(data / "catalog.db") as cat:
            # Interleaved on purpose: a per-format plan would regroup these.
            for i, fmt in enumerate(["static", "animated", "static", "video"]):
                img = data / "media" / fmt / f"i{i}.png"
                _make_png(img)
                cat.add(content_key=f"{fmt[0]}:item{i:030d}", fmt=fmt,
                        file_path=img, emojis=["😀"], keywords=[f"i{i}"])
            cat.set_order([f"{f[0]}:item{i:030d}"
                           for i, f in enumerate(["static", "animated",
                                                  "static", "video"])])
            plan = cs.freeze_plan(cat, data, "b", [cs.MIXED])

        keys = plan[cs.MIXED]
        self.assertEqual(len(keys), 4, "every format belongs to the one plan")
        self.assertEqual([k[0] for k in keys], ["s", "a", "s", "v"],
                         "the panel's interleaved order must survive")
        # No format letter in the set name.
        self.assertEqual(cs.FMT_TAG.get(cs.MIXED, ""), "")

    def test_a_family_started_per_format_cannot_switch_to_mixed(self):
        """state["sets"] and the plan are keyed by format.

        Continuing a split family as one family would strand every set already
        created -- invisible to resume and re-created under new names.
        """
        src = Path(bc.__file__).read_text(encoding="utf-8")
        block = src[src.index("state = load_state(data_dir, base)"):]
        block = block[:block.index("plan = freeze_plan(")]
        self.assertIn("started", block)
        self.assertIn("EXIT_USAGE", block)

    def test_state_written_by_mixed_can_be_read_back(self):
        """The validator rejected the very state the publisher had written.

        `--mixed` records sets with fmt="mixed"; load_state only accepted the
        three real formats, so the first resume of a mixed family refused its
        own file and the publish could not continue.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        cs.save_json(cs._state_path(data, "b"), {
            "base": "b", "sent": [], "skipped": [],
            "sets": [{"fmt": cs.MIXED, "index": 1, "name": "b1_by_bot",
                      "title": "T 1", "live": 1, "logo": True, "keys": []}]})
        state = cs.load_state(data, "b")          # must not raise
        self.assertEqual(state["sets"][0]["fmt"], cs.MIXED)

        # A genuinely unknown format must still be refused.
        cs.save_json(cs._state_path(data, "c"), {
            "base": "c", "sent": [], "skipped": [],
            "sets": [{"fmt": "sideways", "index": 1, "name": "c1_by_bot",
                      "title": "T", "live": 0, "keys": []}]})
        with self.assertRaises(cs.StateError):
            cs.load_state(data, "c")



class EveryPublisherSharesOneAnnouncer(unittest.TestCase):
    """All three publishers announce through ``announce_packs``.

    They used to carry three copies of "format the link and sendMessage", and
    when the Worker arrived only the collector learned about it -- so a coin
    rebuild or a single-pack build kept talking to Telegram from this machine
    while the owner believed the bot was posting. These tests fail if any
    publisher grows its own copy again.
    """

    WORKER = {"WORKER_PUBLISH_URL": "https://w.dev/publish",
              "WORKER_PUBLISH_SECRET": "s"}

    def test_the_single_pack_build_and_the_coin_rebuild_both_import_it(self):
        import coins.rebuild_dedup as rd
        for mod in (bc, bp, rd):
            self.assertIs(mod.announce_packs, announce_packs,
                          f"{mod.__name__} does not use the shared announcer")

    def test_half_a_worker_config_takes_the_direct_path(self):
        # URL without secret is a half-finished setup. Routing to it anyway
        # would 401 every announcement; silently "succeeding" via the direct
        # path at least still posts, and the missing secret stays visible.
        tg = FakeTG()
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": "https://w.dev/publish",
                                          "WORKER_PUBLISH_SECRET": "",
                                          "PACK_LINKS_CHAT_ID": ""}, clear=False), \
             mock.patch.object(announce, "announce_via_worker") as worker:
            dest = announce_packs(tg, 7, [{"name": "a_by_bot", "title": "A"}],
                                  bot="general")
        worker.assert_not_called()
        self.assertEqual(len(tg.sent), 1)
        self.assertEqual(dest, "7")

    def test_the_direct_path_disables_link_previews(self):
        # 30 addemoji links with a preview card each buries the list. The coin
        # script used to do this with a private _call; losing it in the move to
        # a shared announcer would be a silent regression.
        seen = []
        tg = mock.Mock()
        tg.send_message.side_effect = lambda *a, **kw: seen.append(kw)
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": "",
                                          "PACK_LINKS_CHAT_ID": ""}, clear=False):
            announce_packs(tg, 7, [{"name": "a_by_bot", "title": "A"}], bot="coin")
        self.assertTrue(all(kw.get("disable_preview") for kw in seen), seen)

    def test_a_whole_family_goes_in_ONE_worker_call(self):
        # The Worker splits across messages when it passes 4096 characters; a
        # per-pack call would defeat that and also post 30 separate messages.
        tg = FakeTG()
        packs = [{"name": f"p{i}_by_bot", "title": str(i)} for i in range(30)]
        with mock.patch.dict(os.environ, self.WORKER, clear=False), \
             mock.patch.object(announce, "announce_via_worker") as worker:
            announce_packs(tg, 7, packs, bot="coin", note="all packs:")
        worker.assert_called_once()
        self.assertEqual(len(worker.call_args.args[0]), 30)
        self.assertEqual(worker.call_args.kwargs["note"], "all packs:")
        self.assertEqual(worker.call_args.kwargs["bot"], "coin")

    def test_the_note_is_sent_too_on_the_direct_path(self):
        tg = FakeTG()
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": "",
                                          "PACK_LINKS_CHAT_ID": ""}, clear=False):
            announce_packs(tg, 7, [{"name": "a_by_bot", "title": "A"}],
                           bot="coin", note="header")
        self.assertEqual(tg.sent[0], "header")
        self.assertIn("t.me/addemoji/a_by_bot", tg.sent[1])


class AFilledPackAnnouncesAgain(unittest.TestCase):
    """A pack is worth announcing twice: when it goes up, and when it FILLS.

    ``state["sent"]`` conflated the two, so pack 2 -- announced while it still
    held 96 emoji -- said nothing to the channel when it reached 200, which is
    the only moment the owner actually cares about.
    """

    def _notify(self, state, *, full):
        with tempfile.TemporaryDirectory() as tmp,                 mock.patch.object(bc, "announce_packs", return_value="test") as spy:
            bc.notify(None, 1, state, Path(tmp), "b", "pack1", "Pack 1", full=full)
        return spy.call_count

    def test_filling_announces_even_though_the_link_already_went_out(self):
        state = {"sent": ["pack1"], "skipped": [], "sets": []}
        self.assertEqual(self._notify(state, full=True), 1, "the FULL milestone is its own")
        self.assertEqual(state["sent_full"], ["pack1"])

    def test_neither_milestone_fires_twice(self):
        state = {"sent": [], "skipped": [], "sets": []}
        self.assertEqual(self._notify(state, full=True), 1)
        self.assertEqual(self._notify(state, full=True), 0, "already announced as full")
        self.assertEqual(self._notify(state, full=False), 1, "first-publish link is separate")
        self.assertEqual(self._notify(state, full=False), 0)

    def test_a_failed_send_is_not_recorded_as_sent(self):
        """Otherwise one network blip silences that pack for good."""
        state = {"sent": [], "skipped": [], "sets": []}
        with tempfile.TemporaryDirectory() as tmp,                 mock.patch.object(bc, "announce_packs", side_effect=RuntimeError("boom")):
            bc.notify(None, 1, state, Path(tmp), "b", "pack1", "Pack 1", full=True)
        self.assertEqual(state.get("sent_full", []), [])

    def test_the_capacity_call_site_asks_for_the_full_milestone(self):
        """The in-run "this set just hit per_set" branch is the whole point."""
        src = Path(bc.__file__).read_text(encoding="utf-8")
        head = src[src.index("if in_set >= per_set:"):]
        self.assertIn("full=True", head[:head.index("in_set = 0")])

    def test_the_state_loader_keeps_the_new_list_a_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "publish_b.json").write_text(
                '{"base": "b", "sets": [], "sent": [], "sent_full": "nope"}', encoding="utf-8")
            with self.assertRaises(cs.StateError):
                cs.load_state(d, "b")


class AnnouncementRoutesThroughTheWorker(unittest.TestCase):
    """With a Worker configured, the BOT posts the link -- not this process.

    The duplicate guard must not change with the route: `state["sent"]` is what
    stops a re-run announcing the same pack twice, and it has to hold whichever
    path did the sending.
    """

    PACK = "clos1_by_bot"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state = {"sent": []}

    def tearDown(self):
        self.tmp.cleanup()

    def _notify(self, tg):
        bc.notify(tg, 1, self.state, self.dir, "clos", self.PACK, "Closure 1")

    def test_worker_is_used_and_telegram_is_not_touched(self):
        tg = mock.Mock()
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": "https://w.dev/publish",
                                          "WORKER_PUBLISH_SECRET": "s"}, clear=False), \
             mock.patch.object(announce, "announce_via_worker") as worker:
            self._notify(tg)
        worker.assert_called_once()
        packs = worker.call_args.args[0]
        self.assertEqual(packs[0]["name"], self.PACK)
        tg.send_message.assert_not_called()
        self.assertIn(self.PACK, self.state["sent"])

    def test_without_a_worker_it_still_posts_directly(self):
        tg = mock.Mock()
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": ""}, clear=False):
            self._notify(tg)
        tg.send_message.assert_called_once()
        self.assertIn(self.PACK, self.state["sent"])

    def test_a_failed_worker_call_does_not_record_it_as_sent(self):
        # Otherwise the pack is never announced: the guard would skip it forever.
        tg = mock.Mock()
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": "https://w.dev/publish",
                                          "WORKER_PUBLISH_SECRET": "s"}, clear=False), \
             mock.patch.object(announce, "announce_via_worker",
                               side_effect=RuntimeError("worker down")):
            self._notify(tg)
        self.assertEqual(self.state["sent"], [])

    def test_an_already_sent_pack_is_not_announced_again(self):
        self.state["sent"].append(self.PACK)
        tg = mock.Mock()
        with mock.patch.dict(os.environ, {"WORKER_PUBLISH_URL": "https://w.dev/publish",
                                          "WORKER_PUBLISH_SECRET": "s"}, clear=False), \
             mock.patch.object(announce, "announce_via_worker") as worker:
            self._notify(tg)
        worker.assert_not_called()
        tg.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
