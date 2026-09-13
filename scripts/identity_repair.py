"""Report -- and, only when asked, migrate -- catalog identity after a decode fix.

Correcting how a video is decoded changes what its content key IS, and that key
is written down in more places than the table it names. The inventory, the
idempotent stages and the journal all live in `collection_migrate`; this file is
the command line over them.

    python scripts/identity_repair.py report
    python scripts/identity_repair.py migrate-video-keys --apply

`report` writes nothing. `migrate-video-keys` takes the pack-family lock, snapshots
the database through SQLite's online backup API, verifies that snapshot, and only
then moves keys, refreshes derived hashes, rewrites the publisher state and plan
files and renames the archived media whose names embed the old key.

Exit codes, and the distinction the previous version did not make:

    0  clean: everything was inspected and nothing is pending
    2  usage
    3  work is pending, and the picture is complete enough to act on
    4  INCOMPLETE or refused: something could not be inspected, or two rows
       would collide. Nothing was changed.

A row whose file is missing used to be counted as "unchanged", so a catalog
nobody could read reported "Every video key already matches" and exited 0. A
failed inspection is not a success, and it is not a migration input either.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import collection_migrate as cm  # noqa: E402
from emojikit import identity  # noqa: E402
from emojikit.logsetup import record_exit_code, setup_logging  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_STALE = 3      # work to do; nothing was changed
EXIT_FAILED = 4     # incomplete or refused; nothing was changed


def _print_survey(sv: cm.Survey, stale: dict[str, list[str]]) -> None:
    print(f"\nVIDEO IDENTITY: {sv.checked} row(s) checked")
    print(f"  keys that would change : {len(sv.changed)}")
    print(f"  hash-only corrections  : {len(sv.phash_only)}")
    print(f"  already correct        : {sv.unchanged}")
    print(f"  media missing on disk  : {len(sv.missing)}")
    print(f"  undecodable            : {len(sv.undecodable)}")
    print(f"  collisions             : {len(sv.collisions)}")
    for old, new, name in sv.changed[:10]:
        print(f"    {old} -> {new}   ({name})")
    if len(sv.changed) > 10:
        print(f"    ... and {len(sv.changed) - 10} more")
    for line in sv.missing:
        print(f"    MISSING {line}")
    for line in sv.undecodable:
        print(f"    UNDECODABLE {line}")
    for new, olds in sv.collisions.items():
        print(f"    COLLISION {new} <- {', '.join(olds)}")

    print(f"\n  state/plan files naming unknown keys: {len(stale)}")
    for name, keys in stale.items():
        print(f"    {name}: {len(keys)} stale key(s), e.g. {keys[0]}")


def _suspects(data_dir: Path) -> None:
    """Rows the OLD recovery rule could have attributed by guesswork.

    A heuristic and nothing more. It does not validate historical
    file_unique_id/custom_emoji_id mappings, and an empty list is not evidence
    that none of them was ever wrong -- the source handle a past download was
    attributed by is not kept, so those bindings cannot be re-derived at all.
    """
    import sqlite3
    con = sqlite3.connect(cm.catalog_path(data_dir))
    try:
        items = con.execute(
            "SELECT i.content_key, i.format, i.phash, "
            "  (SELECT COUNT(*) FROM seen_files s WHERE s.content_key=i.content_key) "
            "FROM items i WHERE i.phash IS NOT NULL").fetchall()
    finally:
        con.close()
    suspects = [(k, sum(1 for k2, f2, h2, _s in items
                        if k2 != k and f2 == fmt and identity.hamming(h2, h) <= 2))
                for k, fmt, h, seen in items if seen]
    suspects = [(k, n) for k, n in suspects if n]
    print(f"\nSUSPECT MAPPINGS: {len(suspects)} (heuristic only)")
    print("  Rows with a recorded Telegram id AND a look-alike of the same")
    print("  format. The old recovery rule compared grayscale structure alone,")
    print("  so these are the rows it COULD have confused. This is NOT a")
    print("  validation of historical id mappings, and zero here would not")
    print("  prove none was ever wrong: the handle a past download was")
    print("  attributed by is not stored, so it cannot be re-checked.")
    for key, n in suspects[:10]:
        print(f"    {key}  ({n} look-alike(s))")
    if len(suspects) > 10:
        print(f"    ... and {len(suspects) - 10} more")


def report(data_dir: Path) -> int:
    print(f"catalog: {cm.catalog_path(data_dir)}")
    journal = cm.read_journal(data_dir)
    if journal:
        print(f"\nAN INTERRUPTED MIGRATION IS RECORDED (stage: "
              f"{journal.get('stage')}, started {journal.get('started_utc')}).")
        print("  Re-run `migrate-video-keys --apply` to complete it; every "
              "stage is idempotent.")
    sv = cm.survey(data_dir)
    stale = cm.stale_state_keys(data_dir)
    _print_survey(sv, stale)
    _suspects(data_dir)

    if not sv.complete:
        print("\nINCOMPLETE: some rows could not be inspected, so what the "
              "catalog should become is not established. Nothing was changed.")
        return EXIT_FAILED
    if sv.collisions:
        print("\nRefusing to offer a migration while collisions exist: two rows "
              "would become one identity and one of them would lose its media.")
        return EXIT_FAILED
    if sv.pending or stale or journal:
        print("\nTo move every reference onto the corrected keys:")
        print("  python scripts/identity_repair.py migrate-video-keys --apply")
        return EXIT_STALE
    print("\nClean: every video key and hash matches, and no state file names "
          "a key the catalog does not have.")
    return EXIT_OK


def migrate(data_dir: Path, apply: bool, from_backup: Path | None = None) -> int:
    recovered: dict[str, str] = {}
    if from_backup is not None:
        if not from_backup.is_file():
            print(f"no backup at {from_backup}")
            return EXIT_USAGE
        recovered = cm.recover_key_map(data_dir, from_backup)
        print(f"recovered {len(recovered)} old->new pair(s) from "
              f"{from_backup.name}")
        stale_now = cm.stale_state_keys(data_dir)
        loose = {k for keys in stale_now.values() for k in keys}
        uncovered = sorted(k for k in loose
                           if k.startswith("v:") and k not in recovered)
        if uncovered:
            print(f"  {len(uncovered)} stale video key(s) are NOT in this "
                  f"backup, so they predate it and are left alone:")
            for k in uncovered[:5]:
                print(f"    {k}")

    sv = cm.survey(data_dir)
    stale = cm.stale_state_keys(data_dir)
    journal = cm.read_journal(data_dir)

    if not sv.complete:
        print(f"{len(sv.missing)} missing and {len(sv.undecodable)} undecodable "
              f"row(s). A key cannot be corrected from a file we cannot read, "
              f"and migrating the rest would leave the catalog half-converted.")
        for line in sv.missing + sv.undecodable:
            print(f"  {line}")
        return EXIT_FAILED
    if sv.collisions:
        print(f"{len(sv.collisions)} collision(s): two rows would claim one key. "
              f"Refusing -- merging them deletes media.")
        for new, olds in sv.collisions.items():
            print(f"  {new} <- {', '.join(olds)}")
        return EXIT_FAILED
    if not (sv.pending or stale or journal or recovered):
        print("Nothing to do: every video key and hash already matches, and no "
              "state file names a key the catalog does not have.")
        return EXIT_OK
    if not apply:
        print(f"{len(sv.changed)} key(s) would move, "
              f"{len(sv.phash_only)} hash-only correction(s), "
              f"{len(stale)} state/plan file(s) would be rewritten. "
              f"Re-run with --apply.")
        return EXIT_STALE

    result = cm.apply_migration(data_dir, sv, recovered=recovered)
    print(f"backup: {result['backup']}")
    print(f"{result['moved']} row(s) moved onto their corrected keys.")
    print(f"{len(sv.phash_only)} derived hash(es) refreshed.")
    print(f"state/plan files rewritten: {', '.join(result['state']) or 'none'}")
    print(f"archived files renamed: {len(result['renamed'])}")

    left = cm.stale_state_keys(data_dir)
    after = cm.survey(data_dir)

    # Did THIS migration finish? That is not the same question as whether every
    # plan file is tidy. A frozen plan can name a key that left the catalog
    # rounds ago -- the publisher skips such an entry (`item is None: continue`)
    # so it is noise rather than damage, and failing on it would brand every
    # future run incomplete for something this tool was never asked to move.
    moved = set(recovered) | set(sv.key_map)
    unmoved = sorted(k for keys in left.values() for k in keys if k in moved)
    if unmoved or after.pending:
        print(f"\nINCOMPLETE after applying: {len(unmoved)} key(s) this "
              f"migration moved are still named by a state file, "
              f"{len(after.changed)} key(s) still pending, "
              f"{len(after.phash_only)} hash(es) still stale, "
              f"{len(after.collisions)} collision(s).")
        return EXIT_FAILED
    print("\nVerified: every key this migration moved is gone from the state "
          "and plan files, and a re-survey finds nothing pending.")
    if left:
        total = sum(len(v) for v in left.values())
        print(f"\nPRE-EXISTING, not this migration's work: {total} key(s) in "
              f"{len(left)} plan file(s) name rows the catalog no longer has. "
              f"`report` lists them; the publisher skips those entries.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    setup_logging("identity_repair")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("report", "migrate-video-keys"),
                    nargs="?", default="report")
    ap.add_argument("--data-dir", default="collection")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it, migrate only reports.")
    ap.add_argument("--from-backup", metavar="DB",
                    help="repair state files left behind by a migration that "
                         "ran before journals existed, by recovering its "
                         "old->new map from the backup it wrote.")
    args = ap.parse_args(argv)
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    try:
        if args.command == "report":
            return report(data_dir)
        backup = Path(args.from_backup) if args.from_backup else None
        if backup is not None and not backup.is_absolute():
            backup = data_dir / backup
        return migrate(data_dir, args.apply, backup)
    except FileNotFoundError as exc:
        print(exc)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(record_exit_code(main()))
