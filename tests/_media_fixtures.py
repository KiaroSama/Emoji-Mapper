"""Builders the media suites share: a PNG, an animated GIF, two probes.

One copy, because a fake that exists twice drifts away from the thing it stands
in for. `_make_png` also exists in `_panel_fixtures.py` under the same name and
a different signature -- different helpers for different suites, and neither is
the other's.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from emojikit import media  # noqa: E402

# Generating a 2-second clip takes well under a second; anything near this bound
# means ffmpeg is stuck, and an unbounded child can hang the whole suite.
FFMPEG_TIMEOUT = 120
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_png(path: Path, color, size=(80, 60), fmt="PNG") -> Path:
    img = Image.new("RGBA", size, color)
    img.save(path, format=fmt)
    return path


def _make_anim_gif(path: Path, size=(64, 64), frames=6) -> Path:
    imgs = []
    for i in range(frames):
        im = Image.new("RGB", size, (10 * i % 255, 50, 200))
        imgs.append(im)
    imgs[0].save(path, format="GIF", save_all=True, append_images=imgs[1:],
                 duration=80, loop=0)
    return path



def _clear_pixels(webm: Path) -> int:
    """Fully transparent pixels in frame 0, read with the ALPHA-AWARE decoder.

    ffmpeg's default vp9 decoder silently drops the separate alpha layer, so a
    naive probe reports every VP9 emoji as opaque -- including the ones that are
    fine. Measuring with the wrong decoder is how this bug hid.
    """
    raw = subprocess.run(
        [media.ffmpeg_path(), "-v", "error", "-c:v", "libvpx-vp9", "-i", str(webm),
         "-frames:v", "1", "-vf", "format=rgba", "-f", "rawvideo",
         "-pix_fmt", "rgba", "-"],
        capture_output=True, check=True).stdout
    return sum(1 for i in range(3, len(raw), 4) if raw[i] == 0)


def _ffmpeg_calls(cmds: list[list[str]]) -> list[list[str]]:
    """Only the ffmpeg launches, dropping the ffprobe that precedes them.

    Choosing a decoder means reading the file's real codec first, so an encode
    or a sample is now a probe plus a run through one ``_run``. A probe reads
    the header and stops; counting it as a decode would make every "one decode"
    assertion read two and say nothing about the expensive half.
    """
    return [c for c in cmds if "ffprobe" not in Path(c[0]).name.lower()]
