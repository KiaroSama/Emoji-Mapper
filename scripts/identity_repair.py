"""Inspect, migrate, recover or restore catalog identity without uploading.

Commands: report; migrate-video-keys [--apply] [--from-backup DB];
restore --bundle PATH.rollback.json [--apply]. Reports preserve application
state. Applied migrations/restore own the canonical directory and retain an
interrupted journal until all application invariants are verified.

Exit 0 is verified/clean, 2 is usage, 3 is pending/preview, and 4 is incomplete
or refused. Exit 4 may follow partial application: retain the recovery bundle.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit import collection_migrate as cm  # noqa: E402
from emojikit import identity  # noqa: E402
from emojikit.logsetup import record_exit_code, setup_logging  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_STALE = 3      # work to do; nothing was changed
EXIT_FAILED = 4     # incomplete/refused; an interrupted run can be partly applied


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
    if sv.pending or cm.required_state_keys(data_dir) or journal:
        print("\nTo move every reference onto the corrected keys:")
        print("  python scripts/identity_repair.py migrate-video-keys --apply")
        return EXIT_STALE
    issues = cm.invariant_issues(data_dir)
    if issues:
        print("INCOMPLETE: " + "; ".join(issues))
        return EXIT_FAILED
    print("\nClean: identities, media and all required references agree.")
    if stale:
        print("Obsolete frozen-plan/skipped-history entries are retained; they are not live references.")
    return EXIT_OK


def migrate(data_dir: Path, apply: bool, from_backup: Path | None = None) -> int:
    # Journal replay precedes survey: a killed rename can leave SQLite naming
    # the old, absent source while the journal proves where its bytes went.
    with cm.maintenance(data_dir) as data_dir:
        journal = cm.read_journal(data_dir)
        if journal and apply:
            result = cm.apply_migration(data_dir)
        else:
            recovered = {}
            if from_backup is not None:
                if not from_backup.is_file():
                    print(f"no backup at {from_backup}")
                    return EXIT_USAGE
                recovered = cm.recover_key_map(data_dir, from_backup)
                print(f"recovered {len(recovered)} old->new pair(s) from {from_backup.name}")
            sv = cm.survey(data_dir)
            stale = cm.stale_state_keys(data_dir)
            required = cm.required_state_keys(data_dir)
            if not sv.complete or sv.collisions:
                _print_survey(sv, stale)
                print("INCOMPLETE: missing/undecodable media or colliding identities; refusing.")
                return EXIT_FAILED
            if not (sv.pending or required or journal or recovered):
                issues = cm.invariant_issues(data_dir)
                if issues:
                    print("INCOMPLETE: " + "; ".join(issues))
                    return EXIT_FAILED
                print("Nothing to do: identities, hashes and required references are consistent.")
                if stale:
                    print("Obsolete plan/skipped-history entries are retained unchanged.")
                return EXIT_OK
            if not apply:
                _print_survey(sv, stale)
                print("Re-run migrate-video-keys --apply to migrate or replay its journal.")
                return EXIT_STALE
            result = cm.apply_migration(data_dir, recovered=recovered)
        print(f"backup: {result['backup']}")
        print(f"rollback bundle: {result['bundle']}")
        print(f"{result['moved']} row(s) moved; {len(result['state'])} state/plan file(s) "
              f"rewritten; {len(result['renamed'])} media file(s) renamed.")
        print("Verified: complete identity survey, media, identifiers and required references "
              "agree; journal retired under maintenance ownership.")
        return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    setup_logging("identity_repair")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("report", "migrate-video-keys", "restore"),
                    nargs="?", default="report")
    ap.add_argument("--data-dir", default="collection")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it, migrate only reports.")
    ap.add_argument("--from-backup", metavar="DB",
                    help="repair state files left behind by a migration that "
                         "ran before journals existed, by recovering its "
                         "old->new map from the backup it wrote.")
    ap.add_argument("--bundle", metavar="JSON", help="rollback manifest produced by migration")
    args = ap.parse_args(argv)
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    try:
        if args.command == "restore":
            if not args.bundle:
                print("restore requires --bundle PATH.rollback.json")
                return EXIT_USAGE
            manifest = Path(args.bundle)
            if not manifest.is_absolute():
                manifest = data_dir / manifest
            cm.restore_migration(data_dir, manifest, apply=args.apply)
            print("Restored and verified the original application snapshot." if args.apply
                  else "Rollback bundle verified; re-run restore with --apply.")
            return EXIT_OK if args.apply else EXIT_STALE
        if args.command == "report":
            with cm.maintenance(data_dir):
                return report(data_dir)
        backup = Path(args.from_backup) if args.from_backup else None
        if backup is not None and not backup.is_absolute():
            backup = data_dir / backup
        return migrate(data_dir, args.apply, backup)
    except FileNotFoundError as exc:
        print(exc)
        return EXIT_FAILED if (data_dir / "catalog.db").is_file() else EXIT_USAGE
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"INCOMPLETE: {exc}")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(record_exit_code(main()))
