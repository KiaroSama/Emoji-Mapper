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
  - GENERAL_BOT_TOKEN   -> @YourEmojiBot, for general (non-coin) packs

Telegram limits each custom-emoji set to 200 emojis, so the images are split
into multiple sets named ``<base><n>_by_<botusername>``. Each emoji is given an
associated standard emoji (--emoji) and optional searchable keywords (from a
keywords.csv mapping ``ticker -> keywords``; falls back to the file name).

Usage (crypto coins, original bot):
  python -m emojikit.build_pack --base cryptoemoji --title "@YourBrand Crypto Emoji" \
      [--user-id 123] [--emoji ߞ] [--limit N] [--start N] [--dry-run]

Usage (general pack, new bot):
  python -m emojikit.build_pack --base mystickers --title "My Emojis" \
      --source-dir build/myset --token-env GENERAL_BOT_TOKEN --emoji ߘ

Run with --dry-run first to validate inputs without calling Telegram.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path


from emojikit.announce import announce_packs
from emojikit.packstate import (LockBusy, StateInvalid, _intent_key,
                       exclusive_lock, make_intent,
                       pack_family_lock_path, validate_state_shape,
                       write_json_atomic)
from emojikit.telegram_api import (DEFAULT_EMOJI, MAX_PER_SET, PER_SET,
                          AmbiguousUploadError, SetState, Telegram)

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("build_pack")


# Exit codes shared by every CLI entry point, so a launcher or CI job can tell
# "done", "bad input", "retry me" and "stop" apart. Returning 0 after total
# failure made retry logic and menu actions treat a dead run as a success.
EXIT_OK = 0
EXIT_USAGE = 2       # invalid arguments or configuration
EXIT_PARTIAL = 3     # some items succeeded, some failed -- retryable
EXIT_FAILED = 4      # nothing succeeded, or an integrity stop


def ingest_exit_code(succeeded: int, failed: int) -> int:
    """Exit code for a batch that processed ``succeeded`` and ``failed`` items."""
    if not failed:
        return EXIT_OK
    return EXIT_PARTIAL if succeeded else EXIT_FAILED


# What to do about emoji Telegram repaints (see media.is_repaintable).
REPAINT_MODES = ("ask", "skip", "keep")
_REPAINT_SAMPLE = 8
_NO_ANSWER = ("  nobody answered, so skipping them. Re-run with "
              "--repaintable keep to ingest them.")


def repaintable_gate(labels: list[str], *, mode: str = "ask",
                     prompt=None) -> bool:
    """Should these repaintable emoji be ingested? True = keep them.

    ``ask`` prompts, but only when there is someone to answer: with no tty the
    answer is SKIP, because skipping is the reversible half. A skipped emoji is
    one re-run away with ``--repaintable keep``; one already published into a
    live set has to be replaced sticker by sticker -- the exact round trip this
    gate exists to prevent.
    """
    if not labels:
        return True
    n = len(labels)
    shown = ", ".join(labels[:_REPAINT_SAMPLE])
    if n > _REPAINT_SAMPLE:
        shown += f", ... (+{n - _REPAINT_SAMPLE} more)"
    warning = "\n".join((
        f"WARNING: {n} of these emoji are REPAINTABLE: {shown}",
        "  Telegram OVERRIDES their colours with the text/accent colour, so"
        " what you see in the source pack is not the stored art.",
        "  In a pack without the flag they show that stored art instead --"
        " sometimes flat black, sometimes full colour, so LOOK before deciding.",
        "  The flag is set once per set at creation; it cannot be added later.",
    ))
    log.warning("%d repaintable emoji in this batch: %s", n, shown)
    print(warning, file=sys.stderr)

    if mode == "keep":
        print("  --repaintable keep: ingesting them anyway.", file=sys.stderr)
        return True
    if mode == "skip":
        print("  --repaintable skip: leaving them out.", file=sys.stderr)
        return False

    if prompt is None:
        if not sys.stdin.isatty():
            print(_NO_ANSWER, file=sys.stderr)
            return False
        prompt = input
    try:
        answer = prompt("  Ingest them anyway? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        # isatty() is NOT enough: under Git Bash `... < /dev/null` still reports
        # a tty, and input() then raises EOFError and kills the whole ingest.
        # A question nobody answered is a no, never a crash.
        print("", file=sys.stderr)
        print(_NO_ANSWER, file=sys.stderr)
        return False
    return answer.strip().lower() in ("y", "yes")




def safe_int_env(name: str, default: int = 0, *, minimum: int | None = None,
                 maximum: int | None = None) -> int:
    """Parse a numeric env var without letting a typo kill the process.

    ``int(os.environ.get(...))`` at import time turns one bad character in .env
    into an unexplained crash before argparse can print anything useful.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"WARNING: {name} is not a whole number; using {default}.",
              file=sys.stderr)
        return default
    if minimum is not None and value < minimum:
        print(f"WARNING: {name}={value} below {minimum}; using {minimum}.",
              file=sys.stderr)
        return minimum
    if maximum is not None and value > maximum:
        print(f"WARNING: {name}={value} above {maximum}; using {maximum}.",
              file=sys.stderr)
        return maximum
    return value


# Default source/keyword locations (crypto-coin workflow). Override per run with
# --source-dir / --keywords so the same engine builds any kind of emoji pack.
EMOJI_DIR = ROOT / "logos" / "emoji"
KEYWORDS_CSV = ROOT / "keywords.csv"





def _under_a_test_runner() -> bool:
    """True when a test runner, not a tool, owns this process.

    The suite's own guard (tests/__init__.py) sets EMOJI_MAPPER_NO_DOTENV, but
    it protects only what is imported AFTER it, and it runs at all only when
    `tests` is imported as a package -- `unittest discover -s tests` without
    `-t .` loads the modules as top level and skips it. Either way the modules
    under test call load_env() at import time and put the real credentials back;
    the scrub that follows removes credential-SHAPED names, so anything else in
    .env survives in os.environ for the whole run.

    Deciding here makes the protection independent of how the suite was invoked
    and of which module imported first. The signal is exact -- it reads the spec
    of the process entry point, so an ordinary CLI run cannot trip it -- and a
    false positive would only mean .env is not auto-loaded, with explicit
    environment variables still working. It fails safe in both directions.
    """
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    entry = getattr(spec, "name", "") or ""
    return entry.split(".")[0] in {"unittest", "pytest"} or "pytest" in sys.modules


def load_env() -> None:
    # Several modules call load_env() at IMPORT time, which would put the real
    # credentials straight back into os.environ after the suite scrubbed them --
    # reopening the hole that once let a test reach live Telegram and replace a
    # sticker in a production pack.
    if os.environ.get("EMOJI_MAPPER_NO_DOTENV") == "1" or _under_a_test_runner():
        return
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




def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="Set name base (letters/digits/_).")
    ap.add_argument("--title", required=True, help="Human-readable set title.")
    ap.add_argument("--user-id", type=int,
                    default=safe_int_env("PACK_OWNER_USER_ID", 0, minimum=0))
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

    # Locked by pack FAMILY, not by state file: the coin providers and the
    # rebuild tool mutate the same sets through different state files, so a
    # per-file lock let them run concurrently against one family.
    try:
        with exclusive_lock(pack_family_lock_path(args.base)):
            return _run_build(args, token, state_file, sources, keywords)
    except LockBusy as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FAILED


def _run_build(args, token, state_file, sources, keywords) -> int:
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
        # An existing state file that belongs to a DIFFERENT pack, or was
        # written with different settings, must not be silently ignored: doing
        # so restarts a published pack from zero and re-uploads everything.
        mismatches = []
        if loaded.get("base") != args.base:
            mismatches.append(f"base {loaded.get('base')!r} != {args.base!r}")
        if loaded.get("per_set") not in (None, args.per_set):
            mismatches.append(f"per_set {loaded.get('per_set')} != {args.per_set}")
        if mismatches:
            print(f"ERROR: resume state {state_file} does not match this run "
                  f"({'; '.join(mismatches)}).\n"
                  f"       Refusing to continue: starting from an empty state "
                  f"would re-upload an already published pack.\n"
                  f"       Use --state for a different file, or delete that one "
                  f"deliberately.", file=sys.stderr)
            return EXIT_FAILED
        try:
            validate_state_shape(loaded, base=args.base, per_set=args.per_set)
        except StateInvalid as exc:
            print(f"ERROR: resume state {state_file} is inconsistent: {exc}.\n"
                  f"       Every resume decision is derived from these numbers, "
                  f"so refusing to mutate Telegram from them.", file=sys.stderr)
            return EXIT_FAILED
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
    intent = state.get("in_flight")
    in_flight = _intent_key(intent)

    # 1) Settle the unresolved intent FIRST, against the set IT names.
    #    Probing only sets[-1] was wrong for an ambiguous CREATE of set #2 or
    #    later: the new set is not in state["sets"] yet, so its name lives only
    #    in the intent, and the old last set says nothing about whether it
    #    landed.
    if in_flight:
        if isinstance(intent, dict):
            target = intent.get("set_name")
            operation = intent.get("operation")
        elif sets:
            # A state file written before intents were structured records only
            # the item key. The last recorded set is the only target that
            # version could have been writing to, so reconcile against it
            # rather than refusing to upgrade.
            intent = {"key": in_flight, "operation": "add",
                      "set_name": sets[-1]["name"],
                      "expected_before": sets[-1]["count"]}
            target, operation = intent["set_name"], "add"
        else:
            target = operation = None
        if not target:
            print(f"ERROR: the unresolved upload of {in_flight} does not name a "
                  f"target set, so it cannot be reconciled.\n"
                  f"       Check Telegram, then delete {state_file.name} "
                  f"deliberately.", file=sys.stderr)
            return EXIT_FAILED

        set_state, sset = tg.probe_set_state(target)
        if set_state is SetState.UNKNOWN:
            print(f"ERROR: cannot determine the live state of {target}.\n"
                  f"       Refusing to continue while {in_flight} may be "
                  f"unresolved. Retry when Telegram is reachable.",
                  file=sys.stderr)
            return EXIT_PARTIAL

        # The image the interrupted request was carrying. Reconciliation
        # compares CONTENT against it: "the set exists" and "the count is one
        # higher" are both equally true when someone else's sticker landed and
        # ours did not.
        in_flight_src = next(
            (p for p in sources if p.stem.lower() == in_flight), None)

        recorded = next((s for s in sets if s["name"] == target), None)
        if operation == "create":
            if set_state is SetState.EXISTS:
                live_n = len(sset.get("stickers", []))
                # A create leaves EXACTLY one sticker. A set holding more was
                # not left by our interrupted create alone, so a matching first
                # sticker proves nothing about the rest -- adopting it records
                # someone else's stickers as this run's own.
                if live_n != 1:
                    print(f"ERROR: {target} holds {live_n} stickers, but the "
                          f"unresolved create of {in_flight} would have left "
                          f"exactly one.\n"
                          f"       Refusing to adopt a set this run may not have "
                          f"created.", file=sys.stderr)
                    return EXIT_FAILED
                first = (sset.get("stickers") or [None])[0]
                proof = (tg._sticker_matches(first, in_flight_src)
                         if first is not None and in_flight_src else None)
                if proof is not True:
                    print(f"ERROR: {target} exists but its first sticker "
                          f"{'does not match' if proof is False else 'could not be compared with'} "
                          f"{in_flight}.\n"
                          f"       Refusing to adopt a set this run may not have "
                          f"created.", file=sys.stderr)
                    return EXIT_FAILED if proof is False else EXIT_PARTIAL
                if recorded is None:
                    sets.append({"name": target,
                                 "title": intent.get("title", ""),
                                 "count": live_n,
                                 "index": intent.get("set_index",
                                                     len(sets) + 1)})
                else:
                    recorded["count"] = live_n
                done.add(in_flight)
                pending = [p for p in pending if p.stem.lower() != in_flight]
                print(f"  adopted {target} created before the interruption "
                      f"({live_n} sticker(s))", flush=True)
            else:
                print(f"  create of {target} did not land; {in_flight} stays "
                      f"pending", flush=True)
        else:                                    # an ADD
            if set_state is SetState.MISSING:
                print(f"ERROR: {target} no longer exists, but an add to it is "
                      f"unresolved.\n       Reconcile manually before "
                      f"continuing.", file=sys.stderr)
                return EXIT_FAILED
            live_n = len(sset.get("stickers", []))
            expected = intent.get("expected_before")
            if expected is None and recorded is not None:
                expected = recorded["count"]
            if expected is None:
                print(f"ERROR: the unresolved add of {in_flight} to {target} "
                      f"records no expected count; refusing to guess.",
                      file=sys.stderr)
                return EXIT_FAILED
            if live_n < expected:
                print(f"ERROR: {target} holds {live_n} stickers but the "
                      f"unresolved add of {in_flight} expected at least "
                      f"{expected}.\n       Stickers were removed; refusing to "
                      f"guess which images are live.", file=sys.stderr)
                return EXIT_FAILED
            # Counting cannot answer this. The sequence this reconciler exists
            # for -- our attempt fails, a FOREIGN sticker lands, our retry then
            # succeeds -- leaves the set two bigger, and demanding expected or
            # expected+1 turned that into a permanent EXIT_FAILED with our
            # sticker live and off the books forever. Ask the only question that
            # matters instead: is OUR image among the ones that arrived?
            #
            # POSITION cannot bound that question. `expected` marks the tail
            # only while stickers are appended and never removed: delete one
            # and add ours and live_n == expected, which left the old slice
            # EMPTY, never consulted the content oracle, announced "did not
            # land" and re-uploaded an image that was already live -- exiting 0.
            # So the search space is the whole set, tail first: an ordinary
            # append is still found in one comparison, and the walk over the
            # rest costs downloads once per interrupted run, never once per
            # upload.
            stickers = sset.get("stickers", [])
            grew = live_n > expected
            # True: ours is live. False: provably absent. None: unproven.
            found = False
            for st in stickers[expected:] + stickers[:expected]:
                verdict = (tg._sticker_matches(st, in_flight_src)
                           if in_flight_src else None)
                if verdict is True:
                    found = True
                    break
                if verdict is None:
                    found = None         # keep looking; a match still decides it
            if found is True:
                # Ours is there. WHAT ELSE arrived does not change that, which
                # is the whole point: expected+2 is the normal outcome of a
                # foreign sticker landing between our failed attempt and our
                # successful retry, and rejecting it stranded a live sticker
                # off the books permanently.
                done.add(in_flight)
                pending = [p for p in pending if p.stem.lower() != in_flight]
                if recorded is not None:
                    recorded["count"] = live_n
                print(f"  reconciled from live: {in_flight} was applied "
                      f"before the interruption", flush=True)
            elif found is False and not grew:
                print(f"  {in_flight} did not land; it stays pending", flush=True)
            elif found is False:
                # Ours is provably absent, yet the set grew: a hand edit, not
                # our upload. The counts every later position is derived from
                # are no longer ours to reason about.
                print(f"ERROR: {target} grew by {live_n - expected}, and none "
                      f"of its {live_n} stickers is {in_flight} -- someone else "
                      f"wrote to this set.\n       Refusing to attribute it to "
                      f"this run.", file=sys.stderr)
                return EXIT_FAILED
            else:
                print(f"ERROR: cannot verify whether {in_flight} is among the "
                      f"{live_n} sticker(s) in {target}.\n       Refusing to "
                      f"guess. Retry when the images can be compared.",
                      file=sys.stderr)
                return EXIT_PARTIAL
        state["in_flight"] = None                # verified postcondition

    # 2) Every RECORDED set must still match what state claims. A set that was
    #    deleted or shrunk by hand invalidates the counts the whole resume is
    #    built on, and drift < 0 used to be ignored entirely.
    for s in sets:
        set_state, sset = tg.probe_set_state(s["name"])
        if set_state is SetState.UNKNOWN:
            print(f"ERROR: cannot determine the live state of {s['name']}.\n"
                  f"       Retry when Telegram is reachable.", file=sys.stderr)
            return EXIT_PARTIAL
        if set_state is SetState.MISSING:
            print(f"ERROR: recorded set {s['name']} no longer exists.\n"
                  f"       Refusing to continue against a state that describes "
                  f"a deleted pack. Restore it, or delete {state_file.name} to "
                  f"start this family again.", file=sys.stderr)
            return EXIT_FAILED
        live_n = len(sset.get("stickers", []))
        if live_n < s["count"]:
            print(f"ERROR: {s['name']} holds {live_n} stickers but state records "
                  f"{s['count']}; stickers were removed.\n"
                  f"       Refusing to publish against a shrunken set.",
                  file=sys.stderr)
            return EXIT_FAILED
        if live_n > s["count"]:
            print(f"ERROR: {s['name']} holds {live_n} stickers but state records "
                  f"{s['count']} and no in-flight upload explains it.\n"
                  f"       Refusing to guess which images are already live.",
                  file=sys.stderr)
            return EXIT_FAILED

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
            dest = announce_packs(tg, args.user_id,
                                  [{"name": name, "title": title}], bot="general")
            sent.add(name)
            print(f"  sent link for {name} to {dest}", flush=True)
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
    failed: list[str] = []      # items this run could not upload
    uploaded = 0
    try:
        for i, path in enumerate(pending):
            ticker = path.stem.lower()
            kw = keywords.get(ticker, ticker)

            # Skip unusable files so one bad logo never stops the whole run.
            if not path.is_file() or path.stat().st_size == 0:
                print(f"  skip {ticker}: missing/empty file", flush=True)
                failed.append(ticker)
                continue

            # Write the intent BEFORE the request. If the process dies between
            # Telegram applying the upload and us recording it, the next run
            # knows exactly which image that was instead of inferring it from
            # positions (see the resume block above).
            if in_set != 0:
                state["in_flight"] = make_intent(
                    key=ticker, operation="add", set_name=set_name,
                    set_index=set_index, expected_before=in_set)
            else:
                next_index = set_index + 1
                state["in_flight"] = make_intent(
                    key=ticker, operation="create",
                    set_name=f"{args.base}{next_index}_by_{bot_username}",
                    set_index=next_index, expected_before=0,
                    title=f"{args.title} {next_index}")
            save_state()

            try:
                placed = False
                if in_set != 0:
                    try:
                        live = tg.add_sticker(args.user_id, set_name, path,
                                              args.emoji, kw,
                                              expected_before=in_set)
                        # A retried add reports what is actually live: the failed
                        # attempt can have let a foreign sticker in, so the set
                        # grew by two while this counted one. Assuming +1 there
                        # made every later expected_before wrong by one.
                        sets[-1]["count"] = (sets[-1]["count"] + 1 if live is None
                                             else live)
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
                    # Replace the intent before the CREATE. On the rollover path
                    # the persisted intent still describes the ADD to the FULL
                    # set, so a crash here would send a restart looking at the
                    # wrong set and conclude the create never happened.
                    state["in_flight"] = make_intent(
                        key=ticker, operation="create", set_name=set_name,
                        set_index=set_index, expected_before=0, title=title)
                    save_state()
                    tg.create_set(args.user_id, set_name, title, path, args.emoji, kw)
                    sets.append({"name": set_name, "title": title, "count": 1,
                                 "index": set_index})
                    created.append(set_name)
                    print(f"[set {set_index}] created {set_name}", flush=True)
            except AmbiguousUploadError as exc:
                # The call may or may not be live. Try to settle it right here;
                # if it cannot be settled, STOP. Continuing to the next item
                # would overwrite this intent with the next one and destroy the
                # only record of which mutation is unresolved -- after which a
                # later run can re-send an upload that already landed.
                if not placed and in_set == 0:
                    set_state, sset = tg.probe_set_state(set_name)
                    stickers = (sset or {}).get("stickers", [])
                    # Shape is not identity -- the same lesson the restart branch
                    # already learned. A set of this name holding one sticker may
                    # be a stranger's; adopting it on the count records a foreign
                    # pack as ours, marks this item done though it was never
                    # uploaded, publishes the link, and then keeps writing our
                    # stickers into someone else's set. Our create puts our image
                    # in first, so that is what has to answer.
                    ours = (tg._sticker_matches(stickers[0], path)
                            if set_state is SetState.EXISTS and len(stickers) == 1
                            else None)
                    if ours is True:
                        sets.append({"name": set_name, "title": title, "count": 1,
                                     "index": set_index})
                        created.append(set_name)
                        state["in_flight"] = None   # verified postcondition
                        print(f"[set {set_index}] adopted {set_name} after "
                              f"ambiguous create", flush=True)
                    else:
                        save_state()                # keep the intent
                        print(f"ERROR: {ticker}: {exc}\n"
                              f"       The create is unresolved; refusing to "
                              f"start another upload. Re-run to reconcile.",
                              file=sys.stderr)
                        return EXIT_PARTIAL
                else:
                    save_state()                    # keep the intent
                    print(f"ERROR: {ticker}: {exc}\n"
                          f"       The upload is unresolved; refusing to start "
                          f"another one. Re-run to reconcile.", file=sys.stderr)
                    return EXIT_PARTIAL
            except RuntimeError as exc:
                # Non-retryable error for THIS sticker (e.g. bad image): the
                # request definitively did not apply, so the intent is settled
                # and the run may continue with the next item.
                if not placed and in_set == 0:
                    set_index -= 1  # undo the index reserved for the failed create
                state["in_flight"] = None
                failed.append(ticker)
                save_state()
                print(f"  skip {ticker}: {exc}", flush=True)
                continue
            # Follow the recorded set rather than incrementing separately: after
            # a retried add that number is the live size, and two counters that
            # advance independently drift apart exactly when it matters.
            in_set = sets[-1]["count"]
            uploaded += 1
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
          f"New sets this run: {len(created)}. "
          f"Uploaded: {uploaded}. Failed/skipped: {len(failed)}.", flush=True)
    for s in sets:
        print(f"  https://t.me/addemoji/{s['name']}  ({s['count']})", flush=True)
    if failed:
        # A run that could not upload some images is not a success; automation
        # and the launcher menu previously saw exit 0 for a half-built pack.
        print(f"  failed/skipped: {', '.join(sorted(failed)[:20])}"
              f"{' ...' if len(failed) > 20 else ''}", file=sys.stderr)
    return ingest_exit_code(uploaded, len(failed))


if __name__ == "__main__":
    raise SystemExit(main())
