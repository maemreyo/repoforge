"""Immutable identity token for bounded workspace verification preflight."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .errors import ErrorCode, RepoForgeError

_SHA40 = re.compile(r"^[a-f0-9]{40}$")
_SHA64 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


@dataclass(frozen=True, slots=True)
class SnapshotToken:
    token_id: str
    workspace_id: str
    head_sha: str
    workspace_fingerprint: str
    validity_token: str
    changed_paths: tuple[str, ...]
    config_generation: int
    policy_hash: str
    captured_at: str


def _invalid(message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.STALE_STATE,
        retryable=True,
        safe_next_action="Capture a fresh workspace snapshot and retry verification.",
    )


def new_snapshot_token(
    *,
    workspace_id: str,
    head_sha: str,
    workspace_fingerprint: str,
    validity_token: str,
    changed_paths: tuple[str, ...],
    config_generation: int,
    policy_hash: str,
    captured_at: str,
) -> SnapshotToken:
    """Validate and hash a deterministic verification identity token."""

    if _SAFE_ID.fullmatch(workspace_id) is None:
        raise _invalid("workspace snapshot workspace_id has an invalid format")
    for name, value, pattern in (
        ("head_sha", head_sha, _SHA40),
        ("workspace_fingerprint", workspace_fingerprint, _SHA64),
        ("validity_token", validity_token, _SHA64),
        ("policy_hash", policy_hash, _SHA64),
    ):
        if pattern.fullmatch(value) is None:
            raise _invalid(f"workspace snapshot {name} has an invalid format")
    if config_generation < 0:
        raise _invalid("workspace snapshot config_generation must be non-negative")
    normalized_paths = tuple(sorted(set(changed_paths)))
    if normalized_paths != changed_paths:
        raise _invalid("workspace snapshot changed_paths must be sorted and unique")
    payload = {
        "workspace_id": workspace_id,
        "head_sha": head_sha,
        "workspace_fingerprint": workspace_fingerprint,
        "validity_token": validity_token,
        "changed_paths": list(changed_paths),
        "config_generation": config_generation,
        "policy_hash": policy_hash,
        "captured_at": captured_at,
    }
    token_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SnapshotToken(
        token_id=token_id,
        workspace_id=workspace_id,
        head_sha=head_sha,
        workspace_fingerprint=workspace_fingerprint,
        validity_token=validity_token,
        changed_paths=changed_paths,
        config_generation=config_generation,
        policy_hash=policy_hash,
        captured_at=captured_at,
    )
