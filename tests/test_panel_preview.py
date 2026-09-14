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


if __name__ == "__main__":
    unittest.main()
