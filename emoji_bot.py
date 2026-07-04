"""Interactive Emoji Mapper bot (general bot, long-polling).

Capabilities:
  1. Send the bot a premium (custom) emoji -> it replies with the emoji's id on
     a tap-to-copy inline button.
  2. Send/forward a post that mixes text + premium emoji -> it lists every
     custom_emoji_id; tapping a button copies all of them at once.
  3. Add the bot to a group/channel -> for every NEW post it sees there, it DMs
     the owner the premium emoji ids (tap to copy). NOTE: the Bot API cannot read
     channel history, so only posts received after the bot joined are processed.

"Tap to copy" uses Telegram's CopyTextButton (Bot API 9.0): an inline button
with ``copy_text`` copies its text to the clipboard on click.

Run:  python emoji_bot.py        (uses GENERAL_BOT_TOKEN from .env)
"""

from __future__ import annotations

import html
import json
import logging
import os
import time

from build_pack import Telegram, load_env
from emojikit.logsetup import redact, setup_logging

log = logging.getLogger("emoji_bot")

MSG_MAX = 3500          # keep well under Telegram's 4096-char message limit


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def extract_custom_emoji_ids(message: dict) -> list[str]:
    """Ordered, de-duplicated custom_emoji_ids from a message's entities.

    Every custom_emoji entity is collected in the order it appears, regardless
    of the spaces, newlines or plain text between the emoji, and regardless of
    how many times the same emoji repeats (each real id is listed once).

    Scans, in order:
    - ``entities`` / ``caption_entities`` -- the message's own text/caption.
    - ``quote.entities`` -- per the Bot API, when a reply *quotes part of* the
      original message, only bold/italic/underline/strikethrough/spoiler and
      **custom_emoji** entities are preserved in that quoted excerpt (``quote``
      is a ``TextQuote``). Without this, premium emoji inside a manually quoted
      reply (visible as the highlighted "> ..." block above the reply) were
      silently dropped.
    - ``external_reply.quote.entities`` -- the same quoting, but for a reply to
      a message from another chat (e.g. quoting a forwarded/external post).
    """
    ids: list[str] = []
    seen = set()

    def _collect(ents) -> None:
        for ent in ents or []:
            if ent.get("type") == "custom_emoji":
                cid = str(ent.get("custom_emoji_id", ""))
                if cid and cid not in seen:
                    seen.add(cid)
                    ids.append(cid)

    _collect(message.get("entities"))
    _collect(message.get("caption_entities"))
    _collect((message.get("quote") or {}).get("entities"))
    _collect(((message.get("external_reply") or {}).get("quote") or {}).get("entities"))
    return ids


DEFAULT_FALLBACK = "\u2b50"   # ⭐ shown if a custom emoji has no associated char
PER_ID_COST = 110             # worst-case chars per id (rich <tg-emoji> + code)
COPY_MAX = 256                # CopyTextButton.text hard limit
IDS_PER_COPY_BTN = 12         # ~19-digit ids + newline fit under COPY_MAX


def _fallback_char(labels: dict[str, str] | None, cid: str) -> str:
    em = (labels or {}).get(cid, "")
    return em or DEFAULT_FALLBACK


def _emoji_span(labels: dict[str, str] | None, cid: str, rich: bool) -> str:
    """The emoji cell for Format 1.

    rich=True renders the ACTUAL premium emoji via a custom_emoji entity
    (``<tg-emoji emoji-id=...>fallback</tg-emoji>``); rich=False shows just the
    fallback standard-emoji char (used if a rich send is rejected).
    """
    fb = html.escape(_fallback_char(labels, cid))
    if rich:
        return f'<tg-emoji emoji-id="{cid}">{fb}</tg-emoji>'
    return fb


def _batch_ids(ids: list[str]) -> list[list[str]]:
    """Split ids into per-MESSAGE batches (mode-agnostic; keeps messages few).

    Batches by the message-length limit, not the copy_text button limit, so as
    many ids as possible land in ONE message (e.g. 50 ids -> 1 message, not 5).
    Telegram's copy_text button is separately capped at 256 chars (~12 ids), so
    a message with more ids than that gets several "Copy a-b" buttons -- see
    _copy_keyboard -- covering the whole message between them.
    """
    per_msg = max(1, (MSG_MAX - 260) // PER_ID_COST)
    return [ids[i:i + per_msg] for i in range(0, len(ids), per_msg)]


def _render_message(ids: list[str], labels: dict[str, str] | None,
                    grand_total: int, part: int, parts: int, rich: bool) -> str:
    """Render one HTML message with both copy formats as collapsed quotes.

    Format 1: ``<premium emoji> <code>id</code>`` per line — tap an id to copy it.
    Format 2: one <code> block of all ids — tap once to copy them all.
    Both are expandable (collapsed) blockquotes.
    """
    head = f"Found <b>{grand_total}</b> premium emoji"
    if parts > 1:
        head += f" — part {part}/{parts}"
    fmt1 = "\n".join(f"{_emoji_span(labels, c, rich)} <code>{c}</code>" for c in ids)
    # A single COLLAPSED (expandable) quote of emoji + ID. Tap an ID to copy just
    # it (mobile); use the "Copy ..." button(s) below to copy this message's IDs
    # in one or a few taps on any platform (see _copy_keyboard).
    return (
        f"{head} — tap to expand; tap an ID to copy it, or use the "
        "“Copy” button(s) below:\n"
        f"<blockquote expandable>{fmt1}</blockquote>"
    )


def _copy_keyboard(ids: list[str]) -> dict:
    """Inline keyboard whose button(s) copy every id in one tap (all platforms).

    copy_text is capped at 256 chars, so long lists are split into a few
    "Copy a-b" buttons; short lists get a single "Copy all N IDs" button.
    """
    chunks = [ids[i:i + IDS_PER_COPY_BTN] for i in range(0, len(ids), IDS_PER_COPY_BTN)]
    kb: list[list[dict]] = []
    if len(chunks) <= 1:
        kb.append([{"text": f"📋 Copy all {len(ids)} IDs",
                    "copy_text": {"text": "\n".join(ids)}}])
    else:
        for k, ch in enumerate(chunks, 1):
            lo = (k - 1) * IDS_PER_COPY_BTN + 1
            hi = lo + len(ch) - 1
            kb.append([{"text": f"📋 Copy {lo}-{hi}",
                        "copy_text": {"text": "\n".join(ch)}}])
    return {"inline_keyboard": kb}


def build_payloads(ids: list[str], labels: dict[str, str] | None = None,
                   rich: bool = True) -> list[tuple[str, dict]]:
    """Build (HTML text, inline_keyboard) message payload(s) for the ids.

    Both formats are collapsed (expandable) quotes; a "Copy all" copy_text button
    provides reliable one-click copy-all on every platform. Large id lists are
    split into several messages, each under Telegram's 4096-char limit. When
    ``rich`` is true, Format 1 renders the real premium emoji via ``<tg-emoji>``.
    Batching is mode-independent so rich and plain renders align 1:1.
    """
    if not ids:
        return [("No premium (custom) emoji found in that message. Send me one or "
                 "more premium emoji in a row (spaces/newlines don't matter), or a "
                 "post that contains premium emoji.", {"inline_keyboard": []})]
    batches = _batch_ids(ids)
    out: list[tuple[str, dict]] = []
    for i, b in enumerate(batches):
        text = _render_message(b, labels, len(ids), i + 1, len(batches), rich)
        out.append((text, _copy_keyboard(b)))
    return out


START_TEXT = (
    "<b>Emoji Mapper</b> — premium custom-emoji ID extractor\n\n"
    "• Send me one or more <b>premium emoji</b> in a row (spaces/newlines don't "
    "matter) → I reply with a collapsed quote of <i>emoji + ID</i> (tap an ID to "
    "copy just it) and a <b>Copy all</b> button to copy every ID at once.\n"
    "• Send or forward a <b>post with premium emoji</b> → same reply.\n"
    "• <b>Add me to a channel/group</b> (as admin) → I DM you the premium emoji IDs "
    "from new posts there.\n\n"
    "Note: I can only read posts I receive after joining (Telegram doesn't let bots "
    "read past channel history)."
)


# --------------------------------------------------------------------------- #
# Telegram glue
# --------------------------------------------------------------------------- #
def enrich_labels(tg: Telegram, ids: list[str]) -> dict[str, str]:
    """Map id -> its fallback emoji char (best-effort, via getCustomEmojiStickers)."""
    out: dict[str, str] = {}
    try:
        for i in range(0, len(ids), 200):
            res = tg._call("getCustomEmojiStickers",
                           data={"custom_emoji_ids": json.dumps(ids[i:i + 200])})
            for st in res or []:
                cid = str(st.get("custom_emoji_id"))
                if cid:
                    out[cid] = st.get("emoji", "")
    except Exception as exc:  # noqa: BLE001 - labels are optional
        log.debug("enrich failed: %s", exc)
    return out


def send_reply(tg: Telegram, chat_id: int, ids: list[str], *, reply_to: int | None = None,
               header: str | None = None) -> None:
    labels = enrich_labels(tg, ids) if ids else {}
    rich = build_payloads(ids, labels, rich=True)
    plain: list[tuple[str, dict]] | None = None
    for i, (text, kb) in enumerate(rich):
        def _data(body: str) -> dict:
            body = (header + "\n\n" + body) if (header and i == 0) else body
            d = {"chat_id": chat_id, "text": body, "parse_mode": "HTML",
                 "disable_web_page_preview": True, "reply_markup": json.dumps(kb)}
            if reply_to and i == 0:
                d["reply_to_message_id"] = reply_to
            return d
        try:
            tg._call("sendMessage", data=_data(text))
        except Exception as exc:  # noqa: BLE001 - a bad custom_emoji must not drop the reply
            # Retry without <tg-emoji> (some ids may not be renderable by the bot).
            log.warning("rich reply failed (%s); retrying with plain fallback chars",
                        redact(str(exc)))
            if plain is None:
                plain = build_payloads(ids, labels, rich=False)
            tg._call("sendMessage", data=_data(plain[i][0]))


def handle_update(tg: Telegram, owner_id: int, upd: dict) -> None:
    # Menu / commands and private messages.
    msg = upd.get("message")
    if msg:
        chat = msg.get("chat", {})
        chat_id = chat["id"]
        text = msg.get("text", "") or ""
        if text.startswith("/start") or text.startswith("/help") or text.startswith("/menu"):
            tg._call("sendMessage", data={"chat_id": chat_id, "text": START_TEXT,
                                          "parse_mode": "HTML"})
            return
        ids = extract_custom_emoji_ids(msg)
        if chat.get("type") == "private":
            send_reply(tg, chat_id, ids, reply_to=msg.get("message_id"))
        elif ids:
            # Group message with premium emoji -> DM the owner.
            send_reply(tg, owner_id, ids,
                       header=f"From group <b>{html.escape(str(chat.get('title','')))}</b>:")
        return

    post = upd.get("channel_post")
    if post:
        ids = extract_custom_emoji_ids(post)
        if ids:
            title = html.escape(str(post.get("chat", {}).get("title", "")))
            send_reply(tg, owner_id, ids, header=f"From channel <b>{title}</b>:")
        return


def main() -> int:
    load_env()
    setup_logging("emoji_bot")
    token = os.environ.get("GENERAL_BOT_TOKEN", "")
    if not token:
        log.error("GENERAL_BOT_TOKEN not set (.env).")
        return 2
    owner_id = int(os.environ.get("PACK_OWNER_USER_ID", "0"))
    tg = Telegram(token)
    me = tg.get_me()
    log.info("Emoji Mapper bot @%s started (owner=%s)", me.get("username"), owner_id)
    try:
        tg._call("setMyCommands", data={"commands": json.dumps([
            {"command": "start", "description": "How to use the bot"},
            {"command": "help", "description": "Show help / menu"},
        ])})
    except Exception as exc:  # noqa: BLE001 - non-fatal
        log.debug("setMyCommands failed: %s", exc)

    offset = 0
    allowed = ["message", "channel_post", "edited_channel_post", "my_chat_member"]
    while True:
        try:
            updates = tg._call("getUpdates", data={
                "offset": offset, "timeout": 50,
                "allowed_updates": json.dumps(allowed),
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("getUpdates failed: %s", redact(str(exc)))
            time.sleep(3)
            continue
        for upd in updates or []:
            offset = upd["update_id"] + 1
            try:
                handle_update(tg, owner_id, upd)
            except Exception as exc:  # noqa: BLE001 - one bad update must not stop the bot
                log.warning("handle_update error: %s", redact(str(exc)))


if __name__ == "__main__":
    raise SystemExit(main())

