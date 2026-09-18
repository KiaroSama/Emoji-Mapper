"""Real preview responses keep dimensions, motion and transparent video posters."""
from __future__ import annotations

import gzip
import io
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import error, request

from PIL import Image

from emojikit import panel
from emojikit import media
from emojikit import panel_preview


class PreviewResponses(unittest.TestCase):
    def test_sized_motion_and_video_posters_are_real_cached_images(self):
        temp_root = Path(__file__).resolve().parent.parent / "logs" / "test-temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(temp.cleanup)
        data = Path(temp.name)
        png, tgs, video = data / "source.png", data / "source.tgs", data / "source.webm"
        with Image.new("RGBA", (100, 100), (240, 20, 40, 128)) as img:
            img.save(png, "PNG")
        doc = {"v": "5.7.4", "w": 512, "h": 512, "fr": 30, "ip": 0, "op": 30,
               "layers": [{"ty": 1, "ind": 1, "sw": 80, "sh": 80, "sc": "#ff0000",
                           "ip": 0, "op": 30, "st": 0, "ks": {
                               "o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0},
                               "a": {"a": 0, "k": [0, 0, 0]},
                               "s": {"a": 0, "k": [100, 100, 100]},
                               "p": {"a": 1, "k": [
                                   {"t": 0, "s": [80, 200, 0], "e": [400, 200, 0],
                                    "i": {"x": 1, "y": 1}, "o": {"x": 0, "y": 0}},
                                   {"t": 30, "s": [400, 200, 0]}]}}}]}
        tgs.write_bytes(gzip.compress(json.dumps(doc).encode("utf-8"), mtime=0))
        subprocess.run([media.ffmpeg_path(), "-v", "error", "-y", "-loop", "1", "-i", str(png),
                        "-t", "0.1", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                        "-threads", "1", "-auto-alt-ref", "0", str(video)],
                       check=True, timeout=20, stdin=subprocess.DEVNULL, capture_output=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), panel.make_handler(
            [], {"animated": tgs, "video": video, "static": png}, data / "catalog.db", "test-token"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            self.assertFalse(thread.is_alive())

        self.addCleanup(stop)
        base = f"http://127.0.0.1:{server.server_address[1]}/preview/"

        def get(path):
            with request.urlopen(base + path, timeout=10) as response:
                return response.read()

        moving = get("animated?fps=10&size=72")
        with Image.open(io.BytesIO(moving)) as img:
            self.assertEqual(img.size, (72, 72))
            self.assertEqual(img.n_frames, 10)
        cache_times = {p.name: p.stat().st_mtime_ns for p in (data / "preview").iterdir()}
        self.assertEqual(get("animated?fps=10&size=72"), moving)
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in (data / "preview").iterdir()}, cache_times)
        for kind in ("static", "video", "animated"):
            with self.subTest(format=kind), Image.open(io.BytesIO(get(kind + "?still=1&size=72"))) as img:
                self.assertEqual(img.size, (72, 72))
                self.assertEqual(getattr(img, "n_frames", 1), 1)
                if kind == "video":
                    self.assertLess(img.convert("RGBA").getextrema()[3][1], 200,
                                    "the video decoder silently dropped alpha")
        with self.assertRaises(error.HTTPError) as refused:
            get("animated?fps=1000&size=10000")
        self.assertEqual(refused.exception.code, 400)


class TheWarmUpRendersBeforeTheScroll(unittest.TestCase):
    """Every preview miss used to be paid at scroll time, behind a bound of 2.

    Measured on the owner's catalog: one animation costs ~155 ms and one video
    poster ~613 ms, so a cold tier arrived two files at a time -- which is what
    "the animations load in pieces" was. The warm-up renders what the page is
    about to ask for, in grid order, so the scroll meets a warm cache.
    """

    def test_it_renders_grid_order_and_a_bad_file_cannot_stop_it(self):
        temp_root = Path(__file__).resolve().parent.parent / "logs" / "test-temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(temp.cleanup)
        data = Path(temp.name)
        png, broken = data / "source.png", data / "broken.png"
        with Image.new("RGBA", (100, 100), (10, 200, 90, 255)) as img:
            img.save(png, "PNG")
        broken.write_bytes(b"not an image at all")
        view = [{"key": "logo", "fmt": "static", "isLogo": True},
                {"key": "bad", "fmt": "static"},
                {"key": "good", "fmt": "static"}]
        by_key = {"logo": png, "bad": broken, "good": png}

        rendered = panel_preview.warm(view, by_key, data / "catalog.db", 15)

        cached = sorted(p.name for p in (data / "preview").iterdir())
        # The unreadable file is skipped, the one after it is still rendered:
        # a warm-up that dies on the first bad row warms nothing.
        self.assertEqual(rendered, 1, cached)
        self.assertEqual(len(cached), 1, cached)
        # The logo is a preview-only card with no catalog media; asking for it
        # would 404 the same way the page never does.
        self.assertNotIn("logo", "".join(cached))

    def test_a_set_stop_event_ends_it_without_finishing_the_catalog(self):
        temp_root = Path(__file__).resolve().parent.parent / "logs" / "test-temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(temp.cleanup)
        data = Path(temp.name)
        png = data / "source.png"
        with Image.new("RGBA", (100, 100), (10, 200, 90, 255)) as img:
            img.save(png, "PNG")
        stop = threading.Event()
        stop.set()

        # Ctrl+C must not wait for a thousand renders to finish.
        rendered = panel_preview.warm(
            [{"key": f"k{i}", "fmt": "static"} for i in range(50)],
            {f"k{i}": png for i in range(50)}, data / "catalog.db", 15, stop=stop)

        self.assertEqual(rendered, 0)
        self.assertFalse((data / "preview").exists())


if __name__ == "__main__":
    unittest.main()
