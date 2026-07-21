"""Exact pull-request remote-version tokens for optimistic write locking."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .errors import ErrorCode, RepoForgeError

PR_REMOTE_VERSION_SCHEMA_VERSION = 2
_TOKEN_PREFIX = f"prv{PR_REMOTE_VERSION_SCHEMA_VERSION}:"
_STABILITY_PREFIX = f"prm{PR_REMOTE_VERSION_SCHEMA_VERSION}:"
_SHA = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
_REQUIRED_FIELDS = (
    "number",
    "title",
    "state",
    "isDraft",
    "mergeable",
    "reviewDecision",
    "headRefOid",
    "updatedAt",
    "comments",
    "reviews",
    "statusCheckRollup",
)
_COLLECTION_FIELDS = ("comments", "reviews", "statusCheckRollup")


@dataclass(frozen=True, slots=True)
class PrRemoteVersion:
    """One complete provider snapshot reduced to a copy-safe opaque token."""

    token: str
    repo_id: str
    pr_number: int
    head_sha: str
    updated_at: str
    stability_version: str
    coverage: tuple[str, ...]


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _missing_coverage(payload: dict[str, Any]) -> tuple[str, ...]:
    missing = [field for field in _REQUIRED_FIELDS if field not in payload]
    for field in _COLLECTION_FIELDS:
        if field in payload and not isinstance(payload[field], list):
            missing.append(f"{field}:invalid")
        if payload.get(f"{field}_truncated") is True:
            missing.append(f"{field}:truncated")
    number = payload.get("number")
    if "number" in payload and (
        not isinstance(number, int) or isinstance(number, bool) or number <= 0
    ):
        missing.append("number:invalid")
    head_sha = payload.get("headRefOid")
    if "headRefOid" in payload and (
        not isinstance(head_sha, str) or _SHA.fullmatch(head_sha.lower()) is None
    ):
        missing.append("headRefOid:invalid")
    for field in ("title", "state", "updatedAt"):
        value = payload.get(field)
        if field in payload and (not isinstance(value, str) or not value):
            missing.append(f"{field}:invalid")
    if "isDraft" in payload and not isinstance(payload.get("isDraft"), bool):
        missing.append("isDraft:invalid")
    return tuple(sorted(set(missing)))


def build_pr_remote_version(payload: dict[str, Any], *, repo_id: str) -> PrRemoteVersion:
    """Build a complete version token or fail closed on partial provider evidence."""

    missing = _missing_coverage(payload)
    if missing:
        raise RepoForgeError(
            "PR_REMOTE_VERSION_INCOMPLETE: GitHub did not return complete PR version evidence",
            code=ErrorCode.PR_REMOTE_VERSION_INCOMPLETE,
            retryable=False,
            safe_next_action=(
                "Refresh workspace_pr_evidence after complete GitHub overview coverage is available; "
                "do not authorize a PR write from this partial snapshot."
            ),
            unchanged_state=("No pull-request write was attempted.",),
            details={
                "field": "remote_version",
                "actual": ",".join(missing)[:1000],
                "missing_coverage": list(missing),
                "result_reference": "workspace_pr_evidence:overview",
            },
        )

    number = payload["number"]
    head_sha = payload["headRefOid"].lower()
    updated_at = payload["updatedAt"]
    coverage = tuple(sorted(_REQUIRED_FIELDS))
    stability_projection = {
        "schema_version": PR_REMOTE_VERSION_SCHEMA_VERSION,
        "provider": "github.status",
        "coverage": tuple(field for field in coverage if field != "statusCheckRollup"),
        "repo_id": repo_id,
        "pr_number": number,
        "head_sha": head_sha,
        "updated_at": updated_at,
        "metadata": {
            "title": payload["title"],
            "state": payload["state"],
            "is_draft": payload["isDraft"],
            "mergeable": payload["mergeable"],
            "review_decision": payload["reviewDecision"],
        },
        "comments_digest": _digest(payload["comments"]),
        "reviews_digest": _digest(payload["reviews"]),
    }
    stability_version = _STABILITY_PREFIX + _digest(stability_projection)
    projection = {
        **stability_projection,
        "coverage": coverage,
        "checks_digest": _digest(payload["statusCheckRollup"]),
    }
    token = _TOKEN_PREFIX + _digest(projection)
    return PrRemoteVersion(
        token=token,
        repo_id=repo_id,
        pr_number=number,
        head_sha=head_sha,
        updated_at=updated_at,
        stability_version=stability_version,
        coverage=coverage,
    )


__all__ = [
    "PR_REMOTE_VERSION_SCHEMA_VERSION",
    "PrRemoteVersion",
    "build_pr_remote_version",
]
