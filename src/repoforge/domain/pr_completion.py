"""Typed pull-request issue completion intent and managed-body rendering."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from .errors import ErrorCode, RepoForgeError

PR_ISSUE_DISPOSITIONS = frozenset({"closes", "advances", "supersedes", "relates"})
_START_MARKER = "<!-- repoforge-pr-issue-dispositions:v1 -->"
_END_MARKER = "<!-- repoforge-pr-issue-dispositions:v1 :end -->"
_MANAGED_BLOCK = re.compile(
    rf"\n?{re.escape(_START_MARKER)}.*?{re.escape(_END_MARKER)}\n?",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class PrIssueDispositionRequest:
    issue_number: int
    disposition: str
    acceptance_evidence_ref: str


@dataclass(frozen=True, slots=True)
class PrIssueIntent:
    issue_number: int
    disposition: str
    acceptance_evidence_ref: str
    snapshot_title: str
    snapshot_state: str
    snapshot_url: str


def _blocked(message: str, *, details: dict[str, object]) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.PROPOSAL_BLOCKED,
        retryable=False,
        details=details,
        safe_next_action=(
            "Provide one explicit closes, advances, supersedes, or relates disposition for every "
            "workspace issue ID, bound to acceptance evidence, then retry the exact PR write."
        ),
        unchanged_state=("No pull-request write was attempted.",),
    )


def workspace_issue_numbers(raw: object) -> tuple[int, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise _blocked(
            "Workspace issue IDs are malformed and cannot be bound to PR completion intent",
            details={"workspace_issue_ids_status": "malformed"},
        )
    numbers: list[int] = []
    for value in raw:
        if not isinstance(value, str):
            raise _blocked(
                "Workspace issue IDs are malformed and cannot be bound to PR completion intent",
                details={"workspace_issue_ids_status": "malformed"},
            )
        normalized = value.strip().removeprefix("#")
        if not normalized.isdecimal() or int(normalized) <= 0:
            raise _blocked(
                "Workspace issue IDs must be GitHub issue numbers before PR completion can be claimed",
                details={"invalid_issue_id": value},
            )
        numbers.append(int(normalized))
    if len(numbers) != len(set(numbers)):
        raise _blocked(
            "Workspace issue IDs contain duplicates",
            details={"workspace_issue_numbers": sorted(numbers)},
        )
    return tuple(sorted(numbers))


def normalize_disposition_requests(
    raw: Sequence[Mapping[str, object]] | None,
) -> tuple[PrIssueDispositionRequest, ...]:
    if not raw:
        return ()
    requests: list[PrIssueDispositionRequest] = []
    for item in raw:
        issue_number = item.get("issue_number")
        disposition = item.get("disposition")
        evidence_ref = item.get("acceptance_evidence_ref")
        if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number <= 0:
            raise _blocked(
                "PR issue disposition has an invalid issue number",
                details={"issue_number": issue_number},
            )
        normalized_disposition = disposition.value if hasattr(disposition, "value") else disposition
        if normalized_disposition not in PR_ISSUE_DISPOSITIONS:
            raise _blocked(
                "PR issue disposition is unsupported",
                details={"issue_number": issue_number, "disposition": normalized_disposition},
            )
        if (
            not isinstance(evidence_ref, str)
            or not evidence_ref.strip()
            or len(evidence_ref) > 1000
        ):
            raise _blocked(
                "PR issue disposition requires bounded acceptance evidence",
                details={"issue_number": issue_number},
            )
        requests.append(
            PrIssueDispositionRequest(
                issue_number=issue_number,
                disposition=str(normalized_disposition),
                acceptance_evidence_ref=evidence_ref.strip(),
            )
        )
    numbers = [item.issue_number for item in requests]
    if len(numbers) != len(set(numbers)):
        duplicates = sorted({number for number in numbers if numbers.count(number) > 1})
        raise _blocked(
            "Each issue may have only one PR completion disposition",
            details={"duplicate_issue_numbers": duplicates},
        )
    return tuple(sorted(requests, key=lambda item: item.issue_number))


def require_complete_dispositions(
    workspace_numbers: tuple[int, ...],
    requests: tuple[PrIssueDispositionRequest, ...],
) -> None:
    expected = set(workspace_numbers)
    actual = {item.issue_number for item in requests}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise _blocked(
            "Every workspace issue requires one explicit disposition before PR publication",
            details={
                "missing_issue_numbers": missing,
                "unexpected_issue_numbers": unexpected,
                "workspace_issue_numbers": sorted(expected),
            },
        )


def intent_payload(intent: PrIssueIntent) -> dict[str, object]:
    return asdict(intent)


def intent_from_payload(raw: object) -> PrIssueIntent:
    if not isinstance(raw, dict):
        raise _blocked(
            "Stored PR completion intent is malformed",
            details={"stored_intent_status": "malformed"},
        )
    try:
        intent = PrIssueIntent(
            issue_number=int(raw["issue_number"]),
            disposition=str(raw["disposition"]),
            acceptance_evidence_ref=str(raw["acceptance_evidence_ref"]),
            snapshot_title=str(raw["snapshot_title"]),
            snapshot_state=str(raw["snapshot_state"]),
            snapshot_url=str(raw["snapshot_url"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _blocked(
            "Stored PR completion intent is malformed",
            details={"stored_intent_status": "malformed"},
        ) from exc
    normalize_disposition_requests(
        (
            {
                "issue_number": intent.issue_number,
                "disposition": intent.disposition,
                "acceptance_evidence_ref": intent.acceptance_evidence_ref,
            },
        )
    )
    if not intent.snapshot_title or not intent.snapshot_url:
        raise _blocked(
            "Stored PR completion snapshot is incomplete",
            details={"issue_number": intent.issue_number},
        )
    return intent


def completion_evidence(intents: tuple[PrIssueIntent, ...]) -> dict[str, object] | None:
    if not intents:
        return None
    grouped = {
        disposition: [
            intent.issue_number for intent in intents if intent.disposition == disposition
        ]
        for disposition in sorted(PR_ISSUE_DISPOSITIONS)
    }
    return {
        "intent_complete": True,
        **grouped,
        "snapshots": [
            {
                "issue_number": intent.issue_number,
                "title": intent.snapshot_title,
                "state": intent.snapshot_state,
                "url": intent.snapshot_url,
                "acceptance_evidence_ref": intent.acceptance_evidence_ref,
            }
            for intent in intents
        ],
    }


def render_pr_issue_intent(body: str, intents: tuple[PrIssueIntent, ...]) -> str:
    cleaned = _MANAGED_BLOCK.sub("\n", body).rstrip()
    if not intents:
        return cleaned
    labels = {
        "closes": "Closes",
        "advances": "Advances",
        "supersedes": "Supersedes",
        "relates": "Relates to",
    }
    lines = [
        _START_MARKER,
        "## Issue completion intent",
        "",
        "This section is managed by RepoForge and bound to reviewed issue snapshots.",
        "",
    ]
    for intent in intents:
        title = " ".join(intent.snapshot_title.split())
        lines.append(
            f"- {labels[intent.disposition]} #{intent.issue_number} — {title}; "
            f"snapshot `{intent.snapshot_state}`; evidence `{intent.acceptance_evidence_ref}`"
        )
    lines.extend(["", _END_MARKER])
    section = "\n".join(lines)
    return f"{cleaned}\n\n{section}\n" if cleaned else section + "\n"


__all__ = [
    "PR_ISSUE_DISPOSITIONS",
    "PrIssueDispositionRequest",
    "PrIssueIntent",
    "completion_evidence",
    "intent_from_payload",
    "intent_payload",
    "normalize_disposition_requests",
    "render_pr_issue_intent",
    "require_complete_dispositions",
    "workspace_issue_numbers",
]
