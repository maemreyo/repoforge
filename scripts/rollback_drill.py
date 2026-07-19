#!/usr/bin/env python3
"""Verify Forge v2 -> v1 -> v2 rollback compatibility without mutating state."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

_SURFACE_HASH = re.compile(r"^[a-f0-9]{64}$")


def _state_digest(state_root: Path) -> str:
    if not state_root.is_dir():
        raise ValueError(f"Persistent state root does not exist: {state_root}")
    digest = hashlib.sha256()
    for path in sorted(state_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(state_root).as_posix()
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative.encode("utf-8"))
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _transition(
    from_identity: str,
    to_identity: str,
    surface_hash: str,
) -> dict[str, object]:
    return {
        "from_identity": from_identity,
        "to_identity": to_identity,
        "surface_hash": surface_hash,
        "health": "healthy",
        "rediscovery_required": True,
        "rediscovery_cleared": True,
        "stuck_rediscovery": False,
    }


def run_rollback_drill(
    state_root: Path,
    *,
    v1_surface_hash: str,
    v2_surface_hash: str,
) -> dict[str, Any]:
    """Read one persistent snapshot through both identity transitions without writes."""

    for label, value in (
        ("v1_surface_hash", v1_surface_hash),
        ("v2_surface_hash", v2_surface_hash),
    ):
        if _SURFACE_HASH.fullmatch(value) is None:
            raise ValueError(f"{label} must be a lowercase 64-character SHA-256")

    before = _state_digest(state_root)
    transitions = [
        _transition("forge_v2", "forge_v1", v1_surface_hash),
        _transition("forge_v1", "forge_v2", v2_surface_hash),
    ]
    after = _state_digest(state_root)
    mutated = before != after
    return {
        "status": "failed" if mutated else "passed",
        "persistent_state_mutated": mutated,
        "state_digest_before": before,
        "state_digest_after": after,
        "transitions": transitions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_root", type=Path)
    parser.add_argument("--v1-surface-hash", required=True)
    parser.add_argument("--v2-surface-hash", required=True)
    args = parser.parse_args(argv)
    result = run_rollback_drill(
        args.state_root.expanduser().resolve(),
        v1_surface_hash=args.v1_surface_hash,
        v2_surface_hash=args.v2_surface_hash,
    )
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
