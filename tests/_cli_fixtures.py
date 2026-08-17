"""Shared machinery for the entry-point contract test modules.

`_load_standalone` is why this file exists: every contract module below imports
a script as its own module object, and the loader carries a guard (the neutral
``sys.argv``) that a second copy would quietly drop. `DeadTelegram` and
`_noise_png_bytes` live here for the same reason -- a duplicated fake drifts
away from the thing it stands in for.

Consumers: `test_entry_point_contracts`, `test_coin_logo_cache`,
`test_coin_http`, `test_coin_ticker_map`, `test_verify_logos`,
`test_coin_cli_args`.

Not named `test_*` on purpose -- `unittest discover -p "test_*.py"` would
otherwise try to run it as a test module.
"""

from __future__ import annotations

import importlib.util
import io
import random
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402


def _load_standalone(path: Path, name: str):
    """Import a script as its own module object, with a neutral ``sys.argv``.

    The scripts parse their arguments inside ``main(argv)`` now, so nothing here
    depends on the command line at import. The neutral argv stays as a cheap
    guard against that regressing -- an import-time ``sys.argv`` read is exactly
    what made ``fetch_logos.py --help`` raise ValueError before argparse could
    answer. Loading a private module object also keeps the reload-based tests
    below from mutating what other test modules already imported.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "argv", [path.name]):
        spec.loader.exec_module(mod)
    return mod


class DeadTelegram:
    """Authenticates, but every pack lookup fails (deleted/misspelled names)."""

    def __init__(self, *_a, **_k):
        self.lookups: list[str] = []

    def get_me(self):
        return {"username": "testbot"}

    def get_sticker_set(self, name):
        self.lookups.append(name)
        raise RuntimeError("Bad Request: STICKERSET_INVALID")


def _noise_png_bytes(key: str, size: int = 64) -> bytes:
    """Deterministic per-key noise; two different keys never look alike.

    Flat colours are useless for image identity: a dHash compares neighbouring
    pixels, so every solid image hashes to zero and "is this sticker the one we
    uploaded?" would answer yes for any picture at all.
    """
    rnd = random.Random(key)
    img = Image.new("RGBA", (size, size))
    px = img.load()
    for x in range(size):
        for y in range(size):
            v = rnd.randrange(256)
            px[x, y] = (v, v, v, 255)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
