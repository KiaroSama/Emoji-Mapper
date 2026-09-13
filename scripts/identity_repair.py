"""Report -- and, only when asked, migrate -- catalog identity after a decode fix.

Correcting how a video is decoded changes what its content key IS. That key is
this project's primary identity: it names the row in `items`, it is the foreign
key in `publications` and `seen_files`, and its first twelve characters are
baked into every archived filename. So a decoder fix cannot simply ship -- the
rows it describes have to be moved with it, deliberately, with a backup, and
with collisions reported rather than merged.

Two commands, and the read-only one is the default:

    python scripts/identity_repair.py report
    python scripts/identity_repair.py migrate-video-keys --apply

`report` writes nothing. `migrate-video-keys` copies the database first, runs
inside one transaction, and refuses outright if two rows would collapse onto
one key -- merging them would delete media, which is the exact defect the
decoder fix exists to stop.

It also reports SUSPECT MAPPINGS: rows whose recorded Telegram file_unique_id
was attributed by the old recovery path, which accepted a candidate on a
grayscale perceptual hash alone and could therefore hand a stranger's sticker
our item's identity. Those are listed for a human. Nothing here remaps or
deletes production data on its own.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emojikit import identity  # noqa: E402
from emojikit.logsetup import record_exit_code, setup_logging  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_STALE = 3      # a report found work to do; nothing was changed
EXIT_FAILED = 4

# Every table that stores a content key. Missing one would leave a publication
# pointing at a row that no longer exists, which is worse than not migrating.
KEY_REFERENCES = (("items", "content_key"),
                  ("publications", "content_key"),
                  ("seen_files", "content_key"))


def _catalog(data_dir: Path) -> Path:
    db = data_dir / "catalog.db"
    if not db.is_file():
        raise SystemExit(f"no catalog at {db}")
    return db


def recompute_video_keys(db: Path) -> tuple[list[tuple], list[str], list[str]]:
    """(changed, undecodable, missing) for every video row, touching nothing.

    `changed` is [(old, new, name)]. Decoding is the expensive part, so this is
    the one pass both commands share.
    """
    changed: list[tuple[str, str, str]] = []
    undecodable: list[str] = []
    missing: list[str] = []
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT content_key, file_path FROM items WHERE format='video'"
        ).fetchall()
    finally:
        con.close()
    for old, path in rows:
        p = Path(path)
        if not p.is_file():
            missing.append(old)
            continue
        try:
            fresh = identity.content_key(p, "video")
        except Exception as exc:  # noqa: BLE001 - a failed decode is not a verdict
            undecodable.append(f"{old}: {type(exc).__name__}: {exc}")
            continue
        if fresh != old:
            changed.append((old, fresh, p.name))
    return changed, undecodable, missing


def collisions(db: Path, changed: list[tuple[str, str, str]]) -> dict[str, list[str]]:
    """New keys that more than one row would claim, or that already exist.

    Either case means two catalog rows are about to become one identity. The
    migration refuses rather than picking a winner: whichever row lost would
    take its media and its publication history with it.
    """
    by_new: dict[str, list[str]] = {}
    for old, new, _name in changed:
        by_new.setdefault(new, []).append(old)
    clashes = {new: olds for new, olds in by_new.items() if len(olds) > 1}

    con = sqlite3.connect(db)
    try:
        moving = {old for old, _n, _f in changed}
        for new, olds in by_new.items():
            row = con.execute("SELECT content_key FROM items WHERE content_key=?",
                              (new,)).fetchone()
            if row and row[0] not in moving:
                clashes.setdefault(new, list(olds)).append("(already in the catalog)")
    finally:
        con.close()
    return clashes


def suspect_mappings(db: Path) -> list[tuple[str, str]]:
    """Rows whose recorded Telegram id may have been attributed by guesswork.

    The old recovery path accepted the single nearest candidate by perceptual
    hash, and that hash is grayscale: an opaque red square and an opaque blue
    square are zero apart. Any row that has a recorded file_unique_id AND a
    perceptual hash close to another row's is one the old rule could have
    confused. This does not prove a mistake -- it lists what a human should
    look at in the roster gallery.
    """
    con = sqlite3.connect(db)
    try:
        items = con.execute(
            "SELECT i.content_key, i.format, i.phash, "
            "       (SELECT COUNT(*) FROM seen_files s WHERE s.content_key=i.content_key) "
            "FROM items i WHERE i.phash IS NOT NULL").fetchall()
    finally:
        con.close()
    suspects = []
    for key, fmt, phash, seen in items:
        if not seen:
            continue
        near = [k for k, f, h, _s in items
                if k != key and f == fmt and identity.hamming(h, phash) <= 2]
        if near:
            suspects.append((key, f"{len(near)} look-alike(s) in the same format"))
    return suspects


def report(data_dir: Path) -> int:
    db = _catalog(data_dir)
    print(f"catalog: {db}")
    started = time.time()
    changed, undecodable, missing = recompute_video_keys(db)
    print(f"\nVIDEO IDENTITY ({time.time() - started:.0f}s of decoding)")
    print(f"  keys that would change : {len(changed)}")
    print(f"  undecodable files      : {len(undecodable)}")
    print(f"  media missing on disk  : {len(missing)}")
    for old, new, name in changed[:10]:
        print(f"    {old} -> {new}   ({name})")
    if len(changed) > 10:
        print(f"    ... and {len(changed) - 10} more")
    for line in undecodable:
        print(f"    UNDECODABLE {line}")

    clashes = collisions(db, changed)
    print(f"  collisions             : {len(clashes)}")
    for new, olds in clashes.items():
        print(f"    {new} <- {', '.join(olds)}")

    suspects = suspect_mappings(db)
    print(f"\nSUSPECT MAPPINGS: {len(suspects)}")
    print("  Rows with a recorded Telegram id AND a look-alike in the catalog.")
    print("  The old recovery rule compared grayscale structure only, so these")
    print("  are the rows it COULD have confused. Check them in the roster")
    print("  gallery; nothing here is evidence of an actual mistake.")
    for key, why in suspects[:10]:
        print(f"    {key}  ({why})")
    if len(suspects) > 10:
        print(f"    ... and {len(suspects) - 10} more")

    if changed and not clashes:
        print("\nTo move the video rows onto their corrected keys:")
        print("  python scripts/identity_repair.py migrate-video-keys --apply")
    elif clashes:
        print("\nRefusing to offer a migration while collisions exist: two rows "
              "would become one identity and one of them would lose its media.")
    return EXIT_STALE if (changed or clashes) else EXIT_OK


def migrate(data_dir: Path, apply: bool) -> int:
    db = _catalog(data_dir)
    changed, undecodable, missing = recompute_video_keys(db)
    if undecodable:
        print(f"{len(undecodable)} video file(s) could not be decoded. A key "
              f"cannot be corrected from a file we cannot read, and migrating "
              f"the rest would leave the catalog half-converted.")
        for line in undecodable:
            print(f"  {line}")
        return EXIT_FAILED
    clashes = collisions(db, changed)
    if clashes:
        print(f"{len(clashes)} collision(s): two rows would claim one key. "
              f"Refusing -- merging them deletes media.")
        for new, olds in clashes.items():
            print(f"  {new} <- {', '.join(olds)}")
        return EXIT_FAILED
    if not changed:
        print("Every video key already matches what the current decoder computes.")
        return EXIT_OK
    if not apply:
        print(f"{len(changed)} video key(s) would move. Re-run with --apply.")
        return EXIT_STALE

    backup = db.with_name(f"catalog.before-video-identity-{int(time.time())}.db")
    shutil.copy2(db, backup)
    print(f"backup: {backup}")

    con = sqlite3.connect(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        for old, new, _name in changed:
            for table, column in KEY_REFERENCES:
                con.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?",
                            (new, old))
        con.commit()
    except Exception:
        con.rollback()
        con.close()
        print(f"nothing was changed; the backup at {backup} is identical")
        raise
    finally:
        con.close()

    print(f"{len(changed)} video row(s) moved onto their corrected keys.")
    print("Archived filenames embed the old key, so re-sync the archive next:")
    print("  .venv\\Scripts\\python.exe pack_archive.py --sync")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    setup_logging("identity_repair")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("report", "migrate-video-keys"),
                    nargs="?", default="report")
    ap.add_argument("--data-dir", default="collection")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it, migrate only reports.")
    args = ap.parse_args(argv)
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    if args.command == "report":
        return report(data_dir)
    return migrate(data_dir, args.apply)


if __name__ == "__main__":
    raise SystemExit(record_exit_code(main()))
