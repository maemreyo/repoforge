"""Constrained GitHub CLI adapter for ticket graph and Project V2 synchronization."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from ...config import ServerConfig
from ...domain.errors import CommandError, ConfigError
from ...domain.ticket_sync import (
    MANAGED_FIELDS,
    TicketIssueIdentity,
    TicketProjectFieldSnapshot,
    TicketProjectItemSnapshot,
    TicketProjectOwnerType,
    TicketProjectPreflight,
    TicketProjectSnapshot,
    TicketProjectTarget,
    TicketProjectViewSnapshot,
    TicketSyncChange,
    TicketSyncChangeKind,
)
from ...domain.tickets import TicketGraph
from ...ports.command import CommandExecutor, CommandResult

_API_VERSION = "2026-03-10"
_MAX_ISSUE_PAGES = 20
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SCOPE_LINE = re.compile(r"Token scopes:\s*(.*)", re.IGNORECASE)
_VIEW_LAYOUTS = {
    "BOARD_LAYOUT": "board",
    "ROADMAP_LAYOUT": "roadmap",
    "TABLE_LAYOUT": "table",
}
_PROJECT_VIEWS_QUERY = """
query RepoForgeTicketProjectViews($projectId: ID!, $first: Int!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      views(first: $first) {
        nodes {
          id
          name
          layout
          filter
          sortByFields(first: 20) {
            nodes {
              direction
              field {
                ... on ProjectV2FieldCommon {
                  name
                }
              }
            }
          }
        }
      }
    }
  }
}
""".strip()


class GhTicketProjectGateway:
    """Map typed ticket-project operations to a fixed set of GitHub CLI commands."""

    def __init__(self, executor: CommandExecutor, server: ServerConfig):
        self.executor = executor
        self.server = server
        self.output_limit = min(max(server.max_tool_output_chars, 200_000), 5_000_000)

    @staticmethod
    def _json(result: CommandResult, context: str) -> Any:
        if result.stdout_truncated:
            raise CommandError(f"{context} returned oversized JSON")
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError as exc:
            raise CommandError(f"{context} returned invalid JSON") from exc

    @classmethod
    def _object(cls, result: CommandResult, context: str) -> dict[str, Any]:
        payload = cls._json(result, context)
        if not isinstance(payload, dict):
            raise CommandError(f"{context} returned a non-object JSON value")
        return cast(dict[str, Any], payload)

    @classmethod
    def _list(cls, result: CommandResult, context: str) -> list[dict[str, Any]]:
        payload = cls._json(result, context)
        if not isinstance(payload, list):
            raise CommandError(f"{context} returned a non-list JSON value")
        return [cast(dict[str, Any], item) for item in payload if isinstance(item, dict)]

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        check: bool = True,
        output_limit: int | None = None,
    ) -> CommandResult:
        return self.executor.run(
            argv,
            cwd=cwd,
            check=check,
            timeout=self.server.default_command_timeout_seconds,
            output_limit=output_limit or self.output_limit,
        )

    @staticmethod
    def _api_headers() -> list[str]:
        return [
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {_API_VERSION}",
        ]

    def _api(
        self,
        cwd: Path,
        method: str,
        endpoint: str,
        *,
        fields: tuple[tuple[str, str], ...] = (),
    ) -> CommandResult:
        argv = ["gh", "api", "--method", method, endpoint]
        argv.extend(self._api_headers())
        for flag, value in fields:
            argv.extend([flag, value])
        return self._run(argv, cwd=cwd)

    def _slug(self, cwd: Path) -> str:
        slug = self._run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=cwd,
            output_limit=512,
        ).stdout.strip()
        if not _REPOSITORY.fullmatch(slug):
            raise CommandError(f"Unexpected GitHub repository name: {slug!r}")
        return slug

    def _project(self, cwd: Path, target: TicketProjectTarget) -> dict[str, Any]:
        return self._object(
            self._run(
                [
                    "gh",
                    "project",
                    "view",
                    str(target.project_number),
                    "--owner",
                    target.owner,
                    "--format",
                    "json",
                ],
                cwd=cwd,
            ),
            "gh project view",
        )

    def _field_records(self, cwd: Path, target: TicketProjectTarget) -> list[dict[str, Any]]:
        payload = self._object(
            self._run(
                [
                    "gh",
                    "project",
                    "field-list",
                    str(target.project_number),
                    "--owner",
                    target.owner,
                    "--format",
                    "json",
                    "--limit",
                    "100",
                ],
                cwd=cwd,
            ),
            "gh project field-list",
        )
        fields = payload.get("fields")
        if not isinstance(fields, list):
            raise CommandError("gh project field-list returned no fields list")
        return [cast(dict[str, Any], item) for item in fields if isinstance(item, dict)]

    def _item_records(
        self, cwd: Path, target: TicketProjectTarget, *, limit: int
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 1000))
        payload = self._object(
            self._run(
                [
                    "gh",
                    "project",
                    "item-list",
                    str(target.project_number),
                    "--owner",
                    target.owner,
                    "--format",
                    "json",
                    "--limit",
                    str(bounded_limit),
                ],
                cwd=cwd,
            ),
            "gh project item-list",
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise CommandError("gh project item-list returned no items list")
        return [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]

    def _view_records(self, cwd: Path, project_id: str) -> list[dict[str, Any]]:
        payload = self._object(
            self._run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    f"query={_PROJECT_VIEWS_QUERY}",
                    "-F",
                    f"projectId={project_id}",
                    "-F",
                    "first=100",
                ],
                cwd=cwd,
            ),
            "GitHub Project views",
        )
        data = payload.get("data")
        node = data.get("node") if isinstance(data, dict) else None
        views = node.get("views") if isinstance(node, dict) else None
        records = views.get("nodes") if isinstance(views, dict) else None
        if not isinstance(records, list):
            raise CommandError("GitHub Project views query returned no views list")
        return [cast(dict[str, Any], item) for item in records if isinstance(item, dict)]

    @staticmethod
    def _scopes(text: str) -> tuple[str, ...]:
        for line in text.splitlines():
            match = _SCOPE_LINE.search(line)
            if match:
                return tuple(sorted(set(re.findall(r"'([^']+)'", match.group(1)))))
        return ()

    @staticmethod
    def _positive_int(value: object) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    def preflight(
        self,
        cwd: Path,
        target: TicketProjectTarget,
        *,
        apply: bool,
    ) -> TicketProjectPreflight:
        auth = self._run(["gh", "auth", "status"], cwd=cwd, check=False)
        authenticated = auth.returncode == 0
        scopes = self._scopes(auth.combined)
        rate_remaining: int | None = None
        rate_reset: str | None = None
        warnings: list[str] = []
        project_access = False

        if authenticated:
            rate = self._object(
                self._api(cwd, "GET", "rate_limit"),
                "GitHub rate limit",
            )
            resources = rate.get("resources")
            candidates: list[tuple[int, int | None]] = []
            if isinstance(resources, dict):
                for resource_name in ("core", "graphql"):
                    resource = resources.get(resource_name)
                    if not isinstance(resource, dict):
                        continue
                    remaining = self._positive_int(resource.get("remaining"))
                    if remaining is None:
                        continue
                    candidates.append((remaining, self._positive_int(resource.get("reset"))))
            if candidates:
                rate_remaining, reset = min(candidates, key=lambda item: item[0])
                if reset is not None:
                    rate_reset = datetime.fromtimestamp(reset, tz=timezone.utc).isoformat()
            project_access = bool(self._project(cwd, target).get("id"))

        missing: list[str] = []
        if not authenticated:
            missing.append("authenticated GitHub CLI session")
        if scopes:
            project_scopes = {"project"} if apply else {"project", "read:project"}
            if not project_scopes.intersection(scopes):
                missing.append("project" if apply else "read:project")
            if apply and not {"repo", "public_repo"}.intersection(scopes):
                missing.append("repo or public_repo")
        elif authenticated:
            warnings.append(
                "GitHub CLI did not expose classic OAuth scopes; fine-grained permissions will be "
                "validated by the project and issue API operations."
            )
        if authenticated and not project_access:
            missing.append("project access")
        if rate_remaining is not None and rate_remaining < 50:
            warnings.append(
                f"GitHub API rate limit is low ({rate_remaining} remaining); apply may stop partially."
            )

        return TicketProjectPreflight(
            authenticated=authenticated,
            ready=not missing,
            scopes=scopes,
            missing_scopes=tuple(missing),
            rate_remaining=rate_remaining,
            rate_reset=rate_reset,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _field_type(raw: dict[str, Any]) -> str:
        value = raw.get("dataType") or raw.get("type") or ""
        text = str(value)
        if text in {"TEXT", "NUMBER", "DATE", "ITERATION", "SINGLE_SELECT"}:
            return text
        if "SingleSelect" in text:
            return "SINGLE_SELECT"
        if text.endswith("Field"):
            return str(raw.get("dataType") or "TEXT")
        return text.upper()

    @staticmethod
    def _value_text(raw: dict[str, Any]) -> str:
        for key in ("name", "text", "date", "value"):
            value = raw.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return (
                    str(int(value))
                    if isinstance(value, float) and value.is_integer()
                    else str(value)
                )
        number = raw.get("number")
        if isinstance(number, (int, float)) and not isinstance(number, bool):
            return (
                str(int(number))
                if isinstance(number, float) and number.is_integer()
                else str(number)
            )
        return ""

    @classmethod
    def _parse_fields(cls, records: list[dict[str, Any]]) -> dict[str, TicketProjectFieldSnapshot]:
        fields: dict[str, TicketProjectFieldSnapshot] = {}
        for raw in records:
            name = raw.get("name")
            field_id = raw.get("id")
            if not isinstance(name, str) or not isinstance(field_id, str):
                continue
            options: dict[str, str] = {}
            raw_options = raw.get("options")
            if isinstance(raw_options, list):
                for option in raw_options:
                    if not isinstance(option, dict):
                        continue
                    option_name = option.get("name")
                    option_id = option.get("id")
                    if isinstance(option_name, str) and isinstance(option_id, str):
                        options[option_name] = option_id
            fields[name] = TicketProjectFieldSnapshot(
                field_id,
                cls._field_type(raw),
                options,
            )
        return fields

    @classmethod
    def _parse_items(
        cls, records: list[dict[str, Any]], slug: str
    ) -> dict[int, TicketProjectItemSnapshot]:
        items: dict[int, TicketProjectItemSnapshot] = {}
        managed_names = {definition.name for definition in MANAGED_FIELDS}
        for raw in records:
            item_id = raw.get("id")
            content = raw.get("content")
            if not isinstance(item_id, str) or not isinstance(content, dict):
                continue
            number = content.get("number")
            repository = content.get("repository")
            if isinstance(repository, dict):
                repository = repository.get("nameWithOwner")
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or not isinstance(repository, str)
                or repository != slug
            ):
                # Fail closed on a missing/unparseable repository identity rather than
                # mapping by issue number alone: a multi-repo Project can carry the same
                # issue number for different repositories, and defaulting to "include"
                # would let this snapshot silently point a mutation at the wrong repo's item.
                continue
            values: dict[str, str] = {}
            raw_values = raw.get("fieldValues")
            if isinstance(raw_values, dict):
                raw_values = raw_values.get("nodes")
            if isinstance(raw_values, list):
                for value in raw_values:
                    if not isinstance(value, dict):
                        continue
                    field = value.get("field")
                    field_name = (
                        field.get("name") if isinstance(field, dict) else value.get("fieldName")
                    )
                    if isinstance(field_name, str) and field_name in managed_names:
                        values[field_name] = cls._value_text(value)
            for name in managed_names:
                direct = raw.get(name)
                if direct is not None:
                    values[name] = str(direct)
            items[number] = TicketProjectItemSnapshot(item_id, values)
        return items

    @staticmethod
    def _parse_views(records: list[dict[str, Any]]) -> dict[str, TicketProjectViewSnapshot]:
        views: dict[str, TicketProjectViewSnapshot] = {}
        for raw in records:
            name = raw.get("name")
            view_id = raw.get("id")
            raw_layout = raw.get("layout")
            if not all(isinstance(item, str) for item in (name, view_id, raw_layout)):
                continue
            layout = _VIEW_LAYOUTS.get(str(raw_layout), str(raw_layout).lower())
            sort_connection = raw.get("sortByFields")
            raw_sort = sort_connection.get("nodes") if isinstance(sort_connection, dict) else None
            sort_by: list[tuple[str, str]] = []
            if isinstance(raw_sort, list):
                for item in raw_sort:
                    if not isinstance(item, dict):
                        continue
                    field = item.get("field")
                    field_name = field.get("name") if isinstance(field, dict) else None
                    direction = item.get("direction")
                    if isinstance(field_name, str) and isinstance(direction, str):
                        sort_by.append((field_name, direction.lower()))
            filter_query = raw.get("filter")
            views[str(name)] = TicketProjectViewSnapshot(
                str(view_id),
                layout,
                filter_query if isinstance(filter_query, str) else "",
                tuple(sort_by),
            )
        return views

    def _issue_identities(
        self,
        cwd: Path,
        slug: str,
        wanted: set[int],
    ) -> tuple[dict[int, TicketIssueIdentity], bool]:
        """Return identities found plus whether the bounded page scan may have missed some.

        `truncated` is True only when the scan exhausted `_MAX_ISSUE_PAGES` without either
        finding every wanted issue or reaching a short (final) page -- i.e. more issues may
        exist beyond the bound that this scan never looked at.
        """
        identities: dict[int, TicketIssueIdentity] = {}
        truncated = True
        for page in range(1, _MAX_ISSUE_PAGES + 1):
            records = self._list(
                self._api(
                    cwd,
                    "GET",
                    f"repos/{slug}/issues?state=all&per_page=100&page={page}",
                ),
                f"GitHub issue identities page {page}",
            )
            for raw in records:
                number = raw.get("number")
                database_id = raw.get("id")
                node_id = raw.get("node_id")
                if (
                    isinstance(number, int)
                    and number in wanted
                    and isinstance(database_id, int)
                    and isinstance(node_id, str)
                    and "pull_request" not in raw
                ):
                    identities[number] = TicketIssueIdentity(number, node_id, database_id)
            if wanted.issubset(identities) or len(records) < 100:
                truncated = False
                break
        return identities, truncated

    def snapshot(
        self,
        cwd: Path,
        target: TicketProjectTarget,
        graph: TicketGraph,
    ) -> TicketProjectSnapshot:
        slug = self._slug(cwd)
        project = self._project(cwd, target)
        project_id = project.get("id")
        if not isinstance(project_id, str) or not project_id:
            raise CommandError("GitHub Project returned no stable node id")
        title = project.get("title")
        field_records = self._field_records(cwd, target)
        fields = self._parse_fields(field_records)
        item_limit = len(graph.nodes) + 100
        raw_items = self._item_records(cwd, target, limit=item_limit)
        # `gh project item-list --limit` returns at most the bounded limit; a full page at
        # that bound means the Project may hold more items this fetch never observed.
        items_truncated = len(raw_items) >= max(1, min(item_limit, 1000))
        items = self._parse_items(raw_items, slug)
        views = self._parse_views(self._view_records(cwd, project_id))
        wanted = {node.number for node in graph.nodes}
        identities, identities_truncated = self._issue_identities(cwd, slug, wanted)

        sub_issues: set[tuple[int, int]] = set()
        parent_numbers = sorted({node.parent for node in graph.nodes if node.parent is not None})
        for parent in parent_numbers:
            children = self._list(
                self._api(
                    cwd,
                    "GET",
                    f"repos/{slug}/issues/{parent}/sub_issues?per_page=100",
                ),
                f"GitHub sub-issues for #{parent}",
            )
            for child in children:
                number = child.get("number")
                if isinstance(number, int) and number in wanted:
                    sub_issues.add((parent, number))

        blocked_by: set[tuple[int, int]] = set()
        for node in graph.nodes:
            if not node.blockers:
                continue
            blockers = self._list(
                self._api(
                    cwd,
                    "GET",
                    f"repos/{slug}/issues/{node.number}/dependencies/blocked_by?per_page=100",
                ),
                f"GitHub blockers for #{node.number}",
            )
            for blocker in blockers:
                number = blocker.get("number")
                if isinstance(number, int) and number in wanted:
                    blocked_by.add((node.number, number))

        return TicketProjectSnapshot(
            project_id,
            title if isinstance(title, str) else "",
            fields,
            items,
            views,
            identities,
            frozenset(sub_issues),
            frozenset(blocked_by),
            identities_truncated=identities_truncated,
            items_truncated=items_truncated,
        )

    @staticmethod
    def _payload_str(change: TicketSyncChange, key: str) -> str:
        value = change.payload.get(key)
        if not isinstance(value, str):
            raise ConfigError(f"Ticket sync change {change.change_id} has invalid {key}")
        return value

    @staticmethod
    def _payload_int(change: TicketSyncChange, key: str) -> int:
        value = change.payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"Ticket sync change {change.change_id} has invalid {key}")
        return value

    def _apply_field(
        self, cwd: Path, target: TicketProjectTarget, change: TicketSyncChange
    ) -> dict[str, Any]:
        name = self._payload_str(change, "name")
        data_type = self._payload_str(change, "data_type")
        definition = next((item for item in MANAGED_FIELDS if item.name == name), None)
        if definition is None or definition.data_type != data_type:
            raise ConfigError(f"Refusing unmanaged Project field mutation: {name!r}")
        argv = [
            "gh",
            "project",
            "field-create",
            str(target.project_number),
            "--owner",
            target.owner,
            "--name",
            name,
            "--data-type",
            data_type,
        ]
        if data_type == "SINGLE_SELECT":
            options = change.payload.get("options")
            if not isinstance(options, list) or not all(isinstance(item, str) for item in options):
                raise ConfigError(f"Managed single-select field {name!r} has invalid options")
            argv.extend(["--single-select-options", ",".join(options)])
        argv.extend(["--format", "json"])
        payload = self._object(self._run(argv, cwd=cwd), "gh project field-create")
        return {"status": "applied", "resource": payload.get("id")}

    def _apply_item(
        self, cwd: Path, target: TicketProjectTarget, change: TicketSyncChange
    ) -> dict[str, Any]:
        issue = self._payload_int(change, "issue")
        slug = self._slug(cwd)
        payload = self._object(
            self._run(
                [
                    "gh",
                    "project",
                    "item-add",
                    str(target.project_number),
                    "--owner",
                    target.owner,
                    "--url",
                    f"https://github.com/{slug}/issues/{issue}",
                    "--format",
                    "json",
                ],
                cwd=cwd,
            ),
            "gh project item-add",
        )
        return {"status": "applied", "resource": payload.get("id")}

    def _apply_field_value(
        self, cwd: Path, target: TicketProjectTarget, change: TicketSyncChange
    ) -> dict[str, Any]:
        issue = self._payload_int(change, "issue")
        field_name = self._payload_str(change, "field")
        value = self._payload_str(change, "value")
        if field_name not in {item.name for item in MANAGED_FIELDS}:
            raise ConfigError(f"Refusing unmanaged Project field value mutation: {field_name!r}")
        project = self._project(cwd, target)
        project_id = project.get("id")
        if not isinstance(project_id, str):
            raise CommandError("GitHub Project returned no stable node id")
        fields = self._parse_fields(self._field_records(cwd, target))
        field = fields.get(field_name)
        if field is None:
            raise CommandError(f"Managed Project field is unavailable after creation: {field_name}")
        slug = self._slug(cwd)
        items = self._parse_items(self._item_records(cwd, target, limit=1000), slug)
        item = items.get(issue)
        if item is None:
            raise CommandError(f"Project item for issue #{issue} is unavailable after addition")
        argv = [
            "gh",
            "project",
            "item-edit",
            "--id",
            item.item_id,
            "--project-id",
            project_id,
            "--field-id",
            field.field_id,
        ]
        if field.data_type == "SINGLE_SELECT":
            option_id = field.options.get(value)
            if option_id is None:
                raise CommandError(
                    f"Managed Project option {value!r} is unavailable for field {field_name!r}"
                )
            argv.extend(["--single-select-option-id", option_id])
        elif field.data_type == "NUMBER":
            try:
                float(value)
            except ValueError as exc:
                raise ConfigError(f"Managed Project number value is invalid: {value!r}") from exc
            argv.extend(["--number", value])
        else:
            argv.extend(["--text", value])
        self._run(argv, cwd=cwd)
        return {"status": "applied", "resource": item.item_id}

    def _apply_sub_issue(self, cwd: Path, change: TicketSyncChange) -> dict[str, Any]:
        parent = self._payload_int(change, "parent")
        child_id = self._payload_int(change, "child_issue_id")
        slug = self._slug(cwd)
        payload = self._object(
            self._api(
                cwd,
                "POST",
                f"repos/{slug}/issues/{parent}/sub_issues",
                fields=(("-F", f"sub_issue_id={child_id}"),),
            ),
            "GitHub add sub-issue",
        )
        return {"status": "applied", "resource": payload.get("number")}

    def _apply_blocked_by(self, cwd: Path, change: TicketSyncChange) -> dict[str, Any]:
        issue = self._payload_int(change, "issue")
        blocker_id = self._payload_int(change, "blocker_issue_id")
        slug = self._slug(cwd)
        payload = self._object(
            self._api(
                cwd,
                "POST",
                f"repos/{slug}/issues/{issue}/dependencies/blocked_by",
                fields=(("-F", f"issue_id={blocker_id}"),),
            ),
            "GitHub add blocked-by dependency",
        )
        return {"status": "applied", "resource": payload.get("number")}

    def _apply_view(
        self, cwd: Path, target: TicketProjectTarget, change: TicketSyncChange
    ) -> dict[str, Any]:
        name = self._payload_str(change, "name")
        layout = self._payload_str(change, "layout")
        filter_query = self._payload_str(change, "filter_query")
        if layout not in {"table", "board", "roadmap"}:
            raise ConfigError(f"Unsupported Project view layout: {layout!r}")
        prefix = "orgs" if target.owner_type is TicketProjectOwnerType.ORGANIZATION else "users"
        fields: list[tuple[str, str]] = [("-f", f"name={name}"), ("-f", f"layout={layout}")]
        if filter_query:
            fields.append(("-f", f"filter={filter_query}"))
        payload = self._object(
            self._api(
                cwd,
                "POST",
                f"{prefix}/{target.owner}/projectsV2/{target.project_number}/views",
                fields=tuple(fields),
            ),
            "GitHub create Project view",
        )
        manual_actions: list[str] = []
        raw_sort = change.payload.get("sort_by")
        if isinstance(raw_sort, list) and raw_sort:
            rendered = ", ".join(
                f"{item[0]} {item[1]}"
                for item in raw_sort
                if isinstance(item, list)
                and len(item) == 2
                and all(isinstance(part, str) for part in item)
            )
            if rendered:
                manual_actions.append(f"Configure {name} sorting in GitHub: {rendered}.")
        value = payload.get("value")
        resource = value.get("node_id") if isinstance(value, dict) else payload.get("node_id")
        return {"status": "applied", "resource": resource, "manual_actions": manual_actions}

    def apply_change(
        self,
        cwd: Path,
        target: TicketProjectTarget,
        change: TicketSyncChange,
    ) -> dict[str, Any]:
        if change.kind is TicketSyncChangeKind.CREATE_FIELD:
            return self._apply_field(cwd, target, change)
        if change.kind is TicketSyncChangeKind.ADD_PROJECT_ITEM:
            return self._apply_item(cwd, target, change)
        if change.kind is TicketSyncChangeKind.SET_FIELD_VALUE:
            return self._apply_field_value(cwd, target, change)
        if change.kind is TicketSyncChangeKind.ADD_SUB_ISSUE:
            return self._apply_sub_issue(cwd, change)
        if change.kind is TicketSyncChangeKind.ADD_BLOCKED_BY:
            return self._apply_blocked_by(cwd, change)
        if change.kind is TicketSyncChangeKind.CREATE_VIEW:
            return self._apply_view(cwd, target, change)
        raise ConfigError(f"Unsupported ticket sync change kind: {change.kind.value}")
