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

COPY_MAX = 256          # CopyTextButton.text hard limit
IDS_PER_COPYALL = 12    # ~19-digit ids + newline fit in COPY_MAX
PER_ROW = 4             # inline buttons per row


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def extract_custom_emoji_ids(message: dict) -> list[str]:
    """Ordered, de-duplicated custom_emoji_ids from a message's entities."""
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


def _chunk(seq: list[str], n: int) -> list[list[str]]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def build_reply(ids: list[str], labels: dict[str, str] | None = None) -> tuple[str, dict]:
    """Build (HTML text, inline_keyboard) for a set of custom_emoji_ids.

    * one id  -> a single button whose label IS the id and which copies it;
    * many    -> a numbered list in the text, per-id copy buttons, and
      "Copy ALL" button(s) (chunked to respect the 256-char copy limit).
    """
    labels = labels or {}
    if not ids:
        return ("No premium (custom) emoji found in that message. Send one, or a "
                "post that contains premium emoji.", {"inline_keyboard": []})

    if len(ids) == 1:
        cid = ids[0]
        em = labels.get(cid, "")
        text = (f"Premium emoji {em}\nID: <code>{html.escape(cid)}</code>\n\n"
                "Tap the button to copy the ID.").strip()
        kb = [[{"text": f"📋 {cid}", "copy_text": {"text": cid}}]]
        return text, {"inline_keyboard": kb}

    lines = [f"Found <b>{len(ids)}</b> premium emoji:"]
    for i, cid in enumerate(ids, 1):
        em = labels.get(cid, "")
        lines.append(f"{i}. {em} <code>{html.escape(cid)}</code>".strip())
    text = "\n".join(lines)

    kb: list[list[dict]] = []
    # Per-emoji copy buttons (each copies that single id).
    row: list[dict] = []
    for i, cid in enumerate(ids, 1):
        row.append({"text": f"#{i}", "copy_text": {"text": cid}})
        if len(row) == PER_ROW:
            kb.append(row); row = []
    if row:
        kb.append(row)
    # "Copy ALL" button(s); chunked so each stays within the 256-char limit.
    chunks = _chunk(ids, IDS_PER_COPYALL)
    if len(chunks) == 1:
        kb.append([{"text": f"📋 Copy all {len(ids)} IDs",
                    "copy_text": {"text": "\n".join(ids)}}])
    else:
        for k, ch in enumerate(chunks, 1):
            lo = (k - 1) * IDS_PER_COPYALL + 1
            hi = lo + len(ch) - 1
            kb.append([{"text": f"📋 Copy IDs {lo}-{hi}",
                        "copy_text": {"text": "\n".join(ch)}}])
    return text, {"inline_keyboard": kb}


START_TEXT = (
    "<b>Emoji Mapper</b> — premium custom-emoji ID extractor\n\n"
    "• Send me a <b>premium emoji</b> → I reply with its ID on a tap-to-copy button.\n"
    "• Send or forward a <b>post with premium emoji</b> → I list every ID; tap to copy.\n"
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
    text, kb = build_reply(ids, labels)
    if header:
        text = header + "\n\n" + text
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "reply_markup": json.dumps(kb)}
    if reply_to:
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

