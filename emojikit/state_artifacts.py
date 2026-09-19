"""One inventory of key-bearing JSON for migration, verification and rollback."""
from __future__ import annotations

import copy
from pathlib import Path

PACK_PLAN_NAME = "pack_plan.json"


def is_state_name(name: str) -> bool:
    """Only sibling application state, never backups or arbitrary paths."""
    return (isinstance(name, str) and "/" not in name and "\\" not in name
            and (name == PACK_PLAN_NAME
                 or (name.startswith("publish_") and name.endswith(".json"))))


def state_files(data_dir: Path) -> list[Path]:
    paths = list(Path(data_dir).glob("publish_*.json"))
    plan = Path(data_dir) / PACK_PLAN_NAME
    if plan.exists() or plan.is_symlink():
        paths.append(plan)
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"unsupported application state path: {path.name}")
    return sorted(paths)


def _plan_refs(doc: dict):
    """Mutable containers and indices of IDs, excluding free-text labels."""
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise ValueError("pack_plan.json has an unsupported schema")
    for field in ("moves", "held"):
        records = doc.get(field)
        if not isinstance(records, list):
            raise ValueError(f"pack_plan.json {field} must be a list")
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("key"), str):
                raise ValueError(f"pack_plan.json {field} contains an invalid key")
            yield record, "key"
    targets = doc.get("targets", [])
    if not isinstance(targets, list):
        raise ValueError("pack_plan.json targets must be a list")
    for pair in targets:
        if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
            raise ValueError("pack_plan.json contains an invalid target")
        yield pair, 0
    for field in ("known", "excluded"):
        keys = doc.get(field, [])
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise ValueError(f"pack_plan.json contains an invalid {field}")
        for i in range(len(keys)):
            yield keys, i


def plan_keys(doc: dict) -> set[str]:
    return {container[index] for container, index in _plan_refs(doc)}


def remap_plan(doc: dict, key_map: dict[str, str]) -> dict:
    fresh = copy.deepcopy(doc)
    for container, index in _plan_refs(fresh):
        container[index] = key_map.get(container[index], container[index])
    return fresh
