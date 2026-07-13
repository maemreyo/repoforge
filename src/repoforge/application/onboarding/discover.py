"""Config-aware classification of raw discovered Git identities."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, replace
from pathlib import Path

from ...domain.onboarding import (
    DiscoveryCandidate,
    DiscoveryExclusion,
    ExclusionReason,
    detect_duplicate_repo_ids,
)
from ...domain.policy import slugify
from ...ports.repository_discovery import DiscoveryRequest, RepositoryDiscovery


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    eligible: tuple[DiscoveryCandidate, ...]
    exclusions: tuple[DiscoveryExclusion, ...]
    duplicates: tuple[tuple[str, tuple[str, ...]], ...]


class OnboardingDiscoveryService:
    def __init__(self, discovery: RepositoryDiscovery):
        self._discovery = discovery

    @staticmethod
    def _generated(path: str) -> bool:
        normalized = Path(path).as_posix()
        return "/.claude/worktrees/" in normalized or "/.worktrees/" in normalized

    def discover(
        self,
        request: DiscoveryRequest,
        *,
        enrolled: tuple[tuple[str, str], ...] = (),
    ) -> DiscoveryResult:
        identities = self._discovery.discover(request)
        enrolled_paths = {
            str(Path(path).expanduser().resolve()): repo_id for repo_id, path in enrolled
        }
        candidates: list[DiscoveryCandidate] = []
        exclusions: list[DiscoveryExclusion] = []
        for identity in identities:
            path = str(Path(identity.path).expanduser().resolve())
            repo_id = slugify(Path(path).name)
            if identity.error:
                reason = (
                    ExclusionReason.UNREADABLE_PATH
                    if identity.error == "unreadable_path"
                    else ExclusionReason.INVALID_GIT_REPOSITORY
                )
                exclusions.append(DiscoveryExclusion(path, reason, identity.error, repo_id))
            elif identity.bare:
                exclusions.append(
                    DiscoveryExclusion(
                        path,
                        ExclusionReason.BARE_REPOSITORY,
                        "Bare repositories have no writable working tree",
                        repo_id,
                    )
                )
            elif not identity.primary:
                exclusions.append(
                    DiscoveryExclusion(
                        path,
                        ExclusionReason.LINKED_WORKTREE,
                        "RepoForge creates its own isolated worktrees",
                        repo_id,
                    )
                )
            elif self._generated(path):
                exclusions.append(
                    DiscoveryExclusion(
                        path,
                        ExclusionReason.GENERATED_WORKTREE_DIRECTORY,
                        "Generated worktree directory",
                        repo_id,
                    )
                )
            elif path in enrolled_paths:
                exclusions.append(
                    DiscoveryExclusion(
                        path,
                        ExclusionReason.ALREADY_ENROLLED,
                        "Repository path is already configured",
                        enrolled_paths[path],
                    )
                )
            elif any(
                fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern)
                for pattern in request.exclude
            ):
                exclusions.append(
                    DiscoveryExclusion(
                        path, ExclusionReason.OPERATOR_EXCLUDED, "Matched --exclude", repo_id
                    )
                )
            else:
                candidates.append(
                    DiscoveryCandidate(replace(identity, path=path, worktree_root=path), repo_id)
                )
        # Preserve real nested repositories while recording the nearest eligible parent.
        ordered = sorted(
            candidates, key=lambda item: (len(Path(item.identity.path).parts), item.identity.path)
        )
        with_parents: list[DiscoveryCandidate] = []
        for item in ordered:
            parents = [
                parent
                for parent in with_parents
                if parent.identity.git_common_dir != item.identity.git_common_dir
                and Path(item.identity.path).is_relative_to(Path(parent.identity.path))
            ]
            parent_id = (
                max(parents, key=lambda value: len(Path(value.identity.path).parts)).repo_id
                if parents
                else None
            )
            with_parents.append(replace(item, parent_repo_id=parent_id))
        duplicates = detect_duplicate_repo_ids(tuple(with_parents))
        return DiscoveryResult(
            tuple(sorted(with_parents, key=lambda item: (item.repo_id, item.identity.path))),
            tuple(sorted(exclusions, key=lambda item: (item.path, item.reason.value))),
            tuple(sorted(duplicates.items())),
        )
