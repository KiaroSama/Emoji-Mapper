"""Preflight may not report acceptance it never obtained.

F10. The old counter counted ATTEMPTS. A transport failure was logged as a
warning and then forgotten, so a run in which every single `check_uploadable`
call failed to reach Telegram printed "Every queued file is acceptable to
Telegram" and exited 0 having validated nothing at all. Someone reads that and
starts a 200-upload publish.

Four outcomes now, counted apart: accepted, refused (Telegram gave a verdict),
missing (never reached Telegram), and unknown (we could not ask). Unknown is
neither an accepted file nor a permanent rejection, and it never borrows the
exit code of either.

No network: `check_uploadable` is the only Telegram call preflight makes, and a
stub records exactly what it was asked.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from tests._panel_fixtures import ROOT, _make_png

sys.path.insert(0, str(ROOT))

import collection_preflight as pf
from build_collection import pending_keys
from build_pack import EXIT_FAILED, EXIT_OK, EXIT_PARTIAL
from emojikit.catalog import Catalog
from telegram_api import BotApiError

BASE = "testbase"


class _StubTelegram:
    """Answers `check_uploadable` from a per-file script. Nothing else exists.

    Deliberately not a mock of the whole client: preflight is allowed to make
    exactly this one call, and an attribute error on anything else is the test
    telling us the contract widened.
    """

    def __init__(self, answers: dict):
        self.answers = answers          # file name -> None | Exception
        self.asked: list[str] = []

    def check_uploadable(self, user_id, path, fmt):
        self.asked.append(Path(path).name)
        answer = self.answers.get(Path(path).name)
        if isinstance(answer, Exception):
            raise answer
        return True


class PreflightOutcomes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cat = Catalog(self.dir / "catalog.db")
        self.keys: list[str] = []
        # Three ordinary static items, so every combination below has material.
        for i in range(3):
            img = self.dir / "media" / f"i{i}.png"
            _make_png(img)
            key = f"s:item{i}"
            self.cat.add(content_key=key, fmt="static", file_path=img)
            self.keys.append(key)
        self.plan = {"static": list(self.keys)}
        # PREFLIGHT_DELAY paces real API calls; a stub needs no pacing and the
        # suite should not pay 0.15s per file for nothing.
        self._delay = pf.PREFLIGHT_DELAY
        pf.PREFLIGHT_DELAY = 0

    def tearDown(self):
        pf.PREFLIGHT_DELAY = self._delay
        self.cat.close()
        self.tmp.cleanup()

    def run_preflight(self, answers):
        tg = _StubTelegram(answers)
        code = pf.preflight(tg, self.cat, 42, self.plan, ["static"], BASE,
                            set(), pending_keys)
        return code, tg

    def test_every_file_accepted_is_the_only_success(self):
        code, tg = self.run_preflight({})
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(tg.asked), 3, "not every queued file was checked")

    def test_a_total_transport_failure_is_never_success(self):
        """The audit's reproduction: zero verdicts obtained, yet it reported
        acceptance and exited 0.

        PARTIAL and not FAILED, even though NOTHING was validated: FAILED is
        the code a Telegram refusal returns, and answering a dropped connection
        with it sends someone editing artwork that was never the problem. The
        count of unreachable files does not change what happened -- nobody
        said no.
        """
        boom = RuntimeError("connection reset by peer")
        code, tg = self.run_preflight({f"i{i}.png": boom for i in range(3)})
        self.assertEqual(len(tg.asked), 3)
        self.assertNotEqual(code, EXIT_OK,
                            "no file was validated, so this cannot be a pass")
        self.assertEqual(code, EXIT_PARTIAL,
                         "an unreachable server is not a bad file")

    def test_a_partial_transport_failure_is_not_success_either(self):
        code, _tg = self.run_preflight({"i1.png": RuntimeError("timed out")})
        self.assertEqual(code, EXIT_PARTIAL,
                         "one unchecked file must not be reported as checked")

    def test_a_telegram_refusal_fails(self):
        code, _tg = self.run_preflight(
            {"i2.png": BotApiError("Bad Request: wrong file type")})
        self.assertEqual(code, EXIT_FAILED)

    def test_a_missing_file_fails_and_is_never_sent(self):
        """A file that is not on disk is not a Telegram verdict at all, and the
        stub proves it was never asked about."""
        (self.dir / "media" / "i0.png").unlink()
        code, tg = self.run_preflight({})
        self.assertEqual(code, EXIT_FAILED)
        self.assertNotIn("i0.png", tg.asked)

    def test_a_refusal_outranks_an_unknown(self):
        """A known-bad file is actionable now; an unreachable server is not.
        Reporting the weaker outcome would hide the one that can be fixed."""
        code, _tg = self.run_preflight({
            "i0.png": BotApiError("Bad Request: STICKER_VIDEO_LONG"),
            "i1.png": RuntimeError("connection reset"),
        })
        self.assertEqual(code, EXIT_FAILED)

    def test_an_empty_queue_is_not_a_validation(self):
        self.plan = {"static": []}
        code, tg = self.run_preflight({})
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(tg.asked, [])

    def test_it_publishes_nothing_whatever_the_outcome(self):
        """Preflight's other contract: `check_uploadable` is the ONLY call it
        may make. Any set-mutating method on the stub would raise."""
        for answers in ({}, {"i0.png": RuntimeError("x")},
                        {"i0.png": BotApiError("no")}):
            with self.subTest(answers=list(answers)):
                _code, tg = self.run_preflight(answers)
                self.assertTrue(set(tg.asked) <= {f"i{i}.png" for i in range(3)})


class TheSummaryMatchesTheOutcome(unittest.TestCase):
    """The printed line is what a human acts on, so it is asserted too."""

    def capture(self, accepted, refused, missing, unknown):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = pf._report(accepted, refused, missing, unknown)
        return code, buf.getvalue()

    def test_success_names_the_number_actually_accepted(self):
        code, out = self.capture(3, [], [], [])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("3 accepted", out)
        self.assertIn("All 3 queued file(s) are acceptable", out)

    def test_an_unknown_never_prints_an_acceptance_claim(self):
        code, out = self.capture(0, [], [], [("k", "n.png", "timed out")])
        # PARTIAL: nothing was accepted, but nothing was refused either.
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertNotIn("acceptable to Telegram", out)
        self.assertIn("1 not checked", out)
        self.assertIn("proves nothing about them", out)

    def test_each_category_is_reported_separately(self):
        code, out = self.capture(1, [("a", "a.png", "refused")], [("b", "b.png")],
                                 [("c", "c.png", "timeout")])
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("1 accepted", out)
        self.assertIn("1 refused", out)
        self.assertIn("1 missing", out)
        self.assertIn("1 not checked", out)
        self.assertIn("REFUSED a", out)
        self.assertIn("MISSING b", out)
        self.assertIn("UNCHECKED c", out)


if __name__ == "__main__":
    unittest.main()
