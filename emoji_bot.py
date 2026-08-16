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
import re
import time
from pathlib import Path

from build_pack import (BotApiError, Telegram, load_env, safe_int_env,
                        write_json_atomic)
from emojikit.logsetup import record_exit_code, redact, setup_logging

# A custom_emoji_id as Telegram issues it: decimal digits, nothing else. The
# property that matters here is "carries no character an HTML parser reacts to",
# not a length floor -- fetch_emoji_ids.py's stricter \d{5,25} exists to avoid
# false positives when scraping ids out of prose, which is a different problem.
_CUSTOM_EMOJI_ID = re.compile(r"\d{1,25}")

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
                # Shape-check at the boundary. This value is inbound message
                # data, and it is interpolated into HTML that Telegram parses
                # (an emoji-id attribute and a <code> block) -- the one
                # externally-supplied string in this module that was not
                # escaped, while group and channel titles beside it are. A
                # custom_emoji_id is a decimal id; anything else is not one, so
                # drop it rather than render it. Same shape fetch_emoji_ids.py
                # already enforces when it parses ids back out of text.
                if not _CUSTOM_EMOJI_ID.fullmatch(cid):
                    if cid:
                        log.debug("ignoring a custom_emoji_id of unexpected shape")
                    continue
                if cid not in seen:
                    seen.add(cid)
                    ids.append(cid)

    _collect(message.get("entities"))
    _collect(message.get("caption_entities"))
    _collect((message.get("quote") or {}).get("entities"))
    _collect(((message.get("external_reply") or {}).get("quote") or {}).get("entities"))
    return ids


DEFAULT_FALLBACK = "\u2b50"   # ⭐ shown if a custom emoji has no associated char
PER_ID_COST = 110             # worst-case chars per id (rich <tg-emoji> + code)
COPY_MAX = 256                # CopyTextButton.text hard limit (Bot API)

# getUpdates cursor, persisted so a restart does not replay handled updates.
OFFSET_FILE = Path(__file__).resolve().parent / "state_emoji_bot.json"


def _load_offset() -> int:
    try:
        return int(json.loads(OFFSET_FILE.read_text(encoding="utf-8"))["offset"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _save_offset(offset: int) -> None:
    try:
        write_json_atomic(OFFSET_FILE, {"offset": offset})
    except OSError as exc:
        # Losing the cursor only costs a replay, so this must never stop the bot.
        log.warning("could not persist update offset: %s", exc)


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
        # Escaped as well as shape-checked at the boundary: belt and braces on
        # the one sink whose input does not originate here.
        return f'<tg-emoji emoji-id="{html.escape(cid)}">{fb}</tg-emoji>'
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
    fmt1 = "\n".join(f"{_emoji_span(labels, c, rich)} <code>{html.escape(c)}</code>"
                     for c in ids)
    # A single COLLAPSED (expandable) quote of emoji + ID. Tap an ID to copy just
    # it (mobile); use the "Copy ..." button(s) below to copy this message's IDs
    # in one or a few taps on any platform (see _copy_keyboard).
    return (
        f"{head} — tap to expand; tap an ID to copy it, or use the "
        "“Copy” button(s) below:\n"
        f"<blockquote expandable>{fmt1}</blockquote>"
    )


def _copy_text(ids: list[str]) -> str:
    """The text one copy button places on the clipboard."""
    return "\n".join(ids) + "\n"


def _chunk_for_copy(ids: list[str], limit: int = COPY_MAX) -> list[list[str]]:
    """Split ids into chunks whose copied text fits Telegram's 256-char cap.

    Packing a FIXED number of ids per button silently overflows: the id parser
    accepts up to 25 digits, and 12 of those plus newlines is 312 characters,
    so Telegram rejects the button. Measure the encoded text instead.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    for cid in ids:
        cost = len(cid) + 1                     # id + its newline
        if current and len(_copy_text(current)) + cost > limit:
            chunks.append(current)
            current = []
        current.append(cid)
    if current:
        chunks.append(current)
    return chunks


def _copy_keyboard(ids: list[str]) -> dict:
    """Inline keyboard whose button(s) copy every id in one tap (all platforms).

    copy_text is capped at 256 chars, so long lists are split into a few
    "Copy a-b" buttons; short lists get a single "Copy all N IDs" button. Each
    button's copied text ends with a trailing newline, so pasting the ids is
    immediately followed by a blank line (handy when pasting several button
    copies in sequence, or pasting an id above other text).
    """
    chunks = _chunk_for_copy(ids)
    kb: list[list[dict]] = []
    if len(chunks) <= 1:
        kb.append([{"text": f"📋 Copy all {len(ids)} IDs",
                    "copy_text": {"text": _copy_text(ids)}}])
    else:
        lo = 1
        for ch in chunks:
            hi = lo + len(ch) - 1
            kb.append([{"text": f"📋 Copy {lo}-{hi}",
                        "copy_text": {"text": _copy_text(ch)}}])
            lo = hi + 1
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
        # i and kb are bound as defaults, not captured: a closure defined in a
        # loop reads the loop variable at CALL time. Both calls happen inside
        # this iteration so today it makes no difference -- binding costs one
        # line and means it still makes none if _data ever outlives the turn.
        def _data(body: str, *, i: int = i, kb=kb) -> dict:
            body = (header + "\n\n" + body) if (header and i == 0) else body
            d = {"chat_id": chat_id, "text": body, "parse_mode": "HTML",
                 "disable_web_page_preview": True, "reply_markup": json.dumps(kb)}
            if reply_to and i == 0:
                d["reply_to_message_id"] = reply_to
            return d
        try:
            # retries=1: sendMessage is not idempotent and has no dedup key, so
            # a timeout AFTER Telegram accepted the message is indistinguishable
            # from one before it. Retrying turns one outage into several
            # identical replies.
            tg._call("sendMessage", retries=1, data=_data(text))
        except BotApiError as exc:
            # Telegram answered and REJECTED this message -- typically an id the
            # bot cannot render as <tg-emoji>. It definitely did not arrive, so
            # sending the plain variant cannot duplicate anything.
            log.warning("rich reply rejected (%s); sending plain fallback chars",
                        redact(str(exc)))
            if plain is None:
                plain = build_payloads(ids, labels, rich=False)
            tg._call("sendMessage", retries=1, data=_data(plain[i][0]))
        except Exception as exc:  # noqa: BLE001
            # No answer from Telegram: the rich message MAY have arrived. Sending
            # the fallback here is what posted the same reply twice. Report and
            # stop instead of guessing.
            log.error("reply to chat %s is unresolved (%s); not sending a "
                      "fallback, which could duplicate it", chat_id,
                      redact(str(exc)))
            return


DENIED_TEXT = ("This is a private bot and you are not on its access list.\n"
               "If you should have access, ask the owner to add your numeric "
               "Telegram user id.")


def allowed_user_ids() -> set[int]:
    """Numeric ids allowed to use the bot.

    ``BOT_ALLOWED_USER_IDS`` is a comma-separated list; when it is unset the
    pack owner is the only allowed user. An empty allowlist is treated as
    "nobody" rather than "everybody" -- a misconfiguration must fail closed.
    """
    raw = os.environ.get("BOT_ALLOWED_USER_IDS", "").strip()
    if not raw:
        owner = safe_int_env("PACK_OWNER_USER_ID", 0, minimum=0)
        return {owner} if owner > 0 else set()
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning("ignoring non-numeric entry in BOT_ALLOWED_USER_IDS: %r", part)
    return out


def handle_update(tg: Telegram, owner_id: int, upd: dict,
                  allowed: set[int] | None = None) -> None:
    allowed = allowed_user_ids() if allowed is None else allowed
    # Menu / commands and private messages.
    msg = upd.get("message")
    if msg:
        chat = msg.get("chat", {})
        chat_id = chat["id"]
        sender = (msg.get("from") or {}).get("id")
        if sender not in allowed:
            # Answer once in private so a real person is not left guessing;
            # stay silent in groups so the bot cannot be used to spam them.
            log.info("ignoring message from unauthorized user %s in %s chat",
                     sender, chat.get("type"))
            if chat.get("type") == "private":
                try:
                    tg._call("sendMessage",
                             data={"chat_id": chat_id, "text": DENIED_TEXT})
                except Exception as exc:  # noqa: BLE001 - denial is best-effort
                    log.debug("could not send denial: %s", redact(str(exc)))
            return
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
    owner_id = safe_int_env("PACK_OWNER_USER_ID", 0, minimum=0)
    if owner_id <= 0:
        # Zero is not a usable chat id; without this the bot polls happily and
        # only fails later, per message, when it tries to reply.
        log.error("PACK_OWNER_USER_ID is not set to a valid numeric id (.env).")
        return 2
    allowed_users = allowed_user_ids()
    if not allowed_users:
        log.error("no authorized users: set BOT_ALLOWED_USER_IDS or "
                  "PACK_OWNER_USER_ID. Refusing to run an open bot.")
        return 2
    log.info("access list: %d authorized user id(s)", len(allowed_users))
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

    # The offset survives a restart: keeping it only in memory made every
    # restart re-fetch old updates and reply to them a second time.
    offset = _load_offset()
    if offset:
        log.info("resuming from update offset %d", offset)
    # Only ask for update types this bot actually dispatches.
    # Deliberately NOT named `allowed`: this is the Telegram update-type filter,
    # a completely different thing from the numeric user allowlist above. They
    # were once both called `allowed`, and the second assignment silently
    # replaced the user ids -- so `sender not in allowed` compared an int
    # against update-type strings and rejected every user, including the owner.
    allowed_update_types = ["message", "channel_post", "my_chat_member"]
    while True:
        try:
            updates = tg._call("getUpdates", data={
                "offset": offset, "timeout": 50,
                "allowed_updates": json.dumps(allowed_update_types),
            })
        except Exception as exc:  # noqa: BLE001
            text = redact(str(exc))
            if "conflict" in text.lower():
                # Another poller, or a webhook, owns this bot. Retrying every
                # three seconds forever just hides a configuration error.
                log.error("getUpdates conflict -- another instance or a webhook "
                          "is active for this bot: %s", text)
                return 4
            log.warning("getUpdates failed: %s", text)
            time.sleep(3)
            continue
        for upd in updates or []:
            try:
                handle_update(tg, owner_id, upd, allowed_users)
            except Exception as exc:  # noqa: BLE001 - one bad update must not stop the bot
                # Explicitly dead-lettered: acknowledged so a poison update
                # cannot wedge the queue, but recorded at error level rather
                # than dropped silently.
                log.error("dead-lettering update %s after handler error: %s",
                          upd.get("update_id"), redact(str(exc)))
            # Advance only AFTER the update has been handled or dead-lettered.
            offset = upd["update_id"] + 1
            _save_offset(offset)


if __name__ == "__main__":
    raise SystemExit(record_exit_code(main()))

