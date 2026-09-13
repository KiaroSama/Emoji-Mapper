"""Lottie: a timeline that is not a number, and a repaint that misses things.

From the audit's adjacent-path list. All three were silent, and two of them
made `repaint_in_place` return True having produced art nobody asked for.

* `validate_tgs` compared against NaN. `nan <= 0`, `nan > 60` and `nan > 3.0`
  are every one of them False, so a Lottie with a NaN frame rate or out-point
  passed the whole function and only failed inside a publish -- which is the
  exact failure mode that check exists to prevent.
* An ANIMATED colour (keyframes rather than a flat triple) was skipped, so a
  "repainted" file kept every one of its original colours.
* A gradient's OPACITY ramp was overwritten with colour, because the stop
  walker strode through the flat array in fours without asking how many of
  those entries were colour stops. A gradient that faded out came back opaque.
"""

from __future__ import annotations

import copy
import gzip
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from tests._panel_fixtures import ROOT

sys.path.insert(0, str(ROOT))

from emojikit import media, repaint

VALID = {"v": "5.5", "fr": 30, "ip": 0, "op": 60, "w": media.TGS_SIZE,
         "h": media.TGS_SIZE, "layers": []}


def _tgs(doc: dict, into: Path) -> Path:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(doc).encode("utf-8"))
    into.write_bytes(buf.getvalue())
    return into


class ATimelineMustBeFiniteNumbers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "x.tgs"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_valid_timeline_still_passes(self):
        media.validate_tgs(_tgs(VALID, self.path))       # must not raise

    def test_nan_in_any_timeline_field_is_refused(self):
        """NaN defeats every comparison rather than failing one, which is why
        it reached a publish. Each field gets its own case: they are read in
        different expressions and a fix that covered only `fr` would look right."""
        for field in ("fr", "ip", "op"):
            with self.subTest(field=field):
                doc = dict(VALID, **{field: float("nan")})
                with self.assertRaises(media.MediaError) as caught:
                    media.validate_tgs(_tgs(doc, self.path))
                self.assertIn(field, str(caught.exception))

    def test_infinity_is_refused_by_name_not_by_luck(self):
        """`inf` happened to trip the range checks. That is an accident of
        which comparison ran first, not a rule, so it is stated as one."""
        for field, value in (("fr", float("inf")), ("ip", float("-inf")),
                             ("op", float("inf"))):
            with self.subTest(field=field):
                doc = dict(VALID, **{field: value})
                with self.assertRaises(media.MediaError):
                    media.validate_tgs(_tgs(doc, self.path))

    def test_a_non_numeric_field_is_still_refused(self):
        doc = dict(VALID, fr="thirty")
        with self.assertRaises(media.MediaError):
            media.validate_tgs(_tgs(doc, self.path))


class RepaintingReachesEveryColour(unittest.TestCase):
    BLUE = [0.0, 0.0, 1.0, 1.0]

    def test_a_flat_colour_is_recoloured(self):
        node = {"c": {"a": 0, "k": [1.0, 0.0, 0.0, 1.0]}}
        repaint._tint_lottie(node, self.BLUE)
        self.assertEqual(node["c"]["k"][:3], [0.0, 0.0, 1.0])

    def test_a_flat_colour_keeps_its_own_alpha(self):
        """The static branch of `repaint_in_place` fills THROUGH the original
        alpha so a cut-out stays a cut-out. Forcing the Lottie alpha to 1 was
        the opposite rule in the same function."""
        node = {"c": {"a": 0, "k": [1.0, 0.0, 0.0, 0.25]}}
        repaint._tint_lottie(node, self.BLUE)
        self.assertEqual(node["c"]["k"][3], 0.25)

    def test_an_animated_colour_is_recoloured_too(self):
        """Keyframes, not a flat triple. This branch did not exist, so the
        function reported a repaint it had not performed."""
        node = {"c": {"a": 1, "k": [{"t": 0, "s": [1.0, 0.0, 0.0, 1.0]},
                                    {"t": 30, "s": [0.0, 1.0, 0.0, 0.5]}]}}
        repaint._tint_lottie(node, self.BLUE)
        self.assertEqual(node["c"]["k"][0]["s"][:3], [0.0, 0.0, 1.0])
        self.assertEqual(node["c"]["k"][1]["s"][:3], [0.0, 0.0, 1.0])
        self.assertEqual(node["c"]["k"][1]["s"][3], 0.5, "keyframe alpha lost")

    def test_legacy_keyframe_end_values_are_recoloured(self):
        node = {"c": {"a": 1, "k": [{"t": 0, "s": [1.0, 0.0, 0.0],
                                     "e": [0.0, 1.0, 0.0]}]}}
        repaint._tint_lottie(node, self.BLUE)
        self.assertEqual(node["c"]["k"][0]["e"][:3], [0.0, 0.0, 1.0])


class GradientOpacitySurvivesARepaint(unittest.TestCase):
    """Lottie packs colour stops and opacity stops into ONE flat array."""

    BLUE = [0.0, 0.0, 1.0, 1.0]

    def grad(self):
        # p=2 colour stops of [offset, r, g, b], then 2 opacity stops of
        # [offset, alpha]: 8 + 4 = 12 entries.
        return {"g": {"p": 2, "k": {"a": 0, "k": [
            0.0, 1.0, 0.0, 0.0,
            1.0, 0.0, 0.0, 1.0,
            0.0, 1.0,
            1.0, 0.25]}}}

    def test_the_colour_stops_are_recoloured(self):
        node = self.grad()
        repaint._tint_lottie(node, self.BLUE)
        stops = node["g"]["k"]["k"]
        self.assertEqual(stops[1:4], [0.0, 0.0, 1.0])
        self.assertEqual(stops[5:8], [0.0, 0.0, 1.0])

    def test_the_opacity_ramp_is_left_alone(self):
        """Measured before the fix: [0.0, 1.0, 1.0, 0.25] came back as
        [0.0, 0, 0, 1], so a gradient that faded out became fully opaque."""
        node = self.grad()
        before = copy.deepcopy(node["g"]["k"]["k"])
        repaint._tint_lottie(node, self.BLUE)
        self.assertEqual(node["g"]["k"]["k"][8:], before[8:])

    def test_the_offsets_are_left_alone(self):
        """They are the shape of the ramp; only the colour flattens."""
        node = self.grad()
        before = copy.deepcopy(node["g"]["k"]["k"])
        repaint._tint_lottie(node, self.BLUE)
        stops = node["g"]["k"]["k"]
        self.assertEqual(stops[0], before[0])
        self.assertEqual(stops[4], before[4])

    def test_a_gradient_with_no_declared_stop_count_is_handled_safely(self):
        """Without `p`, the only safe reading of "all colour" is an array that
        divides cleanly by four. Anything else is left untouched rather than
        guessed at."""
        clean = {"g": {"k": {"a": 0, "k": [0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0]}}}
        repaint._tint_lottie(clean, self.BLUE)
        self.assertEqual(clean["g"]["k"]["k"][1:4], [0.0, 0.0, 1.0])

        ragged = {"g": {"k": {"a": 0, "k": [0.0, 1.0, 0.0, 0.0, 1.0, 0.5]}}}
        before = copy.deepcopy(ragged["g"]["k"]["k"])
        repaint._tint_lottie(ragged, self.BLUE)
        self.assertEqual(ragged["g"]["k"]["k"], before)


class TheRepaintApiIsStillWhereCallersLookForIt(unittest.TestCase):
    """It moved to `emojikit.repaint`; `fetch_emoji_ids` and the existing tests
    reach for it on `media`."""

    def test_media_re_exports_the_public_names(self):
        self.assertIs(media.parse_tint, repaint.parse_tint)
        self.assertIs(media.repaint_in_place, repaint.repaint_in_place)


if __name__ == "__main__":
    unittest.main()
