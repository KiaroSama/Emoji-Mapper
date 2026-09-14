"""Announcing finished packs -- through the Worker, or directly.

``WORKER_PUBLISH_URL`` unset means the old direct path, so an existing setup
keeps working untouched.
"""

from __future__ import annotations

import logging
import os

import requests

from emojikit.telegram_api import Telegram

log = logging.getLogger("build_pack")


def links_chat_id(owner_id: int) -> str | int:
    """Where finished-pack links are announced.

    ``PACK_LINKS_CHAT_ID`` may be a channel id (``-100...``) or an ``@name``.
    Falls back to the owner's private chat so existing setups keep working.
    The bot must be an administrator of that channel to post in it.
    """
    raw = os.environ.get("PACK_LINKS_CHAT_ID", "").strip()
    if not raw:
        return owner_id
    return raw if raw.startswith("@") else int(raw)


def worker_publish_url() -> str:
    """Where finished packs are announced from, when a Worker is deployed.

    Set ``WORKER_PUBLISH_URL`` (and ``WORKER_PUBLISH_SECRET``) to have the
    Cloudflare Worker post pack links to the channel instead of this process
    talking to Telegram directly. Unset means the old direct path, so an
    existing setup keeps working untouched.
    """
    return os.environ.get("WORKER_PUBLISH_URL", "").strip()


def announce_via_worker(packs: list[dict], *, note: str = "",
                        bot: str = "coin", style: str = "cards",
                        timeout: int = 30) -> None:
    """Ask the Worker to announce finished packs. Raises on any failure.

    ``packs`` is a list of ``{"name", "title", "count"}`` -- the Worker builds
    the message and owns the channel destination, so the link text lives in one
    place instead of being duplicated per publisher.

    NOT retried, for the same reason the direct send is not: the Worker's
    sendMessage is not idempotent and has no dedup key, so a timeout after
    Telegram accepted the post cannot be told from one before it, and retrying
    turns one outage into several identical announcements. The caller's
    ``state["sent"]`` guard is what makes a later re-run safe.
    """
    url = worker_publish_url()
    secret = os.environ.get("WORKER_PUBLISH_SECRET", "")
    if not url or not secret:
        raise RuntimeError("WORKER_PUBLISH_URL and WORKER_PUBLISH_SECRET must both be set")
    body = {"bot": bot, "packs": packs, "style": style}
    if note:
        body["note"] = note
    resp = requests.post(url, json=body, timeout=timeout,
                         headers={"Authorization": f"Bearer {secret}"})
    if resp.status_code != 200:
        # The body can carry the Worker's reason; the bearer never appears in it.
        raise RuntimeError(f"worker announce failed (HTTP {resp.status_code}): "
                           f"{resp.text[:200]}")


def announce_packs(tg: "Telegram", owner_id: int, packs: list[dict], *,
                   bot: str, note: str = "", style: str = "cards") -> str:
    """Post finished packs' add-links. Returns where they went, for the log.

    ONE function, because this project had three publishers -- the single-pack
    build, the collector and the coin rebuild -- each carrying its own copy of
    "format the link and sendMessage". When the Worker arrived only the
    collector learned about it, so a coin rebuild or a plain build went on
    talking to Telegram from this machine while the owner believed the bot was
    posting. A third copy is how that happens again.

    Routing: the Worker when BOTH ``WORKER_PUBLISH_URL`` and
    ``WORKER_PUBLISH_SECRET`` are set -- one alone is a half-configured setup,
    and silently falling back would look identical to a working Worker. The
    direct path is unchanged otherwise.

    Not retried on either route (see ``announce_via_worker``). Callers keep
    their ``state["sent"]`` guard; this function has no memory.
    """
    if worker_publish_url() and os.environ.get("WORKER_PUBLISH_SECRET", "").strip():
        announce_via_worker(packs, bot=bot, note=note, style=style)
        return "the worker"
    dest = links_chat_id(owner_id)
    # Previews off: these messages are mostly addemoji URLs, and one preview
    # card per link buries them. The Worker route does the same by default.
    #
    # The direct path renders exactly what the Worker renders. If it did not,
    # switching the Worker on would silently change how a real post looks.
    if style == "list":
        lines = [note, ""] if note else []
        lines += [f"{p.get('title') or p['name']}. "
                  f"https://t.me/addemoji/{p['name']}" for p in packs]
        tg.send_message(dest, "\n".join(lines), disable_preview=True)
        return str(dest)
    if note:
        tg.send_message(dest, note, disable_preview=True)
    for p in packs:
        tg.send_message(dest, f"✅ {p.get('title') or p['name']}\n"
                              f"https://t.me/addemoji/{p['name']}",
                        disable_preview=True)
    return str(dest)
