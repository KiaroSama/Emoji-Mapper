"""Fitting an image onto the 100x100 canvas must not touch its opacity.

F11. `fit_100` pasted the image using ITSELF as the mask, which is a composite
against the transparent canvas underneath: colour came back multiplied by alpha
and alpha came back multiplied by alpha again. A half-opaque red fitted to a
quarter-opaque dark red. Fully opaque art was unharmed -- alpha 255 is the
identity for that arithmetic -- which is why the defect survived every fixture
this project had.

Three modules carried byte-identical copies of the expression, so all three are
exercised here through their own public entry points.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tests._panel_fixtures import ROOT

sys.path.insert(0, str(ROOT))

from emojikit import make_emoji_pngs as mp
from emojikit import media


class FittingPreservesOpacity(unittest.TestCase):
    """The pixel the audit measured, plus the neighbours that prove the shape."""

    def assertPixel(self, got, want, *, resampled=False):
        """Alpha is always exact; colour tolerates resampling, never premultiply.

        LANCZOS moves a colour channel by a unit or two when it rescales, so an
        exact colour assertion on a resized pixel fails for a reason that has
        nothing to do with this defect. The bug being pinned multiplied colour
        by alpha/255 -- at alpha 150 that is a shift of 105, not of 1 -- so a
        delta of 2 still catches it while surviving a Pillow point release.
        Alpha gets no tolerance at all: squaring it is the other half of the
        bug, and it must come back byte-exact.
        """
        self.assertEqual(got[3], want[3], f"alpha changed: {got} != {want}")
        delta = 2 if resampled else 0
        for i, name in enumerate(("red", "green", "blue")):
            self.assertAlmostEqual(got[i], want[i], delta=delta,
                                   msg=f"{name} channel: {got} != {want}")

    def test_a_partly_transparent_pixel_keeps_its_colour_and_its_alpha(self):
        """The exact reproduction: (255, 0, 0, 128) fitted to (128, 0, 0, 64).

        Both halves were wrong. Colour was premultiplied (255 -> 128) and alpha
        was squared (128 -> 64), so the emoji shipped both darker and fainter
        than its source.
        """
        for alpha in (1, 64, 128, 192, 254):
            with self.subTest(alpha=alpha):
                src = Image.new("RGBA", (media.SIZE, media.SIZE), (255, 0, 0, alpha))
                got = media.fit_100(src).getpixel((50, 50))
                self.assertPixel(got, (255, 0, 0, alpha))

    def test_opaque_art_is_the_control_that_always_passed(self):
        """Pinned deliberately: this is the case that hid the bug, so a future
        change that only ever gets tested on opaque art fails here first."""
        src = Image.new("RGBA", (media.SIZE, media.SIZE), (12, 200, 90, 255))
        self.assertPixel(media.fit_100(src).getpixel((50, 50)), (12, 200, 90, 255))

    def test_every_fitting_entry_point_shares_one_implementation(self):
        """The bug had to be found three times because the expression was
        copied three times. One implementation is the fix; these identities are
        what stop a fourth copy being introduced quietly."""
        from coins import _paprika_api

        self.assertIs(mp._fit_100, media.fit_100)
        self.assertIs(mp._trim, media._trim)
        self.assertIs(_paprika_api._fit_100, media.fit_100)

    def test_a_resized_image_keeps_its_opacity(self):
        """The audit's case needed no resize. Scaling runs a different branch --
        LANCZOS then paste -- so it gets its own assertion."""
        src = Image.new("RGBA", (40, 40), (0, 100, 255, 96))
        out = media.fit_100(src)
        self.assertEqual(out.size, (media.SIZE, media.SIZE))
        self.assertPixel(out.getpixel((50, 50)), (0, 100, 255, 96), resampled=True)

    def test_a_cutout_stays_a_cutout(self):
        """A transparent hole must not acquire colour, and the ring around it
        must not lose any."""
        src = Image.new("RGBA", (60, 60), (255, 128, 0, 200))
        for x in range(20, 40):
            for y in range(20, 40):
                src.putpixel((x, y), (0, 0, 0, 0))
        out = media.fit_100(src)
        self.assertEqual(out.getpixel((50, 50))[3], 0, "the hole filled in")
        self.assertPixel(out.getpixel((5, 5)), (255, 128, 0, 200), resampled=True)

    def test_a_transparent_border_is_trimmed_not_composited(self):
        """`_trim` crops to the alpha bbox first; what survives must still hold
        its own alpha rather than the alpha it would have had after a paste."""
        src = Image.new("RGBA", (80, 80), (0, 0, 0, 0))
        for x in range(30, 50):
            for y in range(30, 50):
                src.putpixel((x, y), (10, 220, 30, 150))
        out = media.fit_100(src)
        self.assertPixel(out.getpixel((50, 50)), (10, 220, 30, 150), resampled=True)

    def test_a_partial_alpha_gradient_survives_end_to_end(self):
        """Through the real file-writing entry point, not just the fitter: a
        PNG on disk is what a pack actually ships."""
        src = Image.new("RGBA", (media.SIZE, media.SIZE))
        for x in range(media.SIZE):
            for y in range(media.SIZE):
                src.putpixel((x, y), (0, 0, 255, max(1, x * 255 // (media.SIZE - 1))))
        with tempfile.TemporaryDirectory() as tmp:
            srcp = Path(tmp) / "in.png"
            src.save(srcp)
            out = media.to_static_png(srcp, Path(tmp) / "out.png")
            got = Image.open(out).convert("RGBA")
        # Sample away from the edges: LANCZOS touches the outermost columns.
        for x in (25, 50, 75):
            self.assertPixel(got.getpixel((x, 50)), (0, 0, 255, x * 255 // 99))


class CompositingOntoAnOpaqueBackdropIsStillCorrect(unittest.TestCase):
    """Not every masked paste was wrong, and the fix must not "fix" this one.

    `verify_logos` flattens a transparent mark onto white before comparing it
    with a reference render. There the mask IS the point: the destination is
    opaque, so the result is real alpha compositing and the output has no alpha
    channel to double.
    """

    def test_flattening_onto_white_still_blends(self):
        rgba = Image.new("RGBA", (10, 10), (255, 0, 0, 128))
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[-1])
        r, g, b = flat.getpixel((5, 5))
        self.assertEqual(r, 255)
        self.assertAlmostEqual(g, 127, delta=2)
        self.assertAlmostEqual(b, 127, delta=2)


if __name__ == "__main__":
    unittest.main()
