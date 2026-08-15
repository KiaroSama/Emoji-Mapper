"""Create Telegram premium custom-emoji pack(s) from a folder of PNG images.

This is the generic Emoji Mapper engine: it uploads every 100x100 PNG found in a
source directory into one or more Telegram custom-emoji sets. It is NOT tied to
cryptocurrency coins -- point ``--source-dir`` at any folder of prepared PNGs
(see make_emoji_pngs.py) to build a pack of arbitrary emojis.

A bot can create a custom-emoji set OWNED by a user, so you need:
  - a bot token (env or .env)                -> selected with --token-env
  - PACK_OWNER_USER_ID  (env, or --user-id)  -> your numeric Telegram user id
  - You must have pressed Start on that bot at least once.

Two bots are configured by default:
  - TELEGRAM_BOT_TOKEN  -> the original crypto-coin bot (default)
  - GENERAL_BOT_TOKEN   -> @GodVerifyEmojiMapperbot, for general (non-coin) packs

Telegram limits each custom-emoji set to 200 emojis, so the images are split
into multiple sets named ``<base><n>_by_<botusername>``. Each emoji is given an
associated standard emoji (--emoji) and optional searchable keywords (from a
keywords.csv mapping ``ticker -> keywords``; falls back to the file name).

Usage (crypto coins, original bot):
  python build_pack.py --base gvcryptoemoji --title "@GodVerify Crypto Emoji" \
      [--user-id 123] [--emoji ߞ] [--limit N] [--start N] [--dry-run]

Usage (general pack, new bot):
  python build_pack.py --base mystickers --title "My Emojis" \
      --source-dir build/myset --token-env GENERAL_BOT_TOKEN --emoji ߘ

Run with --dry-run first to validate inputs without calling Telegram.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent


def write_json_atomic(path: Path, data) -> None:
    """Write JSON so an interrupted run can never leave a truncated file.

    Upload progress lives in these files: a half-written state file is read back
    as corrupt (or, worse, silently replaced by an empty default) and the whole
    source set gets uploaded again. Write to a sibling temp file, flush it to
    disk, then rename -- rename is atomic on both NTFS and POSIX.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
# Default source/keyword locations (crypto-coin workflow). Override per run with
# --source-dir / --keywords so the same engine builds any kind of emoji pack.
EMOJI_DIR = ROOT / "logos" / "emoji"
KEYWORDS_CSV = ROOT / "keywords.csv"
# Legacy default state file (kept for backwards compatibility); the actual state
# file used by a run defaults to state_<base>.json so different packs never
# clobber each other.
STATE_FILE = ROOT / "pack_state.json"

# Custom emoji must be 100x100 PNG; build_pack uploads the prepared PNGs.
_MIME = {".png": "image/png", ".webp": "image/webp"}
DEFAULT_EMOJI = "\U0001FA99"  # ߞ coin
# Telegram's hard cap for a custom-emoji set. See
# https://core.telegram.org/bots/api#addstickertoset -- "Emoji sticker sets can
# have up to 200 stickers." Exceeding it only produces STICKERS_TOO_MUCH at
# upload time, after the wrong set count has already been planned.
MAX_PER_SET = 200
PER_SET = MAX_PER_SET
# Upper bound on how long a create may wait for a deleted set name to be
# released, across all of its retries.
NAME_LOCK_TIMEOUT = 300.0


def api_base() -> str:
    """Resolve the API base per call, not at import.

    ``load_env()`` runs inside main(), which is *after* this module is imported,
    so reading the env var at import time silently ignores a TELEGRAM_API_BASE
    that is configured only in .env.
    """
    return os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")


def load_env() -> None:
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_keywords(path: Path = KEYWORDS_CSV) -> dict[str, str]:
    """ticker -> 'ticker, name' keyword string (optional; missing file -> {})."""
    out: dict[str, str] = {}
    if path and Path(path).is_file():
        with open(path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                out[row["ticker"].lower()] = row.get("keywords") or row["ticker"]
    return out


class AmbiguousUploadError(RuntimeError):
    """A state-changing call failed at the network level and the live state
    could not be verified: the change may or may not have been applied.
    Callers must reconcile against live Telegram state instead of re-sending
    (re-sending a non-idempotent call such as addStickerToSet would DUPLICATE
    its effect)."""


class Telegram:
    def __init__(self, token: str) -> None:
        self.token = token
        self.s = requests.Session()

    def _safe(self, exc: BaseException) -> str:
        """Exception text with the bot token stripped.

        Every request URL embeds the token, and requests puts the URL in its
        exception message -- printing ``str(exc)`` raw publishes the token to
        the console and, via the coin runner's output redirect, to a log file.
        """
        return str(exc).replace(self.token, "[REDACTED]")

    def _call(self, method: str, *, data=None, files=None, retries: int = 5,
              applied_check=None):
        """POST a Bot API method with retries.

        ``applied_check`` makes retries safe for NON-idempotent methods
        (addStickerToSet / createNewStickerSet). After a network-level failure
        (the request may have been processed even though the response never
        arrived) it probes the live state and returns:
          True  -> the change IS live: report success, never re-send;
          False -> definitely not applied: safe to re-send;
          None  -> live state unknown: raise AmbiguousUploadError so the
                   caller reconciles instead of guessing.
        Without a check, such methods keep the historical blind-retry behavior.
        """
        url = f"{api_base()}/bot{self.token}/{method}"
        # A just-deleted set name stays locked for ~2 min, so a CREATE may
        # legitimately need to wait it out. For every other method
        # STICKERSET_INVALID means "no such set" -- a permanent answer that must
        # be returned at once, not slept on for six minutes.
        name_lock_retry = method == "createNewStickerSet"
        deadline = time.monotonic() + NAME_LOCK_TIMEOUT
        for attempt in range(1, retries + 1):
            try:
                r = self.s.post(url, data=data, files=files, timeout=60)
                payload = r.json()
                if payload.get("ok"):
                    return payload["result"]
                desc = str(payload.get("description", ""))
                # Honor flood waits.
                if "retry after" in desc.lower():
                    wait = int(payload.get("parameters", {}).get("retry_after", 5))
                    print(f"  flood wait {wait}s ({method})", flush=True)
                    time.sleep(wait + 1)
                    continue
                if "stickerset_invalid" in desc.lower():
                    remaining = deadline - time.monotonic()
                    if not name_lock_retry or attempt >= retries or remaining <= 0:
                        raise RuntimeError(f"{method} failed: {desc}")
                    wait = min(30 * attempt, 90, remaining)
                    print(f"  stickerset_invalid; name not released yet, "
                          f"wait {wait:.0f}s ({method})", flush=True)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"{method} failed: {desc}")
            except requests.RequestException as exc:
                if applied_check is not None:
                    time.sleep(2)  # let Telegram settle before probing
                    applied = applied_check()
                    if applied is True:
                        print(f"  {method}: network error but the change is "
                              f"verified live; not re-sending", flush=True)
                        return {"verified_applied": True}
                    if applied is None:
                        raise AmbiguousUploadError(
                            f"{method}: network failure and live state "
                            f"unknown ({self._safe(exc)})") from exc
                    # applied is False: definitely not applied, safe to re-send.
                if attempt >= retries:
                    break          # never sleep after the final attempt
                wait = min(3 * attempt, 20)
                print(f"  net retry {attempt}/{retries} ({method}): "
                      f"{self._safe(exc)} (wait {wait}s)", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"{method} failed after {retries} attempts")

    def probe_sticker_set(self, name: str) -> tuple[bool, dict | None]:
        """Single, non-retrying live probe of a sticker set.

        Returns ``(known, set)``: ``(True, dict)`` it exists, ``(True, None)``
        it definitely does not exist, ``(False, None)`` live state unknown
        (network/API failure). Unlike getStickerSet via ``_call`` this never
        raises and never sleeps (``_call`` treats STICKERSET_INVALID as a
        name-release lock and waits minutes, which a probe must not do).
        """
        try:
            r = self.s.post(f"{api_base()}/bot{self.token}/getStickerSet",
                            data={"name": name}, timeout=30)
            payload = r.json()
        except (requests.RequestException, ValueError):
            return False, None
        if payload.get("ok"):
            return True, payload["result"]
        if "stickerset_invalid" in str(payload.get("description", "")).lower():
            return True, None
        return False, None

    def _added_check(self, name: str, expected_before: int | None):
        """applied_check for addStickerToSet: did the set grow by exactly one?"""
        if expected_before is None:
            return None

        def check():
            known, sset = self.probe_sticker_set(name)
            if not known or sset is None:
                return None  # unknown / set vanished: reconcile, don't guess
            n = len(sset.get("stickers", []))
            if n == expected_before + 1:
                return True
            if n == expected_before:
                return False
            return None  # count drifted: reconcile, don't guess

        return check

    def _created_check(self, name: str):
        """applied_check for createNewStickerSet: does the set now exist?"""
        def check():
            known, sset = self.probe_sticker_set(name)
            if not known:
                return None
            return sset is not None

        return check

    def get_me(self) -> dict:
        return self._call("getMe")

    def send_message(self, chat_id: int, text: str) -> None:
        self._call("sendMessage", data={
            "chat_id": chat_id, "text": text, "disable_web_page_preview": False,
        })

    # NOTE: all upload methods pass the file CONTENT (bytes), not an open
    # handle: a retried request must re-send the full body, and a file object
    # is already exhausted after the first attempt (a flood-wait or network
    # retry would silently send an empty file and fail the sticker).

    def upload_sticker(self, user_id: int, path: Path) -> str:
        mime = _MIME.get(path.suffix.lower(), "application/octet-stream")
        res = self._call(
            "uploadStickerFile",
            data={"user_id": user_id, "sticker_format": "static"},
            files={"sticker": (path.name, path.read_bytes(), mime)},
        )
        return res["file_id"]

    def create_set(self, user_id: int, name: str, title: str, png: Path,
                   emoji: str, keywords: str) -> None:
        # Upload the image inline via attach:// (1 request instead of 2).
        self._call("createNewStickerSet", data={
            "user_id": user_id, "name": name, "title": title,
            "sticker_type": "custom_emoji",
            "stickers": json.dumps([_sticker_json(emoji, keywords)]),
        }, files={"file0": (png.name, png.read_bytes(), "image/png")},
            applied_check=self._created_check(name))

    def add_sticker(self, user_id: int, name: str, png: Path,
                    emoji: str, keywords: str, *,
                    expected_before: int | None = None) -> None:
        """Add a static sticker. Pass ``expected_before`` (the live sticker
        count the caller expects BEFORE this add) to make network retries
        duplicate-proof; without it the historical blind retry is kept."""
        self._call("addStickerToSet", data={
            "user_id": user_id, "name": name,
            "sticker": json.dumps(_sticker_json(emoji, keywords)),
        }, files={"file0": (png.name, png.read_bytes(), "image/png")},
            applied_check=self._added_check(name, expected_before))

    # ----- multi-format helpers (static / animated / video) -------------- #
    def get_sticker_set(self, name: str) -> dict:
        """Return the full Bot API StickerSet object for a set short name."""
        return self._call("getStickerSet", data={"name": name})

    def get_custom_emoji_stickers(self, custom_emoji_ids: list[str]) -> list[dict]:
        """Resolve custom-emoji IDs to their Sticker objects (max 200 per call).

        Any bot can resolve arbitrary ``custom_emoji_id`` values; the Bot API
        silently drops IDs it cannot find, so the returned list may be shorter
        than the input. Each returned Sticker carries ``custom_emoji_id``,
        ``file_id`` and ``file_unique_id`` plus the animated/video flags.
        """
        if not custom_emoji_ids:
            return []
        if len(custom_emoji_ids) > 200:
            raise ValueError("getCustomEmojiStickers accepts at most 200 IDs per call")
        return self._call("getCustomEmojiStickers",
                          data={"custom_emoji_ids": json.dumps(custom_emoji_ids)})

    def download_file(self, file_id: str, dest: Path, retries: int = 5) -> Path:
        """Download a Telegram file (by file_id) to ``dest`` (with retries)."""
        info = self._call("getFile", data={"file_id": file_id})
        url = f"{api_base()}/file/bot{self.token}/{info['file_path']}"
        for attempt in range(1, retries + 1):
            try:
                r = self.s.get(url, timeout=60)
                r.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.content)
                return dest
            except requests.RequestException as exc:
                if attempt >= retries:
                    break
                wait = min(3 * attempt, 15)
                print(f"  download retry {attempt}/{retries}: "
                      f"{self._safe(exc)} (wait {wait}s)", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"download failed for file_id {file_id}")

    def create_emoji_set(self, user_id: int, name: str, title: str, path: Path,
                         fmt: str, emoji_list: list[str], keywords: list[str]) -> None:
        """Create a custom-emoji set whose first emoji is ``path`` (any format)."""
        self._call("createNewStickerSet", data={
            "user_id": user_id, "name": name, "title": title,
            "sticker_type": "custom_emoji",
            "stickers": json.dumps([_input_sticker(fmt, emoji_list, keywords)]),
        }, files={"file0": (path.name, path.read_bytes(), _mime_for_path(path))},
            applied_check=self._created_check(name))

    def add_emoji(self, user_id: int, name: str, path: Path, fmt: str,
                  emoji_list: list[str], keywords: list[str], *,
                  expected_before: int | None = None) -> None:
        """Add one emoji (any format) to an existing custom-emoji set.

        ``expected_before`` (the live sticker count expected BEFORE this add)
        makes network retries duplicate-proof; see ``add_sticker``."""
        self._call("addStickerToSet", data={
            "user_id": user_id, "name": name,
            "sticker": json.dumps(_input_sticker(fmt, emoji_list, keywords)),
        }, files={"file0": (path.name, path.read_bytes(), _mime_for_path(path))},
            applied_check=self._added_check(name, expected_before))


_MIME_BY_FORMAT = {
    "static": "image/png",
    "animated": "application/gzip",
    "video": "video/webm",
}

_MIME_BY_EXT = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tgs": "application/gzip",
    ".webm": "video/webm",
}


def _mime_for(fmt: str) -> str:
    return _MIME_BY_FORMAT.get(fmt, "application/octet-stream")


def _mime_for_path(path: Path) -> str:
    """MIME type derived from the file's real extension (preferred for uploads)."""
    return _MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")


def _trim_keywords(keywords: list[str]) -> list[str]:
    """Clamp a keyword list to Telegram's per-sticker budget (<=20, ~64 chars)."""
    kw: list[str] = []
    total = 0
    for raw in keywords:
        k = (raw or "").strip()[:48]
        if not k:
            continue
        if kw and total + len(k) + 1 > 60:
            break
        kw.append(k)
        total += len(k) + 1
        if len(kw) >= 20:
            break
    return kw


def _input_sticker(fmt: str, emoji_list: list[str], keywords: list[str]) -> dict:
    """Build a Bot API InputSticker for any custom-emoji format (uploaded as file0)."""
    emojis = [e for e in (emoji_list or []) if e][:20] or [DEFAULT_EMOJI]
    return {"sticker": "attach://file0", "format": fmt,
            "emoji_list": emojis, "keywords": _trim_keywords(keywords)}


def _sticker_json(emoji: str, keywords: str) -> dict:
    """Static InputSticker from a comma-separated keyword string."""
    return {"sticker": "attach://file0", "format": "static",
            "emoji_list": [emoji],
            "keywords": _trim_keywords(keywords.split(","))}


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="Set name base (letters/digits/_).")
    ap.add_argument("--title", required=True, help="Human-readable set title.")
    ap.add_argument("--user-id", type=int, default=int(os.environ.get("PACK_OWNER_USER_ID", "0")))
    ap.add_argument("--emoji", default=DEFAULT_EMOJI, help="Associated standard emoji.")
    ap.add_argument("--per-set", type=int, default=PER_SET)
    ap.add_argument("--limit", type=int, default=0, help="Max images to add (0=all).")
    ap.add_argument("--start", type=int, default=0, help="Skip this many images first.")
    ap.add_argument("--source-dir", default=str(EMOJI_DIR),
                    help="Folder of 100x100 PNGs to upload (default: logos/emoji).")
    ap.add_argument("--keywords", default="auto",
                    help="keywords.csv (ticker->keywords). 'auto' uses keywords.csv "
                         "only for the default coin source; missing file is OK.")
    ap.add_argument("--token-env", default="TELEGRAM_BOT_TOKEN",
                    help="Env var holding the bot token (e.g. GENERAL_BOT_TOKEN).")
    ap.add_argument("--state", default="",
                    help="Resume state file (default: state_<base>.json).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"ERROR: {args.token_env} not set (env or .env).", file=sys.stderr)
        return 2
    if not args.user_id:
        print("ERROR: provide --user-id or PACK_OWNER_USER_ID (your numeric Telegram id).",
              file=sys.stderr)
        return 2
    if not 1 <= args.per_set <= MAX_PER_SET:
        print(f"ERROR: --per-set must be between 1 and {MAX_PER_SET} "
              f"(Telegram's cap for a custom-emoji set); got {args.per_set}.",
              file=sys.stderr)
        return 2
    if args.limit < 0 or args.start < 0:
        print("ERROR: --limit and --start must not be negative.", file=sys.stderr)
        return 2

    # Per-base state file so coin and general packs never clobber each other.
    state_file = Path(args.state) if args.state else ROOT / f"state_{args.base}.json"

    # Source: prepared 100x100 emoji PNGs (run make_emoji_pngs.py first).
    source_dir = Path(args.source_dir)
    sources = sorted(source_dir.glob("*.png"))
    if args.start:
        sources = sources[args.start:]
    if args.limit:
        sources = sources[:args.limit]
    if not sources:
        print(f"ERROR: no PNGs in {source_dir}. Run make_emoji_pngs.py first.",
              file=sys.stderr)
        return 2

    # 'auto' loads the coin keywords.csv only for the default coin source dir; a
    # general pack uses no keywords unless --keywords points at a file.
    if args.keywords == "auto":
        kw_path = KEYWORDS_CSV if source_dir.resolve() == EMOJI_DIR.resolve() else None
    else:
        kw_path = Path(args.keywords)
    keywords = load_keywords(kw_path) if kw_path else {}

    # Dry run validates inputs WITHOUT calling Telegram (no network required).
    if args.dry_run:
        total = len(sources)
        n_sets = (total + args.per_set - 1) // args.per_set
        print(f"DRY RUN: token-env={args.token_env}  owner_user_id={args.user_id}  "
              f"source={source_dir}  images={total}  keywords={len(keywords)}", flush=True)
        print(f"DRY RUN: {total} images -> {n_sets} set(s) of up to {args.per_set}, "
              f"named {args.base}1_by_<bot> ...  state={state_file.name}", flush=True)
        return 0

    tg = Telegram(token)
    me = tg.get_me()
    bot_username = me["username"]

    # Resume support: load progress so an interrupted/flood-limited run can
    # continue without recreating existing sets or re-adding emojis.
    state = {"base": args.base, "per_set": args.per_set, "done": [], "sets": []}
    if state_file.is_file():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Continuing from the empty default would treat every already
            # uploaded image as pending and upload the whole set a second time.
            print(f"ERROR: cannot read resume state {state_file}: {exc}\n"
                  f"       Refusing to start from scratch -- that would re-upload "
                  f"everything already published.\n"
                  f"       Inspect or delete the file deliberately, then re-run.",
                  file=sys.stderr)
            return 4
        if loaded.get("base") == args.base:
            state = loaded
    done = set(state["done"])
    sets = state["sets"]

    pending = [p for p in sources if p.stem.lower() not in done]

    # Resume safety. A previous run can have applied an upload without recording
    # it (crash, or an ambiguous network failure). Which image that was is known
    # exactly, because the intent is written BEFORE the request: state["in_flight"]
    # names it. Positional attribution -- "the first (live - counted) pending
    # images must be the unrecorded ones" -- is wrong as soon as any earlier
    # image was skipped for being missing/unusable, because a skip consumes a
    # position in `pending` without producing a sticker; that mis-attribution
    # both re-uploads a duplicate and marks the wrong image as done.
    if sets:
        known, sset = tg.probe_sticker_set(sets[-1]["name"])
        if known and sset is not None:
            live_n = len(sset.get("stickers", []))
            drift = live_n - sets[-1]["count"]
            in_flight = state.get("in_flight")
            if drift == 1 and in_flight:
                done.add(in_flight)
                pending = [p for p in pending if p.stem.lower() != in_flight]
                sets[-1]["count"] = live_n
                print(f"  reconciled from live: {in_flight} was applied before "
                      f"the crash", flush=True)
            elif drift > 0:
                # More live stickers than we can account for: another run, a
                # manual edit, or a lost in-flight record. Guessing here is what
                # writes the wrong emoji id onto the wrong item.
                print(f"ERROR: {sets[-1]['name']} has {live_n} stickers but state "
                      f"records {sets[-1]['count']} and no single in-flight upload "
                      f"explains the difference.\n"
                      f"       Refusing to guess which images are already live. "
                      f"Reconcile the set manually, or delete it and re-run.",
                      file=sys.stderr)
                return 4
    state["in_flight"] = None

    print(f"Bot: @{bot_username}  owner_user_id={args.user_id}  "
          f"images={len(sources)}  already_done={len(done)}  pending={len(pending)}", flush=True)

    def save_state() -> None:
        state["done"] = sorted(done)
        state["sets"] = sets
        state["sent"] = sorted(sent)
        write_json_atomic(state_file, state)

    sent = set(state.get("sent", []))

    def notify(name: str, title: str) -> None:
        """Send the share/add link of a finished pack to the owner, once."""
        if name in sent:
            return
        try:
            tg.send_message(
                args.user_id,
                f"\u2705 {title}\nhttps://t.me/addemoji/{name}",
            )
            sent.add(name)
            print(f"  sent link for {name}", flush=True)
        except Exception as exc:  # noqa: BLE001 - never let notify break the build
            print(f"  notify failed for {name}: {exc}", flush=True)

    def notify_full_sets(final: bool = False) -> None:
        """Send links for finished packs, ALWAYS in ascending pack-number order.
        Any non-last set is complete; the last set counts as finished when full
        or when the whole run is done."""
        ordered = sorted(sets, key=lambda s: s["index"])
        last_index = max((s["index"] for s in sets), default=0)
        for s in ordered:
            if final or s["index"] != last_index or s["count"] >= args.per_set:
                notify(s["name"], s["title"])

    # Catch up: send links for any already-finished packs not yet sent.
    notify_full_sets()
    save_state()

    # Reconstruct the active set (last one that is not yet full).
    if sets and sets[-1]["count"] < args.per_set:
        set_index = sets[-1]["index"]
        set_name = sets[-1]["name"]
        in_set = sets[-1]["count"]
    else:
        set_index = len(sets)
        set_name = ""
        in_set = 0

    created = []
    try:
        for i, path in enumerate(pending):
            ticker = path.stem.lower()
            kw = keywords.get(ticker, ticker)

            # Skip unusable files so one bad logo never stops the whole run.
            if not path.is_file() or path.stat().st_size == 0:
                print(f"  skip {ticker}: missing/empty file", flush=True)
                continue

            # Write the intent BEFORE the request. If the process dies between
            # Telegram applying the upload and us recording it, the next run
            # knows exactly which image that was instead of inferring it from
            # positions (see the resume block above).
            state["in_flight"] = ticker
            save_state()

            try:
                placed = False
                if in_set != 0:
                    try:
                        tg.add_sticker(args.user_id, set_name, path, args.emoji, kw,
                                       expected_before=in_set)
                        sets[-1]["count"] += 1
                        placed = True
                    except RuntimeError as exc:
                        # Set is full (count drift or 200-limit): roll to a new set.
                        if "STICKERS_TOO_MUCH" not in str(exc):
                            raise
                        in_set = 0
                if not placed:
                    set_index += 1
                    set_name = f"{args.base}{set_index}_by_{bot_username}"
                    title = f"{args.title} {set_index}"  # every pack is numbered
                    tg.create_set(args.user_id, set_name, title, path, args.emoji, kw)
                    sets.append({"name": set_name, "title": title, "count": 1,
                                 "index": set_index})
                    created.append(set_name)
                    print(f"[set {set_index}] created {set_name}", flush=True)
            except AmbiguousUploadError as exc:
                # The call may or may not be live. NEVER re-send (that is how
                # the same emoji ends up in a pack twice); adopt a create that
                # verifiably landed, otherwise leave it to the next-run
                # live-count reconcile above.
                if not placed and in_set == 0:
                    known, sset = tg.probe_sticker_set(set_name)
                    if known and sset is not None and len(sset.get("stickers", [])) == 1:
                        sets.append({"name": set_name, "title": title, "count": 1,
                                     "index": set_index})
                        created.append(set_name)
                        print(f"[set {set_index}] adopted {set_name} after "
                              f"ambiguous create", flush=True)
                    else:
                        set_index -= 1
                        print(f"  {ticker}: {exc}; left for next-run reconcile", flush=True)
                        save_state()
                        continue
                else:
                    print(f"  {ticker}: {exc}; left for next-run reconcile", flush=True)
                    save_state()
                    continue
            except RuntimeError as exc:
                # Non-retryable error for THIS sticker (e.g. bad image): skip it.
                if not placed and in_set == 0:
                    set_index -= 1  # undo the index reserved for the failed create
                print(f"  skip {ticker}: {exc}", flush=True)
                continue
            in_set += 1
            done.add(ticker)
            state["in_flight"] = None
            # Persist immediately: batching this every 10 items is what leaves a
            # window where an upload is live but unrecorded.
            save_state()
            if in_set >= args.per_set:
                in_set = 0
            notify_full_sets()  # send link as soon as a pack is full
            if (i + 1) % 50 == 0:
                print(f"  ...{i + 1}/{len(pending)} added this run", flush=True)
            time.sleep(0.1)
        # All logos processed: the last (partial) pack is finished too.
        notify_full_sets(final=True)
    finally:
        save_state()

    print("", flush=True)
    print(f"DONE. {len(done)} total emojis across {len(sets)} set(s). "
          f"New sets this run: {len(created)}.", flush=True)
    for s in sets:
        print(f"  https://t.me/addemoji/{s['name']}  ({s['count']})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
