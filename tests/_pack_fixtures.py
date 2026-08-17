"""Shared PNG builder for the build_pack test modules.

`test_resume_safety`, `test_telegram_client` and the rest of the build_pack
family all need the same throwaway 100x100 image. It lives here rather than in
each module because a duplicated fixture drifts: this suite has already been
bitten by a fake that silently stopped checking what it claimed to.

Not named `test_*` on purpose -- `unittest discover -p "test_*.py"` would
otherwise try to run it as a test module.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image


def _png(path: Path, color=(10, 20, 30, 255)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (100, 100), color).save(path, "PNG")
