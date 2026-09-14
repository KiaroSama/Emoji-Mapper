"""Verified dedup decisions and non-clobbering collector media storage."""

from __future__ import annotations

import os
import hashlib
import shutil
import tempfile
from pathlib import Path

from emojikit import identity
from emojikit.errors import MediaError
from emojikit.packstate import write_json_atomic


def equivalent_files(a: Path, b: Path, fmt: str) -> bool | None:
    """Exact bytes prove equality; otherwise examine the actual content."""
    try:
        if a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes():
            return True
    except OSError:
        return None
    return identity.same_image(a, b, fmt)


def catalog_identity(cat, key: str, fmt: str, path: Path,
                     phash: int | None) -> tuple[str, bool]:
    """Return the verified existing identity or a safe unused insertion key.

    Neither a sampled key nor a dHash is equality evidence. Check every
    nominated rival before merging labels/IDs or removing incoming media.
    """
    prefix, _, digest = key.partition(":")
    lookup = f"{prefix}:{digest.rsplit(':', 1)[-1]}"
    matches = []
    unknown = False
    for item in cat.all_items(fmt):
        candidate_lookup = item.content_key
        if candidate_lookup.count(":") == 2:
            candidate_lookup = f"{prefix}:{candidate_lookup.rsplit(':', 1)[-1]}"
        exact = item.content_key == key or candidate_lookup == lookup
        near = (cat.phash_threshold >= 0 and phash is not None
                and item.phash is not None
                and identity.hamming(item.phash, phash) <= cat.phash_threshold)
        if not exact and not near:
            continue
        same = equivalent_files(Path(item.file_path), path, fmt)
        if same is True:
            matches.append(item.content_key)
        elif same is None:
            unknown = True
    if unknown or len(matches) > 1:
        raise MediaError("catalog identity is undecidable or ambiguous; incoming media was retained")
    if matches:
        return matches[0], False
    if cat.get(key) is not None:
        key = identity.collision_key(path, lookup)
        if cat.get(key) is not None:
            raise MediaError("collision identity is already occupied; incoming media was retained")
    return key, True


def _retain_refused(source: Path, destination: Path, fmt: str, key: str,
                    provenance: dict[str, str]) -> Path:
    """Keep complete refused bytes outside the CLI's disposable scratch tree.

    A hard link publishes the complete file atomically. Identical retries reuse
    one entry; an unrelated occupied quarantine name is never overwritten.
    """
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    directory = destination.parent / "quarantine"
    directory.mkdir(parents=True, exist_ok=True)
    retained = directory / (digest + destination.suffix)
    try:
        os.link(source, retained)
    except FileExistsError:
        if retained.is_symlink() or retained.read_bytes() != payload:
            retained = None
    except OSError:
        retained = None
    if retained is None:
        # This target is ours, created exclusively. Renaming into it avoids a
        # second partial-copy window on filesystems without hard-link support.
        fd, name = tempfile.mkstemp(prefix=digest + "-", suffix=destination.suffix, dir=directory)
        os.close(fd)
        retained = Path(name)
        try:
            source.replace(retained)
        except BaseException:
            retained.unlink(missing_ok=True)
            raise
    record = retained.with_suffix(".json")
    if not record.exists():
        write_json_atomic(record, {"reason": "media-destination-collision", "format": fmt,
                                   "content_key": key, "sha256": digest, "size": len(payload),
                                   "destination": destination.name, "provenance": provenance})
    source.unlink(missing_ok=True)
    return retained


def store_media(source: Path, destination: Path, fmt: str, key: str, *,
                provenance: dict[str, str] | None = None) -> Path:
    """Keep incoming bytes unless an existing file is verified equivalent.

    Exclusive creation never overwrites an existing destination, including an
    untracked orphan. A collision gets its own name before Catalog.add runs.
    The caller holds the catalog writer lease across storage and row insertion.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    alternatives = [destination, destination.with_name(
        identity.collision_key(source, key).replace(":", "_") + destination.suffix)]
    for target in alternatives:
        try:
            output = target.open("xb")
        except FileExistsError:
            if equivalent_files(source, target, fmt) is True:
                if source.resolve() != target.resolve():
                    source.unlink()
                return target
            continue
        try:
            with output, source.open("rb") as incoming:
                shutil.copyfileobj(incoming, output)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        source.unlink()
        return target
    retained = _retain_refused(source, destination, fmt, key, provenance or {})
    raise MediaError(f"media destination collision could not be resolved; incoming media retained at {retained}")
