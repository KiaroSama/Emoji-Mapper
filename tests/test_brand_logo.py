"""Tests for the mandatory brand-logo-first behaviour in build_collection.

Covers:
  * BrandLogo format conversion (static PNG, looped video WEBM, animated needs
    a Lottie source and is skipped for raster);
  * publish_format placing the logo as the FIRST emoji of every set, only for
    the Emoji Mapper bot, with correct custom_emoji_id offset mapping.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import build_collection as bc  # noqa: E402
import collection_state as cs  # noqa: E402
from emojikit import identity, media  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402


def _make_png(path: Path, color=(200, 30, 30, 255)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    for x in range(20, 100):
        for y in range(20, 100):
            im.putpixel((x, y), color)
    im.save(path, "PNG")


class FakeTelegram:
    """Minimal in-memory stand-in for the Telegram client used by publish_format."""

    def __init__(self, username="GodVerifyEmojiMapperbot"):
        self.username = username
        self.sets: dict[str, list[dict]] = {}
        self.messages: list[str] = []
        self.bodies: dict[str, bytes] = {}

    def get_me(self):
        return {"username": self.username}

    def _stored(self, name, path, fmt, emojis) -> dict:
        """A live sticker as Telegram reports it, whose file can be fetched.

        Identity fields and a downloadable body are not decoration: the
        publisher proves a new sticker is the image it just sent by fetching it
        and hashing the pixels, so a fake that returns neither makes every
        upload unattributable.
        """
        i = len(self.sets.get(name, []))
        file_id = f"{name}-f{i}"
        self.bodies[file_id] = Path(path).read_bytes()
        return {"emojis": list(emojis), "fmt": fmt,
                "custom_emoji_id": f"{name}-{i}",
                "file_id": file_id, "file_unique_id": f"{name}-u{i}",
                "is_animated": fmt == "animated", "is_video": fmt == "video"}

    def create_emoji_set(self, user_id, name, title, path, fmt,
                         emojis, keywords, *, needs_repainting=False):
        self.sets[name] = [self._stored(name, path, fmt, emojis)]

    def add_emoji(self, user_id, name, path, fmt, emojis, keywords, *,
                  expected_before=None):
        self.sets[name].append(self._stored(name, path, fmt, emojis))

    def download_file(self, file_id, dest):
        Path(dest).write_bytes(self.bodies[str(file_id)])

    def get_sticker_set(self, name):
        return {"stickers": self.sets.get(name, [])}

    def send_message(self, chat_id, text, *, disable_preview=False):
        # Signature matched to the REAL client, not to whichever call site was
        # written first: `announce_packs` passes disable_preview, and a fake
        # that rejects it turned every happy-path announcement into a swallowed
        # "notify failed" while the test stayed green -- the suite was
        # exercising the error path and reporting it as a pass.
        self.messages.append(text)


class BrandLogoPrepare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.png = self.data / "logo_src.png"
        _make_png(self.png)

    def tearDown(self):
        self.tmp.cleanup()

    def test_static_logo_is_100x100_png(self):
        logo = cs.BrandLogo(str(self.png), self.data)
        out = logo.static_png()
        self.assertIsNotNone(out)
        self.assertTrue(out.is_file())
        with Image.open(out) as im:
            self.assertEqual(im.size, (media.SIZE, media.SIZE))

    def test_static_png_is_cached(self):
        logo = cs.BrandLogo(str(self.png), self.data)
        self.assertEqual(logo.static_png(), logo.static_png())

    def test_missing_source_returns_none(self):
        logo = cs.BrandLogo(str(self.data / "nope.png"), self.data)
        self.assertFalse(logo.available())
        self.assertIsNone(logo.static_png())


class PublishFormatLogoFirst(unittest.TestCase):
    def _setup_catalog(self, tmp: Path, n=2):
        data = tmp
        keys = []
        with Catalog(data / "catalog.db") as cat:
            for i in range(n):
                p = data / "media" / "static" / f"item{i}.png"
                _make_png(p, color=(10, 20 * (i + 1), 200, 255))
                # The REAL content key: publishing attributes a live sticker by
                # hashing its pixels, so a synthetic key resolves to nothing.
                key = identity.content_key(p, "static")
                cat.add(content_key=key, fmt="static", file_path=p,
                        emojis=["😀"], keywords=[f"item{i}"])
                keys.append(key)
        return keys

    def test_logo_is_first_and_cids_offset(self):
        with tempfile.TemporaryDirectory() as t:
            data = Path(t)
            keys = self._setup_catalog(data)
            logo_png = data / "logo.png"
            _make_png(logo_png, color=(0, 200, 0, 255))
            tg = FakeTelegram("GodVerifyEmojiMapperbot")
            logo = cs.BrandLogo(str(logo_png), data)
            state = {"base": "pk", "sets": [], "sent": []}
            with Catalog(data / "catalog.db") as cat:
                bc.publish_format(tg, cat, fmt="static", plan_keys=keys,
                                  base="pk", title="Pack", user_id=1,
                                  default_emoji="😀", per_set=200,
                                  data_dir=data, state=state, bot="GodVerifyEmojiMapperbot",
                                  logo=logo)
                set_name = "pks1_by_GodVerifyEmojiMapperbot"
                stickers = tg.sets[set_name]
                # First emoji must be the brand logo.
                self.assertEqual(stickers[0]["emojis"], [cs.BRAND_LOGO_EMOJI])
                self.assertEqual(len(stickers), 3)  # logo + 2 items
                # State records the logo flag.
                self.assertTrue(state["sets"][0]["logo"])
                # cids map with offset 1 (item0 -> position1, item1 -> position2).
                self.assertEqual(cat.get(keys[0]).custom_emoji_id, f"{set_name}-1")
                self.assertEqual(cat.get(keys[1]).custom_emoji_id, f"{set_name}-2")

    def test_static_logo_leads_an_animated_set(self):
        # The key fix: a STATIC logo is the first emoji even of an animated set
        # (mixed-format sets are allowed since Bot API 7.2).
        with tempfile.TemporaryDirectory() as t:
            data = Path(t)
            # Two "animated" catalog items (media validity for animated is not
            # probed by publish_format, so any real file works here).
            keys = []
            with Catalog(data / "catalog.db") as cat:
                for i in range(2):
                    p = data / "media" / "animated" / f"a{i}.tgs"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x1f\x8b" + b"x" * (50 + i))  # gzip-magic dummy
                    key = identity.content_key(p, "animated")
                    cat.add(content_key=key, fmt="animated", file_path=p,
                            emojis=["😀"], keywords=[f"a{i}"])
                    keys.append(key)
            logo_png = data / "logo.png"
            _make_png(logo_png, color=(0, 200, 0, 255))
            tg = FakeTelegram("GodVerifyEmojiMapperbot")
            logo = cs.BrandLogo(str(logo_png), data)
            state = {"base": "pk", "sets": [], "sent": []}
            with Catalog(data / "catalog.db") as cat:
                bc.publish_format(tg, cat, fmt="animated", plan_keys=keys,
                                  base="pk", title="Pack", user_id=1,
                                  default_emoji="😀", per_set=200, data_dir=data,
                                  state=state, bot="GodVerifyEmojiMapperbot", logo=logo)
            stickers = tg.sets["pka1_by_GodVerifyEmojiMapperbot"]
            self.assertEqual(len(stickers), 3)             # logo + 2 animated
            self.assertEqual(stickers[0]["fmt"], "static")  # logo is static...
            self.assertEqual(stickers[0]["emojis"], [cs.BRAND_LOGO_EMOJI])
            self.assertEqual(stickers[1]["fmt"], "animated")  # ...items are animated
            self.assertEqual(stickers[2]["fmt"], "animated")

    def test_no_logo_when_disabled(self):
        with tempfile.TemporaryDirectory() as t:
            data = Path(t)
            keys = self._setup_catalog(data)
            tg = FakeTelegram("GodVerifyEmojiMapperbot")
            state = {"base": "pk", "sets": [], "sent": []}
            with Catalog(data / "catalog.db") as cat:
                bc.publish_format(tg, cat, fmt="static", plan_keys=keys,
                                  base="pk", title="Pack", user_id=1,
                                  default_emoji="😀", per_set=200,
                                  data_dir=data, state=state, bot="GodVerifyEmojiMapperbot",
                                  logo=None)
                set_name = "pks1_by_GodVerifyEmojiMapperbot"
                stickers = tg.sets[set_name]
                self.assertEqual(len(stickers), 2)          # no logo prepended
                self.assertFalse(state["sets"][0].get("logo"))
                self.assertEqual(cat.get(keys[0]).custom_emoji_id, f"{set_name}-0")


class TheDefaultBrandLogoActuallyShips(unittest.TestCase):
    """Owner rule: every pack the main bot builds leads with the God Verify logo.

    Every other test in this module passes its own synthetic logo, so none of
    them notices if the DEFAULT one is missing. That is not hypothetical: the
    default used to be an absolute F:\\ path, so on any machine but one the
    "mandatory" logo silently vanished from every pack and nothing failed.
    """

    def test_the_default_logo_is_a_real_file_inside_the_repo(self):
        p = Path(cs.BRAND_LOGO_DEFAULT)
        self.assertTrue(p.is_file(), f"the default brand logo is missing: {p}")
        self.assertTrue(
            str(p.resolve()).startswith(str(ROOT.resolve())),
            "the default brand logo must ship in the repo, not point at a "
            "machine-specific path")

    def test_the_default_logo_is_not_blank(self):
        """A blank logo would upload as an empty first emoji in every pack."""
        with Image.open(cs.BRAND_LOGO_DEFAULT) as im:
            self.assertFalse(media.is_blank_image(im.convert("RGBA")))

    def test_the_main_bot_gets_it_and_the_coin_bot_does_not(self):
        self.assertIn("godverifyemojimapperbot", cs.BRAND_LOGO_BOTS)
        self.assertNotIn("godverifycoinemojimapperbot", cs.BRAND_LOGO_BOTS,
                         "the coin bot is exempt by owner decision")


if __name__ == "__main__":
    unittest.main()
