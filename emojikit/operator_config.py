"""The operator's own identities, read from configuration -- never from source.

Which bots lead their packs with a brand logo, the logo itself, the pack bases
and the coin pack title all belong to whoever runs these tools. The repository
is public (docs/adr/0001), so none of them has a default: an unset key is
UNKNOWN, and whoever needs it stops and names it rather than publishing under
another operator's identity.

`value()` never raises, so a module may read a key at import time (the project
rule: nothing raises at import). `require()` is the stop, called before the
first change.
"""

from __future__ import annotations

import os
from pathlib import Path

from emojikit.errors import OperatorConfigMissing

ROOT = Path(__file__).resolve().parent.parent

WHAT = {
    "BRAND_LOGO_BOTS": "bots whose packs lead with your brand logo; empty = none",
    "BRAND_LOGO_PATH": "your brand logo image",
    "COLLECTION_PACK_BASE": "your general collection pack base",
    "COIN_PACK_BASE": "your coin pack base",
    "COIN_PACK_TITLE": "your coin pack title",
    "EMOJI_ARCHIVE_DIR": "your pack archive folder",
}


def value(key: str) -> str:
    return os.environ.get(key, "").strip()


def require(*keys: str) -> None:
    """Raise OperatorConfigMissing naming every key in `keys` that is unset."""
    missing = [k for k in keys if not value(k)]
    if missing:
        raise OperatorConfigMissing(
            "not configured: "
            + "; ".join(f"{k} ({WHAT.get(k, 'setting')})" for k in missing)
            + ". Set it in .env (see .env.example). Nothing was changed.")


def brand_logo_bots(strict: bool = True) -> frozenset[str]:
    """Bot usernames, lower-case without '@', whose packs lead with the logo.

    Set-but-empty is the explicit "no bot" answer; UNSET is unknown and raises
    unless `strict` is off (a preview may show no logo; a publish may not).
    """
    if "BRAND_LOGO_BOTS" not in os.environ:
        if strict:
            raise OperatorConfigMissing(
                f"not configured: BRAND_LOGO_BOTS ({WHAT['BRAND_LOGO_BOTS']}). "
                "Set it in .env (see .env.example). Nothing was changed.")
        return frozenset()
    raw = os.environ["BRAND_LOGO_BOTS"].replace(",", " ").split()
    return frozenset(b.lstrip("@").lower() for b in raw)


def brand_logo_path(strict: bool = True) -> Path | None:
    """The configured logo; a relative path is relative to the project root."""
    raw = value("BRAND_LOGO_PATH")
    if not raw:
        if strict:
            require("BRAND_LOGO_PATH")
        return None
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def brand_logo_keywords() -> list[str]:
    raw = value("BRAND_LOGO_KEYWORDS")
    return [k.strip() for k in raw.split(",") if k.strip()] or ["logo"]


def brand_logo(disabled: bool, override: str | None) -> tuple[frozenset[str], str | None]:
    """(bots that get the logo, logo file) for one publish, checked up front.

    A listed bot with no usable logo file is a stop, not a warning: the logo is
    the operator's mandatory first emoji, and a pack built without it cannot be
    fixed afterwards -- its first slot is already taken.
    """
    if disabled:
        return frozenset(), None
    bots = brand_logo_bots()
    if not bots:
        return bots, None
    path = Path(override) if override else brand_logo_path()
    if not path.is_file():
        raise OperatorConfigMissing(
            f"brand logo not found: {path}. Fix BRAND_LOGO_PATH in .env, or "
            "pass --no-brand-logo. Nothing was changed.")
    return bots, str(path)


def stop_unless(*keys: str) -> None:
    """`require()` for a command line: print the stop and exit 2, no traceback."""
    try:
        require(*keys)
    except OperatorConfigMissing as exc:
        raise SystemExit(f"ERROR: {exc}") from None
