"""Typed GitHub issue, pull-request, and Check Run boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..config import RepositoryConfig
from .issue_mutation import RemoteComment


@dataclass(frozen=True, slots=True)
class GitHubCheckAnnotation:
    path: str
    start_line: int | None
    end_line: int | None
    level: str
    title: str
    message: str
    raw_details: str


@dataclass(frozen=True, slots=True)
class GitHubActionsStep:
    number: int | None
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True, slots=True)
class GitHubActionsJob:
    job_id: int
    run_id: int | None
    attempt: int | None
    name: str
    status: str
    conclusion: str | None
    source_url: str
    steps: tuple[GitHubActionsStep, ...]


@dataclass(frozen=True, slots=True)
class GitHubCheckRun:
    check_run_id: int
    name: str
    head_sha: str
    status: str
    conclusion: str | None
    details_url: str
    source_url: str
    started_at: str
    completed_at: str
    app_name: str
    output_title: str
    output_summary: str
    output_text: str
    annotations_count: int
    run_id: int | None
    job_id: int | None


@dataclass(frozen=True, slots=True)
class GitHubJobLog:
    text: str
    truncated: bool


class PullRequestGateway(Protocol):
    def auth_status(self, cwd: Path) -> tuple[bool, str]: ...

    def issue_read(self, cwd: Path, issue_number: int) -> dict[str, Any]: ...

    def pr_read(self, cwd: Path, pr_number: int) -> dict[str, Any]: ...

    def find_pr(self, cwd: Path, branch: str) -> dict[str, Any] | None: ...

    def create_draft(
        self,
        cwd: Path,
        repo: RepositoryConfig,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> str: ...

    def update(
        self, cwd: Path, branch: str, *, title: str | None, body: str | None
    ) -> dict[str, Any]: ...

    def pr_comments(
        self, cwd: Path, pr_number: int, *, max_comments: int
    ) -> tuple[tuple[RemoteComment, ...], bool]: ...

    def pr_comment(self, cwd: Path, pr_number: int, body: str) -> RemoteComment: ...

    def pr_review_reply(self, cwd: Path, review_comment_id: int, body: str) -> RemoteComment: ...

    def status(self, cwd: Path, branch: str) -> dict[str, Any]: ...

    def checks(self, cwd: Path, branch: str, *, required_only: bool) -> list[dict[str, Any]]: ...

    def check_run(self, cwd: Path, check_run_id: int) -> GitHubCheckRun: ...

    def check_annotations(
        self,
        cwd: Path,
        check_run_id: int,
        *,
        max_annotations: int,
    ) -> tuple[list[GitHubCheckAnnotation], bool]: ...

    def actions_job(self, cwd: Path, job_id: int) -> GitHubActionsJob: ...

    def actions_job_log(self, cwd: Path, job_id: int, *, max_chars: int) -> GitHubJobLog: ...
