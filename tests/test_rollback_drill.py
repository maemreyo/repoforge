from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from repoforge.interfaces.mcp.server import tool_surface_hash

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rollback_drill.py"
_SPEC = importlib.util.spec_from_file_location("repoforge_rollback_drill", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
run_rollback_drill = cast(Callable[..., dict[str, Any]], _MODULE.run_rollback_drill)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_rollback_drill_exercises_both_identities_without_mutating_persistent_state(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "persistent-state"
    state_root.mkdir()
    (state_root / "runtime.json").write_text(
        json.dumps({"active_generation": 7, "workspaces": ["ws-1"]}) + "\n",
        encoding="utf-8",
    )
    nested = state_root / "operations"
    nested.mkdir()
    (nested / "op.json").write_text(
        json.dumps({"operation_id": "op-1", "state": "succeeded"}) + "\n",
        encoding="utf-8",
    )
    before = _snapshot(state_root)
    legacy_surface = "1" * 64

    result = run_rollback_drill(
        state_root,
        v1_surface_hash=legacy_surface,
        v2_surface_hash=tool_surface_hash(),
    )

    assert result["status"] == "passed"
    assert result["persistent_state_mutated"] is False
    assert result["state_digest_before"] == result["state_digest_after"]
    assert _snapshot(state_root) == before
    assert result["transitions"] == [
        {
            "from_identity": "forge_v2",
            "to_identity": "forge_v1",
            "surface_hash": legacy_surface,
            "health": "healthy",
            "rediscovery_required": True,
            "rediscovery_cleared": True,
            "stuck_rediscovery": False,
        },
        {
            "from_identity": "forge_v1",
            "to_identity": "forge_v2",
            "surface_hash": tool_surface_hash(),
            "health": "healthy",
            "rediscovery_required": True,
            "rediscovery_cleared": True,
            "stuck_rediscovery": False,
        },
    ]


def test_rollback_drill_is_deterministic_for_the_same_persistent_snapshot(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "accepted.json").write_text('{"generation":3}\n', encoding="utf-8")

    first = run_rollback_drill(
        state_root,
        v1_surface_hash="a" * 64,
        v2_surface_hash="b" * 64,
    )
    second = run_rollback_drill(
        state_root,
        v1_surface_hash="a" * 64,
        v2_surface_hash="b" * 64,
    )

    assert first == second
