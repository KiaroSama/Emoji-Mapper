"""Test package guard: no test may touch the real network or real credentials.

This exists because it already went wrong. During an automated repair pass a
test that was only meant to exercise a CLI *usage-error* path reached far enough
into the real code to call Telegram, with a real token present in the
environment, and replaced a sticker in a live production pack.

Importing this package (which ``unittest discover -s tests`` always does) makes
that impossible:

* every credential-shaped environment variable is scrubbed, so even a code path
  that ignores its injected fake cannot authenticate;
* outbound sockets are refused, so a missed fake surfaces as a loud, obvious
  error inside the test instead of a silent live mutation.

Tests that legitimately exercise transport behaviour inject a fake session or
patch ``requests``; none of them opens a real connection, so nothing here needs
an opt-out. If a future test genuinely needs one, add a narrow, named context
manager rather than removing the guard.
"""

from __future__ import annotations

import os
import socket

# --- 1. Remove real credentials -------------------------------------------- #
# Anything token/key-shaped goes, plus the owner id, so an accidental live call
# fails at authentication rather than succeeding against the real account.
_SECRET_HINTS = ("TOKEN", "API_KEY", "SECRET", "PASSWORD")
for _name in list(os.environ):
    if any(hint in _name.upper() for hint in _SECRET_HINTS):
        os.environ.pop(_name, None)
os.environ.pop("PACK_OWNER_USER_ID", None)
os.environ.pop("PACK_LINKS_CHAT_ID", None)
os.environ.pop("BOT_ALLOWED_USER_IDS", None)

# Point the Bot API at an address that cannot be a real server, so a request
# built before the socket guard bites still cannot reach Telegram.
os.environ["TELEGRAM_API_BASE"] = "http://127.0.0.1:9"   # discard port

# Several modules call load_env() at import time, which would read .env and put
# the real credentials straight back. Honoured by build_pack.load_env().
os.environ["EMOJI_MAPPER_NO_DOTENV"] = "1"


# --- 2. Refuse outbound connections ----------------------------------------- #
class NetworkAccessDenied(RuntimeError):
    """A test tried to open a real network connection."""


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _host_of(address) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


def _guarded_connect(self, address, *a, **kw):
    # Loopback stays open: the panel tests start a real local HTTP server.
    if _host_of(address) in _LOOPBACK:
        return _real_connect(self, address, *a, **kw)
    raise NetworkAccessDenied(
        f"a test attempted to connect to {_host_of(address)!r}. Tests must use "
        f"fakes; a real connection here is how a live pack once got mutated."
    )


def _guarded_connect_ex(self, address, *a, **kw):
    if _host_of(address) in _LOOPBACK:
        return _real_connect_ex(self, address, *a, **kw)
    raise NetworkAccessDenied(
        f"a test attempted to connect to {_host_of(address)!r}.")


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
