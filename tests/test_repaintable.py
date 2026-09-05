"""Emoji Telegram repaints must be flagged before they reach the catalog.

A repaintable emoji does not keep the colour you see: the client OVERRIDES it
with the text or accent colour. Republished into one of our sets -- which are
created without that flag, and the Bot API has no method to add it afterwards --
it renders its STORED art instead, which may be flat black or may be full
colour. The source pack is precisely where you cannot tell which.

That is not hypothetical. `5354899958329784877` from Telegram's built-in
`TopicIcons` was ingested, published into pack 2, reported as "why is it black?",
and had to be replaced sticker by sticker. These tests exist so the next one is
caught at ingest instead.
"""

from __future__ import annotations

import gzip
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_pack as bp  # noqa: E402
import fetch_emoji_ids  # noqa: E402
import fetch_pack  # noqa: E402
from emojikit import media  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402

from tests.test_fetch_pack_limit import FakeTelegram, _png_bytes  # noqa: E402


class TheFlagIsOnTheStickerNotTheSet(unittest.TestCase):
    """Reading the SET is how this gets missed."""

    def test_a_repaintable_sticker_is_recognised(self):
        self.assertTrue(media.is_repaintable({"needs_repainting": True}))

    def test_an_ordinary_sticker_is_not(self):
        self.assertFalse(media.is_repaintable({"emoji": "\U0001F600"}))
        self.assertFalse(media.is_repaintable({"needs_repainting": False}))

    def test_absence_is_not_repaintable(self):
        """Telegram omits the field entirely for ordinary stickers."""
        self.assertFalse(media.is_repaintable({}))

    def test_the_set_level_answer_is_not_the_stickers_answer(self):
        """Measured on TopicIcons: 160/160 stickers True, set field None.

        A gate that read ``sset.get("needs_repainting")`` would pass every one
        of them through.
        """
        sset = {"needs_repainting": None,
                "stickers": [{"needs_repainting": True} for _ in range(3)]}
        self.assertFalse(media.is_repaintable(sset))
        self.assertTrue(all(media.is_repaintable(s) for s in sset["stickers"]))


class TheGateAsksBeforeIngesting(unittest.TestCase):
    def _gate(self, labels, **kw) -> tuple[bool, str]:
        err = io.StringIO()
        with redirect_stderr(err):
            return bp.repaintable_gate(labels, **kw), err.getvalue()

    def test_nothing_repaintable_asks_nothing(self):
        asked = []
        ok, out = self._gate([], prompt=lambda p: asked.append(p) or "n")
        self.assertTrue(ok)
        self.assertEqual(asked, [], "an ordinary batch must not prompt at all")
        self.assertEqual(out, "")

    def test_yes_keeps_them(self):
        ok, out = self._gate(["a", "b"], prompt=lambda p: "y")
        self.assertTrue(ok)
        self.assertIn("REPAINTABLE", out)

    def test_anything_but_yes_skips(self):
        for answer in ("n", "", "no", "maybe", "  N  "):
            with self.subTest(answer=answer):
                ok, _ = self._gate(["a"], prompt=lambda p, a=answer: a)
                self.assertFalse(ok, "only an explicit yes may proceed")

    def test_skip_and_keep_never_prompt(self):
        for mode, expected in (("skip", False), ("keep", True)):
            with self.subTest(mode=mode):
                def refuse(_p, m=mode):
                    self.fail(f"--repaintable {m} must not ask")
                ok, _ = self._gate(["a"], mode=mode, prompt=refuse)
                self.assertIs(ok, expected)

    def test_with_no_terminal_the_answer_is_skip(self):
        """Nobody can answer, and skipping is the reversible half.

        A skipped emoji is one re-run away with --repaintable keep. One already
        published into a live set has to be replaced sticker by sticker.
        """
        real = sys.stdin
        sys.stdin = io.StringIO()          # a StringIO is not a tty
        try:
            ok, out = self._gate(["a"])
        finally:
            sys.stdin = real
        self.assertFalse(ok)
        self.assertIn("nobody answered", out)
        self.assertIn("--repaintable keep", out, "the way back must be named")

    def test_an_unanswerable_prompt_is_a_no_not_a_crash(self):
        """isatty() is not enough, and a real run proved it.

        Under Git Bash, `fetch_emoji_ids.py ... < /dev/null` still reports a
        tty; input() then raised EOFError and killed the whole ingest with an
        uncaught traceback. The fakes never saw it because they inject `prompt`.
        """
        for boom in (EOFError, KeyboardInterrupt):
            with self.subTest(boom=boom.__name__):
                def raiser(_p, exc=boom):
                    raise exc()
                ok, out = self._gate(["a"], prompt=raiser)
                self.assertFalse(ok)
                self.assertIn("nobody answered", out)

    def test_the_warning_names_them_and_bounds_the_list(self):
        _, out = self._gate([f"id{i}" for i in range(30)], mode="skip")
        self.assertIn("30 of these emoji are REPAINTABLE", out)
        self.assertIn("id0", out)
        self.assertIn("+22 more", out, "a 30-item list must not flood the console")


class _RepaintingTG(FakeTelegram):
    """A pack whose stickers at ``marked`` are repaintable."""

    def __init__(self, n: int, marked: tuple[int, ...]):
        super().__init__(n)
        for i in marked:
            self.stickers[i]["needs_repainting"] = True


class FetchPackHonoursTheGate(unittest.TestCase):
    def _fetch(self, tg, data: Path, mode: str) -> dict[str, int]:
        tmp = data / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        with Catalog(data / "catalog.db") as cat:
            with redirect_stderr(io.StringIO()):
                return fetch_pack.fetch_one(tg, cat, "pack", data, tmp,
                                            repaintable=mode)

    def test_skip_leaves_them_out_of_the_catalog(self):
        with tempfile.TemporaryDirectory() as t:
            tg = _RepaintingTG(4, marked=(1, 3))
            counts = self._fetch(tg, Path(t), "skip")
        self.assertEqual(counts["new"], 2)
        self.assertEqual(counts["repaintable"], 2)
        self.assertEqual(sorted(tg.downloaded), ["0", "2"],
                         "a skipped emoji must not even be downloaded")

    def test_keep_ingests_them(self):
        with tempfile.TemporaryDirectory() as t:
            tg = _RepaintingTG(4, marked=(1, 3))
            counts = self._fetch(tg, Path(t), "keep")
        self.assertEqual(counts["new"], 4)
        self.assertEqual(counts["repaintable"], 0)

    def test_an_ordinary_pack_is_untouched(self):
        with tempfile.TemporaryDirectory() as t:
            tg = _RepaintingTG(3, marked=())
            counts = self._fetch(tg, Path(t), "skip")
        self.assertEqual(counts["new"], 3)
        self.assertEqual(counts["repaintable"], 0)


class _IdsTG:
    """getCustomEmojiStickers + download, with per-id repaint flags."""

    def __init__(self, marked: tuple[str, ...]):
        self.marked = set(marked)
        self.downloaded: list[str] = []

    def get_custom_emoji_stickers(self, ids):
        out = []
        for n, cid in enumerate(ids):
            st = {"custom_emoji_id": cid, "file_id": str(n),
                  "file_unique_id": f"FU-{cid}", "emoji": "\U0001F600"}
            if cid in self.marked:
                st["needs_repainting"] = True
            out.append(st)
        return out

    def download_file(self, file_id, dest: Path):
        self.downloaded.append(file_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_png_bytes(int(file_id)))


class FetchEmojiIdsHonoursTheGate(unittest.TestCase):
    IDS = ["101", "102", "103"]

    def _fetch(self, tg, data: Path, mode: str) -> dict[str, int]:
        tmp = data / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        with Catalog(data / "catalog.db") as cat:
            with redirect_stderr(io.StringIO()):
                return fetch_emoji_ids.fetch_ids(tg, cat, self.IDS, data, tmp,
                                                 mode)

    def test_skip_leaves_them_out_of_the_catalog(self):
        with tempfile.TemporaryDirectory() as t:
            tg = _IdsTG(marked=("102",))
            counts = self._fetch(tg, Path(t), "skip")
        self.assertEqual(counts["new"], 2)
        self.assertEqual(counts["repaintable"], 1)
        self.assertNotIn("1", tg.downloaded, "id 102 is index 1 and was skipped")

    def test_keep_ingests_them(self):
        with tempfile.TemporaryDirectory() as t:
            tg = _IdsTG(marked=("102",))
            counts = self._fetch(tg, Path(t), "keep")
        self.assertEqual(counts["new"], 3)
        self.assertEqual(counts["repaintable"], 0)


class BothEntryPointsOfferTheSameChoice(unittest.TestCase):
    """One flag, one vocabulary: a mode that works for one must work for both."""

    def test_the_modes_are_shared(self):
        self.assertEqual(bp.REPAINT_MODES, ("ask", "skip", "keep"))

    def test_ask_is_the_default_in_both(self):
        for mod in (fetch_pack, fetch_emoji_ids):
            with self.subTest(mod=mod.__name__):
                src = Path(mod.__file__).read_text(encoding="utf-8")
                self.assertIn('choices=REPAINT_MODES, default="ask"', src)


class TintParsing(unittest.TestCase):
    """A bad colour must fail at the CLI, not halfway through a download."""

    def test_it_accepts_both_spellings(self):
        for text in ("#FF8800", "ff8800"):
            with self.subTest(text=text):
                self.assertEqual(media.parse_tint(text), (255, 136, 0))

    def test_it_refuses_anything_else(self):
        for text in ("white", "#FFF", "#GGGGGG", "#FF88000", ""):
            with self.subTest(text=text):
                with self.assertRaises(media.MediaError):
                    media.parse_tint(text)


def _tgs_bytes(doc: dict) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(doc).encode("utf-8"))
    return buf.getvalue()


_LOTTIE = {
    "v": "5.5", "fr": 30, "ip": 0, "op": 30, "w": 512, "h": 512,
    "layers": [{"ty": 4, "ks": {}, "shapes": [{"ty": "gr", "it": [
        {"ty": "fl", "c": {"a": 0, "k": [1.0, 0.0, 0.0, 1.0]}},
        {"ty": "st", "c": {"a": 0, "k": [0.0, 1.0, 0.0, 1.0]}},
        {"ty": "gf", "g": {"p": 2, "k": {"a": 0, "k": [
            0.0, 0.0, 0.0, 1.0,
            1.0, 1.0, 1.0, 0.0]}}},
    ]}]}],
}


class BakingTheRepaintOurselves(unittest.TestCase):
    """The flag cannot be set, but the look can be reproduced in the asset."""

    TINT = (255, 255, 255)

    def test_a_static_emoji_keeps_its_shape_and_loses_its_hue(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "s.png"
            src.write_bytes(_png_bytes(3))
            with Image.open(src) as im:
                before = im.convert("RGBA").getchannel("A").tobytes()

            self.assertTrue(media.repaint_in_place(src, "static", self.TINT))

            with Image.open(src) as im:
                after = im.convert("RGBA")
            # The silhouette is the point: alpha must survive byte for byte.
            self.assertEqual(after.getchannel("A").tobytes(), before)
            visible = [p for p in after.get_flattened_data() if p[3] > 0]
            self.assertTrue(visible)
            self.assertEqual({p[:3] for p in visible}, {self.TINT})

    def test_an_animated_emoji_is_recoloured_through_its_lottie(self):
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "a.tgs"
            src.write_bytes(_tgs_bytes(_LOTTIE))

            self.assertTrue(media.repaint_in_place(src, "animated", self.TINT))

            doc = json.loads(gzip.decompress(src.read_bytes()).decode("utf-8"))
            items = doc["layers"][0]["shapes"][0]["it"]
            # Fill AND stroke, because a mark drawn as an outline is still a mark.
            self.assertEqual(items[0]["c"]["k"], [1.0, 1.0, 1.0, 1.0])
            self.assertEqual(items[1]["c"]["k"], [1.0, 1.0, 1.0, 1.0])
            # A gradient keeps its OFFSETS (0.0 and 1.0) and loses its colours,
            # or the ramp collapses and the shape changes.
            self.assertEqual(items[2]["g"]["k"]["k"],
                             [0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
            # Still a real animation, not a flattened frame.
            self.assertEqual(doc["op"], 30)

    def test_video_is_refused_rather_than_silently_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "v.webm"
            src.write_bytes(b"not really a webm")
            before = src.read_bytes()
            self.assertFalse(media.repaint_in_place(src, "video", self.TINT))
            self.assertEqual(src.read_bytes(), before)


class TintAnswersTheGate(unittest.TestCase):
    """--tint fixes what the gate warns about, so it must not also skip."""

    IDS = ["101", "102", "103"]

    def _fetch(self, tg, data: Path, mode: str, tint):
        tmp = data / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        with Catalog(data / "catalog.db") as cat:
            with redirect_stderr(io.StringIO()):
                counts = fetch_emoji_ids.fetch_ids(tg, cat, self.IDS, data, tmp,
                                                   mode, tint)
            rows = {i.content_key: i for i in cat.all_items()}                 if hasattr(cat, "all_items") else {}
        return counts, rows

    def test_a_tinted_run_ingests_what_skip_would_have_dropped(self):
        with tempfile.TemporaryDirectory() as t:
            tg = _IdsTG(marked=("102",))
            counts, _ = self._fetch(tg, Path(t), "skip", (255, 255, 255))
        self.assertEqual(counts["new"], 3, "the repaintable one must be kept")
        self.assertEqual(counts["repaintable"], 0)
        self.assertEqual(counts["repainted"], 1, "only the flagged one is tinted")

    def test_without_a_tint_skip_still_drops_it(self):
        with tempfile.TemporaryDirectory() as t:
            tg = _IdsTG(marked=("102",))
            counts, _ = self._fetch(tg, Path(t), "skip", None)
        self.assertEqual(counts["new"], 2)
        self.assertEqual(counts["repaintable"], 1)
        self.assertEqual(counts["repainted"], 0)

    def test_the_tint_is_recorded_on_the_item(self):
        with tempfile.TemporaryDirectory() as t:
            data = Path(t)
            tg = _IdsTG(marked=("102",))
            self._fetch(tg, data, "keep", (255, 0, 0))
            with Catalog(data / "catalog.db") as cat:
                marks = [kw for i in cat.pending() for kw in i.keywords
                         if kw.startswith("tint:")]
        # Provenance, same reason premium-id: is kept: a recoloured asset does
        # not look like its source and nothing else records why.
        self.assertEqual(marks, ["tint:#FF0000"])


if __name__ == "__main__":
    unittest.main()
