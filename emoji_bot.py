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
    of the spaces, newlines or plain text between the emoji.
    """
    ids: list[str] = []
    seen = set()
    for field in ("entities", "caption_entities"):
        for ent in message.get(field, []) or []:
            if ent.get("type") == "custom_emoji":
                cid = str(ent.get("custom_emoji_id", ""))
                if cid and cid not in seen:
                    seen.add(cid)
                    ids.append(cid)
    return ids


def _label(labels: dict[str, str] | None, cid: str) -> str:
    """Fallback standard-emoji char for an id, HTML-escaped (• if unknown)."""
    em = (labels or {}).get(cid, "")
    return html.escape(em) if em else "\u2022"


def _batch_ids(ids: list[str], labels: dict[str, str] | None) -> list[list[str]]:
    """Split ids so each rendered message stays under MSG_MAX characters."""
    batches: list[list[str]] = []
    cur: list[str] = []
    size = 0
    overhead = 240  # headers + blockquote/label markup per message
    for cid in ids:
        # cost in "Format 1" (emoji + <code></code> + id + nl) plus "Format 2".
        cost = len(_label(labels, cid)) + len(cid) * 2 + 28
        if cur and overhead + size + cost > MSG_MAX:
            batches.append(cur)
            cur, size = [], 0
        cur.append(cid)
        size += cost
    if cur:
        batches.append(cur)
    return batches


def _render_message(ids: list[str], labels: dict[str, str] | None,
                    grand_total: int, part: int, parts: int) -> str:
    """Render one HTML message with both copy formats as collapsed quotes.

    Format 1: ``emoji <code>id</code>`` per line — tap an id to copy just it.
    Format 2: one <code> block of all ids — tap once to copy them all.
    Both are expandable (collapsed) blockquotes.
    """
    head = f"Found <b>{grand_total}</b> premium emoji"
    if parts > 1:
        head += f" — part {part}/{parts}"
    fmt1 = "\n".join(f"{_label(labels, c)} <code>{c}</code>" for c in ids)
    fmt2 = "\n".join(ids)
    return (
        f"{head}:\n\n"
        "<b>1) Emoji + ID</b> — tap an ID to copy it:\n"
        f"<blockquote expandable>{fmt1}</blockquote>\n\n"
        "<b>2) IDs only</b> — tap the block to copy them all:\n"
        f"<blockquote expandable><code>{fmt2}</code></blockquote>"
    )


def build_messages(ids: list[str], labels: dict[str, str] | None = None) -> list[str]:
    """Build the HTML reply message(s) for a set of custom_emoji_ids.

    Returns one message when everything fits, or several when the id list is too
    large for a single Telegram message. No inline keyboard is used: copying is
    done by tapping the <code> ids (Telegram's built-in tap-to-copy).
    """
    if not ids:
        return ["No premium (custom) emoji found in that message. Send me one or "
                "more premium emoji in a row (spaces/newlines don't matter), or a "
                "post that contains premium emoji."]
    batches = _batch_ids(ids, labels)
    return [_render_message(b, labels, len(ids), i + 1, len(batches))
            for i, b in enumerate(batches)]


START_TEXT = (
    "<b>Emoji Mapper</b> — premium custom-emoji ID extractor\n\n"
    "• Send me one or more <b>premium emoji</b> in a row (spaces/newlines don't "
    "matter) → I reply in two collapsed quotes:\n"
    "   1) <i>emoji + ID</i> — tap an ID to copy just it;\n"
    "   2) <i>IDs only</i> — tap the block to copy them all at once.\n"
    "• Send or forward a <b>post with premium emoji</b> → same two-format reply.\n"
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
    messages = build_messages(ids, labels)
    for i, text in enumerate(messages):
        if header and i == 0:
            text = header + "\n\n" + text
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True}
        if reply_to and i == 0:
            data["reply_to_message_id"] = reply_to
        tg._call("sendMessage", data=data)


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

