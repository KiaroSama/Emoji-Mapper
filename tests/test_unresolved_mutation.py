"""What happens when a mutation cannot be proved either way.

Split from test_resume_safety.py, which covers the ordinary resume path.
Every case here is one where the live state is UNKNOWN -- an ambiguous
create, an unresolved in-flight record, a recorded set that no longer
reads back -- and the guarantee is that the run stops rather than guess.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_pack as bp  # noqa: E402
import telegram_api as tg_api  # noqa: E402
import packstate as ps  # noqa: E402
from tests._pack_fixtures import _png  # noqa: E402


class UnresolvedMutationStopsTheRun(unittest.TestCase):
    """After an ambiguous upload, NO further mutation may be attempted.

    Continuing would overwrite the in-flight intent with the next item's, which
    destroys the only record of which mutation is unresolved.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src"
        for name, color in (("a.png", (200, 0, 0, 255)),
                            ("b.png", (0, 200, 0, 255)),
                            ("c.png", (0, 0, 200, 255))):
            _png(self.src / name, color)
        self.state = self.dir / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, tg):
        argv = ["build_pack.py", "--base", "t", "--title", "T", "--user-id", "1",
                "--source-dir", str(self.src), "--token-env", "FAKE_TOKEN",
                "--state", str(self.state)]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict("os.environ", {"FAKE_TOKEN": "x"}, clear=False), \
             mock.patch.object(bp, "Telegram", return_value=tg), \
             mock.patch.object(bp.time, "sleep", lambda s: None):
            return bp.main()

    def _tg(self):
        tg = mock.Mock()
        tg.get_me.return_value = {"username": "bot"}
        tg.probe_set_state.return_value = (tg_api.SetState.MISSING, None)
        tg.send_message.return_value = None
        return tg

    def test_a_strangers_set_is_not_adopted_after_an_ambiguous_create(self):
        """The IN-RUN twin of the restart adoption, and it had no test at all.

        A set of the right name holding one sticker is a SHAPE, not an identity:
        it may be a stranger's. Adopting on the count records a foreign pack as
        ours, marks the item done though it was never uploaded, publishes the
        link, and every later add writes our stickers into someone else's set.
        A mutation test proved the content check here was uncovered — removing
        it left the whole 530-test suite green.
        """
        tg = self._tg()
        tg.create_set.side_effect = tg_api.AmbiguousUploadError("timed out after apply")
        # A set of that name exists with exactly one sticker -- but it is not ours.
        tg.probe_set_state.return_value = (
            tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "STRANGER"}]})
        tg._sticker_matches.return_value = False

        self.assertEqual(self._run(tg), bp.EXIT_PARTIAL)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["sets"], [], "a stranger's pack was recorded as ours")
        self.assertEqual(saved["done"], [], "an item was marked done unuploaded")
        self.assertIsNotNone(saved["in_flight"],
                             "the unresolved create must stay on the books")
        tg.send_message.assert_not_called()      # no link for a pack we do not own

    def test_an_unverifiable_set_is_not_adopted_after_an_ambiguous_create(self):
        """`_sticker_matches` returning None is "could not look", not "it is ours"."""
        tg = self._tg()
        tg.create_set.side_effect = tg_api.AmbiguousUploadError("timed out after apply")
        tg.probe_set_state.return_value = (
            tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "UNREADABLE"}]})
        tg._sticker_matches.return_value = None

        self.assertEqual(self._run(tg), bp.EXIT_PARTIAL)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["sets"], [])
        self.assertIsNotNone(saved["in_flight"])

    def test_our_own_set_is_still_adopted_after_an_ambiguous_create(self):
        """The positive control: proving identity must not block the real case."""
        tg = self._tg()
        tg.create_set.side_effect = tg_api.AmbiguousUploadError("timed out after apply")
        sset = {"stickers": [{"file_unique_id": "OURS"}]}
        tg.probe_set_state.return_value = (tg_api.SetState.EXISTS, sset)
        tg.probe_sticker_set.return_value = (True, sset)
        tg.add_sticker.return_value = None
        tg._sticker_matches.return_value = True

        self._run(tg)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual([s["name"] for s in saved["sets"]], ["t1_by_bot"])
        self.assertIsNone(saved["in_flight"], "a proven create stays unresolved")

    def test_ambiguous_add_stops_before_the_next_item(self):
        tg = self._tg()
        # First item creates the set; the second add comes back ambiguous.
        tg.create_set.return_value = None
        tg.add_sticker.side_effect = tg_api.AmbiguousUploadError("timed out after apply")
        tg.probe_set_state.return_value = (tg_api.SetState.EXISTS,
                                           {"stickers": [{"file_unique_id": "f0"}]})

        code = self._run(tg)
        self.assertEqual(code, bp.EXIT_PARTIAL, "an unresolved add must not exit 0")
        self.assertEqual(tg.add_sticker.call_count, 1,
                         "no second mutation may be attempted")

        saved = json.loads(self.state.read_text(encoding="utf-8"))
        intent = saved["in_flight"]
        self.assertIsNotNone(intent, "the unresolved intent must be preserved")
        self.assertEqual(intent["operation"], "add")
        self.assertEqual(intent["key"], "b")

    def test_ambiguous_create_records_the_target_set(self):
        tg = self._tg()
        tg.create_set.side_effect = tg_api.AmbiguousUploadError("timed out after apply")
        tg.probe_set_state.return_value = (tg_api.SetState.MISSING, None)

        code = self._run(tg)
        self.assertEqual(code, bp.EXIT_PARTIAL)
        intent = json.loads(self.state.read_text(encoding="utf-8"))["in_flight"]
        self.assertEqual(intent["operation"], "create")
        self.assertTrue(intent["set_name"],
                        "an ambiguous create must record the set name it targeted")

    def test_unknown_live_state_on_resume_keeps_the_intent(self):
        ps.write_json_atomic(self.state, {
            "base": "t", "per_set": 200, "done": [], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 0, "index": 1}],
            "in_flight": {"key": "a", "operation": "add", "set_name": "t1_by_bot",
                          "set_index": 1, "expected_before": 0},
        })
        tg = self._tg()
        tg.probe_set_state.return_value = (tg_api.SetState.UNKNOWN, None)

        code = self._run(tg)
        self.assertEqual(code, bp.EXIT_PARTIAL)
        tg.add_sticker.assert_not_called()
        tg.create_set.assert_not_called()
        intent = json.loads(self.state.read_text(encoding="utf-8"))["in_flight"]
        self.assertIsNotNone(intent, "an unknown probe must not clear the intent")

    def test_state_for_a_different_base_is_refused(self):
        ps.write_json_atomic(self.state, {
            "base": "OTHER", "per_set": 200, "done": [], "sent": [], "sets": [],
        })
        tg = self._tg()
        self.assertEqual(self._run(tg), bp.EXIT_FAILED)
        tg.create_set.assert_not_called()

    def test_unusable_images_make_the_run_partial(self):
        (self.src / "bad.png").write_bytes(b"")
        tg = self._tg()
        tg.create_set.return_value = None
        tg.add_sticker.return_value = None
        tg.probe_set_state.return_value = (tg_api.SetState.EXISTS, {"stickers": []})
        self.assertEqual(self._run(tg), bp.EXIT_PARTIAL,
                         "a skipped image must not report full success")


class AmbiguousCreateForLaterSets(unittest.TestCase):
    """An ambiguous CREATE of set #2+ must be reconciled against ITS OWN set.

    Recovery used to probe only sets[-1]. For a create, the new set is not in
    state["sets"] yet, so the old last set says nothing about whether it landed
    -- the intent was effectively ignored whenever any earlier set existed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src"
        _png(self.src / "a.png", (200, 0, 0, 255))
        _png(self.src / "b.png", (0, 200, 0, 255))
        self.state = self.dir / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self, **over):
        base = {
            "base": "t", "per_set": 1, "done": ["a"], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 1, "index": 1}],
            "in_flight": {"key": "b", "operation": "create",
                          "set_name": "t2_by_bot", "set_index": 2,
                          "expected_before": 0, "title": "T 2"},
        }
        base.update(over)
        ps.write_json_atomic(self.state, base)

    def _run(self, tg):
        argv = ["build_pack.py", "--base", "t", "--title", "T", "--user-id", "1",
                "--source-dir", str(self.src), "--token-env", "FAKE_TOKEN",
                "--state", str(self.state), "--per-set", "1"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict("os.environ", {"FAKE_TOKEN": "x"}, clear=False), \
             mock.patch.object(bp, "Telegram", return_value=tg), \
             mock.patch.object(bp.time, "sleep", lambda s: None):
            return bp.main()

    def _tg(self, states, *, matches=True):
        tg = mock.Mock()
        tg.get_me.return_value = {"username": "bot"}
        tg.probe_set_state.side_effect = lambda name: states[name]
        tg.send_message.return_value = None
        # Reconciliation now requires CONTENT proof, so the fake must say
        # whether the live sticker is the image the intent was carrying.
        tg._sticker_matches.return_value = matches
        return tg

    def test_landed_create_of_set_two_is_adopted(self):
        self._state()
        tg = self._tg({
            "t1_by_bot": (tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "f1"}]}),
            "t2_by_bot": (tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "f2"}]}),
        })
        self._run(tg)
        tg.create_set.assert_not_called()
        tg.add_sticker.assert_not_called()
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("b", saved["done"], "the landed create must be adopted")
        self.assertIn("t2_by_bot", [s["name"] for s in saved["sets"]])
        self.assertIsNone(saved["in_flight"])

    def test_missing_create_of_set_two_leaves_the_item_pending(self):
        self._state()
        tg = self._tg({
            "t1_by_bot": (tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "f1"}]}),
            "t2_by_bot": (tg_api.SetState.MISSING, None),
        })
        tg.create_set.return_value = None
        self._run(tg)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("b", saved["done"], "b should be retried and then land")
        self.assertTrue(tg.create_set.called, "a MISSING create must be retried")

    def test_a_same_named_set_holding_a_FOREIGN_image_is_never_adopted(self):
        """Existence is not proof we created it.

        A set with the expected name may be someone else's, or left over. If
        its first sticker is not the image the intent was carrying, adopting it
        attaches this run's state to a pack it did not build.
        """
        self._state()
        tg = self._tg({
            "t1_by_bot": (tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "f1"}]}),
            "t2_by_bot": (tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "foreign"}]}),
        }, matches=False)
        self.assertEqual(self._run(tg), bp.EXIT_FAILED)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn("t2_by_bot", [s["name"] for s in saved["sets"]],
                         "a foreign set must not be adopted")

    def test_a_create_target_holding_EXTRA_stickers_is_never_adopted(self):
        """A create leaves exactly ONE sticker.

        A matching first sticker in a set of several proves only that our image
        is in there somewhere; the set was not left by our interrupted create
        alone. Adopting it books whatever else is in there as this run's work,
        and the recorded count then drives every later expectation.
        """
        self._state()
        tg = self._tg({
            "t1_by_bot": (tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "f1"}]}),
            "t2_by_bot": (tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "f2"},
                                                            {"file_unique_id": "extra"}]}),
        }, matches=True)          # even a matching FIRST sticker is not enough
        self.assertEqual(self._run(tg), bp.EXIT_FAILED)
        tg.create_set.assert_not_called()
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn("t2_by_bot", [s["name"] for s in saved["sets"]],
                         "a set we may not have created must not be adopted")
        self.assertNotIn("b", saved["done"])

    def test_an_unverifiable_create_target_stops_retryably(self):
        self._state()
        tg = self._tg({
            "t1_by_bot": (tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "f1"}]}),
            "t2_by_bot": (tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "x"}]}),
        }, matches=None)
        self.assertEqual(self._run(tg), bp.EXIT_PARTIAL)

    def test_unknown_create_stops_retryably(self):
        self._state()
        tg = self._tg({
            "t1_by_bot": (tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "f1"}]}),
            "t2_by_bot": (tg_api.SetState.UNKNOWN, None),
        })
        self.assertEqual(self._run(tg), bp.EXIT_PARTIAL)
        tg.create_set.assert_not_called()
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIsNotNone(saved["in_flight"], "UNKNOWN must keep the intent")


class RecordedSetIntegrity(unittest.TestCase):
    """A recorded set that vanished or shrank invalidates the whole resume."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src"
        _png(self.src / "a.png", (200, 0, 0, 255))
        self.state = self.dir / "state.json"
        ps.write_json_atomic(self.state, {
            "base": "t", "per_set": 200, "done": ["x"], "sent": [],
            "sets": [{"name": "t1_by_bot", "title": "T 1", "count": 5, "index": 1}],
            "in_flight": None,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, probe):
        tg = mock.Mock()
        tg.get_me.return_value = {"username": "bot"}
        tg.probe_set_state.return_value = probe
        tg.send_message.return_value = None
        argv = ["build_pack.py", "--base", "t", "--title", "T", "--user-id", "1",
                "--source-dir", str(self.src), "--token-env", "FAKE_TOKEN",
                "--state", str(self.state)]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict("os.environ", {"FAKE_TOKEN": "x"}, clear=False), \
             mock.patch.object(bp, "Telegram", return_value=tg), \
             mock.patch.object(bp.time, "sleep", lambda s: None):
            return bp.main(), tg

    def test_deleted_recorded_set_is_an_integrity_stop(self):
        code, tg = self._run((tg_api.SetState.MISSING, None))
        self.assertEqual(code, bp.EXIT_FAILED)
        tg.add_sticker.assert_not_called()
        tg.create_set.assert_not_called()

    def test_shrunken_set_is_an_integrity_stop(self):
        code, tg = self._run(
            (tg_api.SetState.EXISTS, {"stickers": [{"file_unique_id": "f"}] * 3}))
        self.assertEqual(code, bp.EXIT_FAILED, "negative drift must not be ignored")
        tg.add_sticker.assert_not_called()

    def test_unknown_recorded_set_is_retryable(self):
        code, tg = self._run((tg_api.SetState.UNKNOWN, None))
        self.assertEqual(code, bp.EXIT_PARTIAL)
        tg.add_sticker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
