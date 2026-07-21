"""Snapshot-bound identities for deterministic verification-failure reuse."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..config import ProfileConfig
from ..domain.diagnostics import DiagnosticProfileConfig
from ..domain.retry_guidance import FailureReuseBinding
from ..domain.verification_steps import VerificationStep


def identity_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def config_identity(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _file_state(root: Path, relative_path: str) -> dict[str, object]:
    candidate = root / relative_path
    if candidate.is_symlink():
        return {"path": relative_path, "state": "symlink"}
    if not candidate.exists():
        return {"path": relative_path, "state": "missing"}
    if not candidate.is_file():
        return {"path": relative_path, "state": "non_regular"}
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return {"path": relative_path, "state": "unreadable"}
    return {"path": relative_path, "state": "file", "sha256": digest.hexdigest()}


def command_source_identity(root: Path, paths: tuple[str, ...]) -> str:
    return identity_digest([_file_state(root, path) for path in sorted(set(paths))])


def profile_target_identity(
    profile: ProfileConfig,
    steps: tuple[VerificationStep, ...],
) -> str:
    return identity_digest(
        {
            "name": profile.name,
            "description": profile.description,
            "verification": profile.verification,
            "timeout_seconds": profile.timeout_seconds,
            "working_directory": profile.working_directory,
            "command_source_paths": list(profile.command_source_paths),
            "baseline_policy": profile.baseline_policy.value,
            "steps": [
                {
                    "id": step.step_id,
                    "kind": step.kind.value,
                    "command": list(step.command),
                }
                for step in steps
            ],
        }
    )


def diagnostic_target_identity(
    profile: DiagnosticProfileConfig,
    *,
    argv: tuple[str, ...],
    resolved_values: dict[str, tuple[str, ...]],
    intent: str,
    expectation: str,
    expected_failure_class: str | None,
) -> str:
    return identity_digest(
        {
            "diagnostic_id": profile.diagnostic_id,
            "summary": profile.summary,
            "argv": list(argv),
            "resolved_values": {
                name: list(values) for name, values in sorted(resolved_values.items())
            },
            "working_directory": profile.working_directory,
            "timeout_seconds": profile.timeout_seconds,
            "network_policy": profile.network_policy.value,
            "mutability": profile.mutability.value,
            "parser": profile.parser.value,
            "output_limit": profile.output_limit,
            "artifact_paths": list(profile.artifact_paths),
            "intent": intent,
            "expectation": expectation,
            "expected_failure_class": expected_failure_class,
        }
    )


def diagnostic_rerun_target_identity(
    profile: DiagnosticProfileConfig,
    *,
    intent: str,
    expectation: str,
    expected_failure_class: str | None,
) -> str:
    """Identity of the reviewed diagnostic contract excluding runtime selector values."""

    return diagnostic_target_identity(
        profile,
        argv=profile.argv_template,
        resolved_values={},
        intent=intent,
        expectation=expectation,
        expected_failure_class=expected_failure_class,
    )


def failure_reuse_binding(
    *,
    fingerprint: str,
    target_identity: str,
    command_source_identity_value: str,
    config_identity_value: str | None,
    environment_identity: str | None,
) -> FailureReuseBinding | None:
    if config_identity_value is None or environment_identity is None:
        return None
    return FailureReuseBinding(
        fingerprint=fingerprint,
        target_identity=target_identity,
        command_source_identity=command_source_identity_value,
        config_identity=config_identity_value,
        environment_identity=environment_identity,
    )


def bounded_evidence(value: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy so callers cannot mutate cached evidence after persistence."""

    return dict(value)
