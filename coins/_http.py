"""One pooled HTTP client for the coin fetchers.

Four scripts each hand-rolled a retry/backoff wrapper over ``urllib.urlopen``,
which opens a fresh TCP+TLS connection for every single call -- and
fetch_logos.py makes thousands of them against one image host. ``requests`` is
already a hard dependency (build_pack uses a Session), so one module-level
Session gives every caller connection reuse and one set of retry rules.

``get`` hands back the ``requests.Response`` rather than decoded JSON on
purpose: the status code IS the semantics for two callers. CoinPaprika answers
402 when the free search quota is gone (stop, resume later) and CMC answers
400/404 for a symbol it does not know (no match, not an error). Both must be
told apart from a transient failure, which decoded JSON cannot express.
"""

from __future__ import annotations

import os
import time

import requests

# ponytail: one process-wide Session, so a run reuses one connection per host.
# requests.Session is not thread-safe; these are single-threaded scripts. Give
# each thread its own Session if anything here ever goes concurrent.
SESSION = requests.Session()

# A provider that answers "come back in an hour" would otherwise turn one 429
# into a run that looks hung.
RETRY_AFTER_MAX = 60.0

# Seconds between paged market-data calls. The old hardcoded 12 s was pure
# insurance against a 429 -- 40 pages of it is ~8 minutes of sleeping per run,
# while get() already backs off on an actual 429 and honours Retry-After.
PAGE_DELAY_DEFAULT = 2.0
PAGE_DELAY_ENV = "COIN_PAGE_DELAY"


def page_delay(default: float = PAGE_DELAY_DEFAULT) -> float:
    """Seconds to pause between pages; ``COIN_PAGE_DELAY`` overrides it.

    ponytail: fixed spacing is the whole rate-limit strategy here. Raise the env
    var if a provider tightens its limits; swap in a token bucket only if one
    starts limiting on something other than request spacing.
    """
    raw = (os.environ.get(PAGE_DELAY_ENV) or "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        # A typo in .env must not crash a fetcher at import time.
        return default


def _retry_after(resp: requests.Response) -> float | None:
    """The wait the server asked for, when it stated one in plain seconds.

    The HTTP-date form is ignored: CoinGecko, CoinPaprika and CMC all send
    integer seconds, and guessing at clock skew is worse than the backoff ladder.
    """
    raw = (resp.headers.get("Retry-After") or "").strip()
    try:
        return min(float(raw), RETRY_AFTER_MAX)
    except ValueError:
        return None


def get(url: str, *, headers: dict | None = None, timeout: float = 40,
        retries: int = 4, backoff: float = 4.0, max_backoff: float = 20.0,
        stop_on: tuple[int, ...] = ()) -> requests.Response | None:
    """GET ``url`` with escalating backoff. None once every attempt has failed.

    A status listed in ``stop_on`` is returned immediately instead of retried:
    it is a definitive answer from the provider, and retrying it only burns the
    rate limit that produced it.
    """
    for attempt in range(1, retries + 1):
        resp = None
        try:
            resp = SESSION.get(url, headers=headers, timeout=timeout)
            if resp.ok or resp.status_code in stop_on:
                return resp
            reason = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 - any transport failure retries
            reason = str(exc)
        if attempt == retries:
            break                      # nothing left to wait for
        wait = min(backoff * attempt, max_backoff)
        if resp is not None:
            wait = _retry_after(resp) or wait
        print(f"  http retry {attempt}/{retries}: {reason} (wait {wait:.0f}s)",
              flush=True)
        time.sleep(wait)
    return None
