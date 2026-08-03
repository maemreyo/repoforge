from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def _load_module() -> Any:
    script = Path(__file__).parents[1] / "scripts/verification_artifact.py"
    spec = importlib.util.spec_from_file_location("repoforge_verification_artifact", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_artifact_is_bounded_and_canonical(tmp_path: Path) -> None:
    artifact_module = _load_module()
    artifact = artifact_module.VerificationArtifact(
        schema_version=1,
        intent="affected",
        head_sha="a" * 40,
        selected_count=2,
        escalated=False,
        escalation_reason=None,
        lanes=(artifact_module.LaneTiming("parallel", 2, 125.5, 0),),
    )
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    artifact_module.write_artifact(first, artifact)
    artifact_module.write_artifact(second, artifact)

    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["selected_count"] == 2
    assert payload["lanes"] == [
        {"duration_ms": 125.5, "file_count": 2, "name": "parallel", "returncode": 0}
    ]
    assert first.read_bytes() == second.read_bytes()
    assert first.stat().st_size < 64_000


def test_artifact_bounds_untrusted_reason_text(tmp_path: Path) -> None:
    artifact_module = _load_module()
    target = tmp_path / "bounded.json"
    artifact_module.write_artifact(
        target,
        artifact_module.VerificationArtifact(
            schema_version=1,
            intent="affected",
            head_sha="b" * 40,
            selected_count=1,
            escalated=True,
            escalation_reason="x" * 100_000,
            lanes=(),
        ),
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert len(payload["escalation_reason"]) == 2_000
    assert target.stat().st_size < 64_000
