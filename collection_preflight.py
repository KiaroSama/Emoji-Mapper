"""Ask Telegram to validate every queued file before anything is published.

Split out of `build_collection` because it is a separate job with a separate
verdict: it uploads nothing, publishes nothing and changes no state -- it only
answers "would Telegram take these files?" -- and it has its own exit-code
contract, which is the whole subject of F10.

``uploadStickerFile`` runs the same validator as ``addStickerToSet`` and touches
no set, so a file Telegram will refuse can be found in seconds instead of at
whatever minute of the publish it happens to reach. One `.tgs` with a subtract
mask was found 46 minutes into a run, after 99 uploads and two flood waits, and
it would have been the very first thing this reported.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from build_pack import EXIT_FAILED, EXIT_OK, EXIT_PARTIAL
from emojikit.catalog import Catalog
from emojikit.logsetup import redact
from telegram_api import BotApiError, Telegram

log = logging.getLogger("build_collection")

# One probe per file plus this pause; a 200-emoji queue takes about a minute.
PREFLIGHT_DELAY = 0.15


def preflight(tg: Telegram, cat: Catalog, user_id: int, plan: dict,
              formats: list[str], base: str, skipped: set[str],
              pending_keys: Callable[..., list[str]]) -> int:
    """Report what Telegram thinks of the queue. Publishes nothing, ever.

    A refusal is the file's own problem, not the pack's: it is reported and the
    run is NOT started, so nothing is half-published while you fix it.

    `pending_keys` is passed in rather than imported so this module does not
    have to import the publisher it was split out of.
    """
    # Four outcomes, counted apart. Collapsing them is the defect this function
    # was rewritten for: the old counter counted ATTEMPTS, and a transport
    # error was logged and then forgotten, so a run in which every single call
    # failed to reach Telegram still printed "Every queued file is acceptable
    # to Telegram" and exited 0 having validated nothing at all. An unknown is
    # neither an accepted file nor a permanent rejection, and it may not borrow
    # the exit code of either.
    accepted = 0
    refused: list[tuple[str, str, str]] = []   # Telegram gave a verdict: no
    missing: list[tuple[str, str]] = []        # never reached Telegram at all
    unknown: list[tuple[str, str, str]] = []   # we could not ask

    for fmt in formats:
        for key in pending_keys(cat, plan, fmt, base, skipped):
            it = cat.get(key)
            path = Path(it.file_path)
            if not path.is_file():
                missing.append((key, path.name))
                continue
            try:
                tg.check_uploadable(user_id, path, it.fmt)
                accepted += 1
            except BotApiError as exc:
                refused.append((key, path.name, redact(str(exc))))
            except RuntimeError as exc:
                # Transport, not a verdict. Saying "bad file" here would send
                # someone editing artwork over a dropped connection -- but
                # saying nothing sent them into a publish on an unchecked pack.
                why = redact(str(exc))
                log.warning("could not check %s: %s", key, why)
                unknown.append((key, path.name, why))
            time.sleep(PREFLIGHT_DELAY)

    return _report(accepted, refused, missing, unknown)


def _report(accepted: int, refused: list, missing: list, unknown: list) -> int:
    """Print the four counts honestly and return the matching exit code."""
    queued = accepted + len(refused) + len(missing) + len(unknown)
    print(f"\nPREFLIGHT: {queued} file(s) queued -- {accepted} accepted, "
          f"{len(refused)} refused, {len(missing)} missing, "
          f"{len(unknown)} not checked.", flush=True)
    for key, name, why in refused:
        print(f"  REFUSED {key}  ({name})\n          {why}", flush=True)
        log.error("preflight: Telegram refuses %s (%s): %s", key, name, why)
    for key, name in missing:
        print(f"  MISSING {key}  ({name}) is not on disk", flush=True)
        log.error("preflight: %s (%s) is missing on disk", key, name)
    for key, name, why in unknown:
        print(f"  UNCHECKED {key}  ({name})\n            {why}", flush=True)

    if refused or missing:
        print("\nNothing was published. Fix or deselect these, then publish.\n",
              flush=True)
        return EXIT_FAILED
    if unknown:
        # PARTIAL even when nothing at all was checked. Both neighbouring codes
        # are a lie here: 0 claims a validation that never took place, and
        # FAILED is what a Telegram refusal returns -- reporting a dropped
        # connection with it sends someone editing artwork that was never the
        # problem. "I could not finish asking" is its own answer, and it is
        # this one, whether it happened to one file or to all of them.
        print(f"\nNothing was published. Telegram could not be reached for "
              f"{len(unknown)} file(s), so this run proves nothing about them. "
              f"Re-run the preflight once the connection is back.\n", flush=True)
        return EXIT_PARTIAL
    if not accepted:
        print("Nothing was queued, so nothing was validated.\n", flush=True)
        return EXIT_OK
    print(f"All {accepted} queued file(s) are acceptable to Telegram.\n",
          flush=True)
    return EXIT_OK
