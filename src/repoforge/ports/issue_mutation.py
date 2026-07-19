"""Typed GitHub-native issue mutation and reconciliation boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RemoteIssue:
    issue_number: int
    database_id: int
    title: str
    state: str
    body: str
    url: str


@dataclass(frozen=True, slots=True)
class RemoteComment:
    comment_id: int
    body: str
    url: str


class IssueMutationGateway(Protocol):
    def issue_details(self, cwd: Path, issue_number: int) -> RemoteIssue: ...

    def issue_comments(
        self, cwd: Path, issue_number: int, *, max_comments: int
    ) -> tuple[tuple[RemoteComment, ...], bool]: ...

    def recent_issues(
        self, cwd: Path, *, max_issues: int
    ) -> tuple[tuple[RemoteIssue, ...], bool]: ...

    def issue_comment(self, cwd: Path, issue_number: int, body: str) -> RemoteComment: ...

    def set_issue_state(self, cwd: Path, issue_number: int, state: str) -> RemoteIssue: ...

    def create_issue(self, cwd: Path, title: str, body: str) -> RemoteIssue: ...

    def sub_issues(
        self, cwd: Path, issue_number: int, *, max_issues: int
    ) -> tuple[tuple[RemoteIssue, ...], bool]: ...

    def blocked_by(
        self, cwd: Path, issue_number: int, *, max_issues: int
    ) -> tuple[tuple[RemoteIssue, ...], bool]: ...

    def add_sub_issue(self, cwd: Path, issue_number: int, sub_issue_id: int) -> RemoteIssue: ...

    def add_blocked_by(
        self, cwd: Path, issue_number: int, blocker_issue_id: int
    ) -> RemoteIssue: ...
