"""Bounded read-only GitHub-native ticket graph snapshots."""

from __future__ import annotations

import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from ...config import GitHubTicketGraphConfig, ServerConfig
from ...domain.errors import CommandError
from ...domain.tickets import (
    TicketGraph,
    TicketGraphError,
    TicketGraphSnapshot,
    TicketLiveMetadata,
    TicketNode,
    TicketPriority,
    TicketStatus,
    TicketType,
)
from ...ports.command import CommandExecutor, CommandResult

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_METADATA_LINE = re.compile(r"(?im)^\s*(?:[-*]\s*)?(?P<name>[A-Za-z ]+)\s*:\s*(?P<value>[^\n]+)")
_API_VERSION = "2022-11-28"
_MAX_BODY_CHARS = 200_000
_MAX_COMMENTS = 20
_MAX_COMMENT_CHARS = 20_000


class CommandGitHubTicketGraphGateway:
    """Traverse native sub-issues and dependencies without invoking GitHub writes."""

    def __init__(self, executor: CommandExecutor, server: ServerConfig) -> None:
        self._executor = executor
        self._server = server
        self._output_limit = min(max(server.max_tool_output_chars, 500_000), 5_000_000)

    def _run(self, argv: list[str], *, cwd: Path, output_limit: int | None = None) -> CommandResult:
        return self._executor.run(
            argv,
            cwd=cwd,
            timeout=self._server.default_command_timeout_seconds,
            output_limit=output_limit or self._output_limit,
        )

    @staticmethod
    def _json(result: CommandResult, context: str) -> Any:
        if result.stdout_truncated:
            raise CommandError(f"{context} returned oversized JSON")
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError as exc:
            raise CommandError(f"{context} returned invalid JSON") from exc

    def _object(self, result: CommandResult, context: str) -> dict[str, Any]:
        payload = self._json(result, context)
        if not isinstance(payload, dict):
            raise CommandError(f"{context} returned a non-object JSON value")
        return cast(dict[str, Any], payload)

    def _list(self, result: CommandResult, context: str) -> list[dict[str, Any]]:
        payload = self._json(result, context)
        if not isinstance(payload, list):
            raise CommandError(f"{context} returned a non-list JSON value")
        return [cast(dict[str, Any], item) for item in payload if isinstance(item, dict)]

    def _slug(self, cwd: Path) -> str:
        slug = self._run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=cwd,
            output_limit=512,
        ).stdout.strip()
        if not _REPOSITORY.fullmatch(slug):
            raise CommandError(f"Unexpected GitHub repository name: {slug!r}")
        return slug

    def _api(self, cwd: Path, endpoint: str) -> CommandResult:
        return self._run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                endpoint,
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                f"X-GitHub-Api-Version: {_API_VERSION}",
            ],
            cwd=cwd,
        )

    @staticmethod
    def _labels(raw: object) -> tuple[str, ...]:
        if not isinstance(raw, list):
            return ()
        labels: list[str] = []
        for value in raw:
            name = value.get("name") if isinstance(value, dict) else value
            if isinstance(name, str) and name.strip():
                labels.append(name.strip())
        return tuple(labels)

    @staticmethod
    def _metadata(body: str, labels: tuple[str, ...]) -> dict[str, str]:
        values: dict[str, str] = {}
        for label in labels:
            if ":" in label:
                key, value = label.split(":", 1)
                values[key.strip().casefold()] = value.strip()
        for match in _METADATA_LINE.finditer(body):
            values.setdefault(match.group("name").strip().casefold(), match.group("value").strip())
        return values

    @staticmethod
    def _enum_value(enum_type: type[Any], raw: str | None) -> Any | None:
        if raw is None:
            return None
        normalized = raw.replace("_", " ").replace("-", " ").strip().casefold()
        for value in enum_type:
            candidate = str(value.value).replace("_", " ").replace("-", " ").casefold()
            if candidate == normalized:
                return value
        return None

    def _project_values(
        self,
        cwd: Path,
        slug: str,
        source: GitHubTicketGraphConfig,
        wanted: set[int],
    ) -> tuple[dict[int, dict[str, str]], bool]:
        if source.project_owner is None or source.project_number is None:
            return {}, True
        payload = self._object(
            self._run(
                [
                    "gh",
                    "project",
                    "item-list",
                    str(source.project_number),
                    "--owner",
                    source.project_owner,
                    "--format",
                    "json",
                    "--limit",
                    str(min(max(len(wanted) + 100, 100), 1000)),
                ],
                cwd=cwd,
            ),
            "gh project item-list",
        )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise CommandError("gh project item-list returned no items list")
        result: dict[int, dict[str, str]] = {}
        field_names = (
            source.status_field,
            source.priority_field,
            source.initiative_field,
            source.type_field,
        )
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            content = raw.get("content")
            if not isinstance(content, dict):
                continue
            number = content.get("number")
            if not isinstance(number, int) or isinstance(number, bool):
                continue
            repository = content.get("repository")
            if isinstance(repository, dict):
                repository = repository.get("nameWithOwner")
            if number not in wanted or repository != slug:
                continue
            values: dict[str, str] = {}
            for field_name in field_names:
                value = raw.get(field_name)
                if value is not None:
                    values[field_name] = str(value)
            result[int(number)] = values
        return result, len(raw_items) < min(max(len(wanted) + 100, 100), 1000)

    def read(
        self,
        cwd: Path,
        source: GitHubTicketGraphConfig,
        *,
        max_items: int,
    ) -> TicketGraphSnapshot:
        if not 1 <= max_items <= 200:
            raise TicketGraphError(
                "GitHub ticket graph reads must contain between 1 and 200 issues"
            )
        slug = self._slug(cwd)
        queue: deque[int] = deque([source.root_issue])
        queued = {source.root_issue}
        parent_by_number: dict[int, int | None] = {source.root_issue: None}
        issues: dict[int, dict[str, Any]] = {}
        unavailable: set[int] = set()
        truncated = False

        while queue:
            number = queue.popleft()
            if len(issues) >= max_items:
                truncated = True
                unavailable.add(number)
                break
            try:
                issue = self._object(
                    self._api(cwd, f"repos/{slug}/issues/{number}"),
                    f"GitHub issue #{number}",
                )
            except CommandError:
                if number == source.root_issue:
                    raise
                unavailable.add(number)
                continue
            if issue.get("number") != number or "pull_request" in issue:
                unavailable.add(number)
                continue
            title = issue.get("title")
            state = issue.get("state")
            body = issue.get("body")
            if (
                not isinstance(title, str)
                or not title.strip()
                or state not in {"open", "closed", "OPEN", "CLOSED"}
                or not isinstance(body, str)
                or len(body) > _MAX_BODY_CHARS
            ):
                unavailable.add(number)
                continue
            issues[number] = issue
            try:
                children = self._list(
                    self._api(cwd, f"repos/{slug}/issues/{number}/sub_issues?per_page=100"),
                    f"GitHub sub-issues for #{number}",
                )
            except CommandError:
                unavailable.add(number)
                continue
            if len(children) == 100:
                truncated = True
            for child in children:
                child_number = child.get("number")
                if (
                    not isinstance(child_number, int)
                    or isinstance(child_number, bool)
                    or child_number <= 0
                    or "pull_request" in child
                ):
                    continue
                existing_parent = parent_by_number.get(child_number)
                if existing_parent is not None and existing_parent != number:
                    unavailable.add(child_number)
                    continue
                parent_by_number[child_number] = number
                if child_number not in queued:
                    queued.add(child_number)
                    queue.append(child_number)

        wanted = set(issues)
        comments_by_number: dict[int, tuple[str, ...]] = {}
        for number in sorted(wanted):
            try:
                raw_comments = self._list(
                    self._api(
                        cwd, f"repos/{slug}/issues/{number}/comments?per_page={_MAX_COMMENTS}"
                    ),
                    f"GitHub comments for #{number}",
                )
            except CommandError:
                unavailable.add(number)
                comments_by_number[number] = ()
                continue
            if len(raw_comments) == _MAX_COMMENTS:
                truncated = True
            comments: list[str] = []
            malformed = False
            for raw_comment in raw_comments[:_MAX_COMMENTS]:
                comment_body = raw_comment.get("body")
                if not isinstance(comment_body, str) or len(comment_body) > _MAX_COMMENT_CHARS:
                    malformed = True
                    continue
                comments.append(comment_body)
            if malformed:
                unavailable.add(number)
            comments_by_number[number] = tuple(comments)

        blockers_by_number: dict[int, set[int]] = {number: set() for number in wanted}
        for number in sorted(wanted):
            try:
                blockers = self._list(
                    self._api(
                        cwd,
                        f"repos/{slug}/issues/{number}/dependencies/blocked_by?per_page=100",
                    ),
                    f"GitHub blockers for #{number}",
                )
            except CommandError:
                unavailable.add(number)
                continue
            if len(blockers) == 100:
                truncated = True
            blockers_by_number[number].update(
                blocker_number
                for blocker in blockers
                if isinstance((blocker_number := blocker.get("number")), int)
                and not isinstance(blocker_number, bool)
                and blocker_number in wanted
            )

        project_values, project_complete = self._project_values(cwd, slug, source, wanted)
        if not project_complete:
            truncated = True
        children_by_number: dict[int, set[int]] = {number: set() for number in wanted}
        for child_number, parent_number in parent_by_number.items():
            if child_number in wanted and parent_number in wanted:
                children_by_number[parent_number].add(child_number)
        blocks_by_number: dict[int, set[int]] = {number: set() for number in wanted}
        for blocked_number, blocker_numbers in blockers_by_number.items():
            for blocker_number in blocker_numbers:
                blocks_by_number[blocker_number].add(blocked_number)

        nodes: list[TicketNode] = []
        live: list[TicketLiveMetadata] = []
        for number in sorted(wanted):
            issue = issues[number]
            title = str(issue["title"]).strip()
            state = str(issue["state"]).upper()
            body = str(issue["body"])
            metadata = self._metadata(body, self._labels(issue.get("labels")))
            overlay = project_values.get(number, {})
            status = (
                TicketStatus.DONE
                if state == "CLOSED"
                else self._enum_value(
                    TicketStatus,
                    overlay.get(source.status_field) or metadata.get("status"),
                )
            )
            priority = self._enum_value(
                TicketPriority,
                overlay.get(source.priority_field) or metadata.get("priority"),
            )
            ticket_type = self._enum_value(
                TicketType,
                overlay.get(source.type_field) or metadata.get("type"),
            )
            if status is None:
                status = TicketStatus.BACKLOG
                unavailable.add(number)
            if priority is None:
                priority = TicketPriority.P3
                unavailable.add(number)
            if ticket_type is None:
                if number == source.root_issue:
                    ticket_type = TicketType.PROGRAM
                elif children_by_number[number]:
                    ticket_type = TicketType.INITIATIVE
                else:
                    ticket_type = TicketType.IMPLEMENTATION_TICKET
            initiative = overlay.get(source.initiative_field) or metadata.get("initiative")
            roadmap = (
                (initiative.strip(),)
                if isinstance(initiative, str) and initiative.strip()
                else ("github",)
            )
            nodes.append(
                TicketNode(
                    number=number,
                    title=title,
                    ticket_type=ticket_type,
                    priority=priority,
                    status=status,
                    parent=parent_by_number.get(number),
                    blockers=tuple(sorted(blockers_by_number[number])),
                    blocks=tuple(sorted(blocks_by_number[number])),
                    children=tuple(sorted(children_by_number[number])),
                    roadmap=roadmap,
                )
            )
            live.append(
                TicketLiveMetadata(
                    number,
                    title,
                    state,
                    body,
                    comments_by_number.get(number, ()),
                )
            )

        if source.root_issue not in wanted:
            raise TicketGraphError(f"GitHub ticket graph root #{source.root_issue} is unavailable")
        snapshot = TicketGraphSnapshot(
            graph=TicketGraph(1, source.root_issue, tuple(nodes)),
            observed_at=datetime.now(timezone.utc).isoformat(),
            evidence_complete=not unavailable and not truncated,
            unavailable=tuple(sorted(unavailable)),
            truncated=truncated,
            live_issues=tuple(live),
        )
        return snapshot


class GitHubTicketGraphReader:
    """Legacy bounded metadata reader retained for fixture and API compatibility."""

    def __init__(self, executor: CommandExecutor, *, cwd: Path) -> None:
        self._executor = executor
        self._cwd = cwd

    def read(
        self, repository: str, issue_numbers: tuple[int, ...]
    ) -> tuple[TicketLiveMetadata, ...]:
        if _REPOSITORY.fullmatch(repository) is None:
            raise TicketGraphError("live repository must use owner/name format")
        if not issue_numbers or len(issue_numbers) > 100:
            raise TicketGraphError("live issue read must contain between 1 and 100 issues")
        if tuple(sorted(set(issue_numbers))) != issue_numbers:
            raise TicketGraphError("live issue numbers must be sorted and unique")
        snapshots: list[TicketLiveMetadata] = []
        for issue_number in issue_numbers:
            if (
                not isinstance(issue_number, int)
                or isinstance(issue_number, bool)
                or issue_number <= 0
            ):
                raise TicketGraphError("live issue numbers must be positive integers")
            try:
                result = self._executor.run(
                    (
                        "gh",
                        "issue",
                        "view",
                        str(issue_number),
                        "--repo",
                        repository,
                        "--json",
                        "number,title,state,body,comments",
                    ),
                    cwd=self._cwd,
                    timeout=30,
                    output_limit=_MAX_BODY_CHARS + 10_000,
                )
            except CommandError:
                continue
            try:
                payload: Any = json.loads(result.stdout)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            live_number = payload.get("number")
            title = payload.get("title")
            state = payload.get("state")
            body = payload.get("body")
            raw_comments = payload.get("comments")
            comments: list[str] = []
            if isinstance(raw_comments, list):
                for raw_comment in raw_comments[:_MAX_COMMENTS]:
                    comment_body = (
                        raw_comment.get("body") if isinstance(raw_comment, dict) else None
                    )
                    if isinstance(comment_body, str) and len(comment_body) <= _MAX_COMMENT_CHARS:
                        comments.append(comment_body)
            if (
                live_number != issue_number
                or not isinstance(title, str)
                or not title.strip()
                or state not in {"OPEN", "CLOSED"}
                or not isinstance(body, str)
                or len(body) > _MAX_BODY_CHARS
            ):
                continue
            snapshots.append(
                TicketLiveMetadata(issue_number, title.strip(), state, body, tuple(comments))
            )
        return tuple(snapshots)
