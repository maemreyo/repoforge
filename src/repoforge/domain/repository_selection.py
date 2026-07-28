"""Deterministic repository-selection policy (#150).

Selection is advisory workflow guidance only: every repository-scoped tool call continues to
enforce the configured repo_id allowlist server-side regardless of this outcome. The policy
never invents a repo_id -- ambiguity always resolves to asking the operator, never to recency,
filesystem order, a default base branch, or model preference. It also never treats a
filesystem path as a repository identity: only repo_id, display_name, and remote are compared.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum

from ..config import RepositoryConfig


class RepositorySelectionOutcome(str, Enum):
    EXACT_MATCH = "exact_match"
    SINGLE_ENROLLED = "single_enrolled"
    INPUT_REQUIRED = "input_required"
    NO_MATCH = "no_match"


@dataclass(frozen=True, slots=True)
class RepositoryCandidate:
    repo_id: str
    display_name: str

    def as_dict(self) -> dict[str, str]:
        return {"repo_id": self.repo_id, "display_name": self.display_name}


@dataclass(frozen=True, slots=True)
class RepositorySelection:
    outcome: RepositorySelectionOutcome
    repo_id: str | None
    candidates: tuple[RepositoryCandidate, ...]
    guidance: str

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "repo_id": self.repo_id,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "guidance": self.guidance,
        }


@dataclass(frozen=True, slots=True)
class RepositorySelectionPin:
    """One session-local selection bound to reviewed capability state."""

    repo_selection_id: str
    repo_id: str
    selection_generation: int
    capability_digest: str
    expires_at_epoch: float

    def is_expired(self, *, now_epoch: float) -> bool:
        return now_epoch >= self.expires_at_epoch

    def as_public_dict(self) -> dict[str, object]:
        expires_at = datetime.fromtimestamp(self.expires_at_epoch, tz=timezone.utc)
        return {
            "repo_selection_id": self.repo_selection_id,
            "repo_id": self.repo_id,
            "selection_generation": self.selection_generation,
            "capability_digest": self.capability_digest,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }


def repository_capability_digest(repo: RepositoryConfig) -> str:
    """Hash the selected repository's exact reviewed capability projection."""

    projection = json.dumps(
        asdict(repo),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(projection).hexdigest()


_NO_MATCH_GUIDANCE = (
    "No repository is enrolled. Do not invent a repo_id; tell the operator to enroll one "
    "(rf onboard) before proceeding."
)
_INPUT_REQUIRED_GUIDANCE = (
    "More than one candidate remains. Ask the user to choose from candidates; never pick by "
    "recency, filesystem order, default base branch, or model preference."
)


def _candidates_from(repositories: tuple[RepositoryConfig, ...]) -> tuple[RepositoryCandidate, ...]:
    ordered = sorted(repositories, key=lambda repo: repo.repo_id)
    return tuple(
        RepositoryCandidate(repo_id=repo.repo_id, display_name=repo.display_name or repo.repo_id)
        for repo in ordered
    )


def _exact_match(
    repo: RepositoryConfig, candidates: tuple[RepositoryCandidate, ...]
) -> RepositorySelection:
    return RepositorySelection(
        outcome=RepositorySelectionOutcome.EXACT_MATCH,
        repo_id=repo.repo_id,
        candidates=candidates,
        guidance=f"requested_repo matched {repo.repo_id} uniquely; use it directly.",
    )


def select_repository(
    repositories: tuple[RepositoryConfig, ...],
    *,
    requested_repo: str | None = None,
) -> RepositorySelection:
    """Resolve a bounded, deterministic repository-selection outcome.

    `requested_repo` must be a literal candidate string a caller believes identifies a
    repository (typically the calling model echoing what the user explicitly named) -- never
    free-form natural language to be parsed here; the server never has visibility into a live
    prompt. An exact repo_id match always wins over a display-name or remote match. This
    function is pure and stateless: it never consults or records a "last used" repository.
    """

    candidates = _candidates_from(tuple(repositories))
    if not candidates:
        return RepositorySelection(
            outcome=RepositorySelectionOutcome.NO_MATCH,
            repo_id=None,
            candidates=(),
            guidance=_NO_MATCH_GUIDANCE,
        )

    hint = requested_repo.strip() if requested_repo else ""
    if hint:
        by_id = [repo for repo in repositories if repo.repo_id == hint]
        if len(by_id) == 1:
            return _exact_match(by_id[0], candidates)

        lowered = hint.lower()
        by_alias = [
            repo
            for repo in repositories
            if lowered in {(repo.display_name or "").lower(), repo.remote.lower()}
        ]
        if len(by_alias) == 1:
            return _exact_match(by_alias[0], candidates)
        if len(by_alias) > 1:
            return RepositorySelection(
                outcome=RepositorySelectionOutcome.INPUT_REQUIRED,
                repo_id=None,
                candidates=_candidates_from(tuple(by_alias)),
                guidance=_INPUT_REQUIRED_GUIDANCE,
            )
        # Hint present but matched nothing: fall through to enrollment-count logic below,
        # reporting the full candidate set rather than treating the hint as authoritative.

    if len(candidates) == 1:
        only = candidates[0]
        return RepositorySelection(
            outcome=RepositorySelectionOutcome.SINGLE_ENROLLED,
            repo_id=only.repo_id,
            candidates=candidates,
            guidance=(
                f"Exactly one repository is enrolled ({only.repo_id}); use it without asking "
                "the user to choose."
            ),
        )

    return RepositorySelection(
        outcome=RepositorySelectionOutcome.INPUT_REQUIRED,
        repo_id=None,
        candidates=candidates,
        guidance=_INPUT_REQUIRED_GUIDANCE,
    )
