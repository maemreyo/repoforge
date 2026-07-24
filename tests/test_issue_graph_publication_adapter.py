from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from repoforge.adapters.github.gh_cli import GhCliGateway
from repoforge.config import ServerConfig
from repoforge.domain.errors import CommandError
from repoforge.ports.cancellation import CancellationToken
from repoforge.ports.command import CommandResult


def _issue(number: int, database_id: int) -> dict[str, object]:
    return {
        "id": database_id,
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "body": "body",
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
    }


class PublicationApiExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.duplicate_once = True

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(extra or {})

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        del input_text, timeout, check, extra_env, output_limit, cancel_token
        command = tuple(argv)
        self.calls.append(command)
        if command[:3] == ("gh", "repo", "view"):
            return CommandResult(command, str(cwd), 0, "acme/widgets\n", "")
        method = command[command.index("--method") + 1]
        endpoint = next(item for item in command if item.startswith("repos/"))
        if method == "PATCH":
            return CommandResult(command, str(cwd), 0, json.dumps(_issue(10, 1010)), "")
        if method == "DELETE":
            return CommandResult(command, str(cwd), 0, json.dumps(_issue(10, 1010)), "")
        if method == "POST" and endpoint.endswith("/sub_issues") and self.duplicate_once:
            self.duplicate_once = False
            raise CommandError("422 Validation Failed: Target issue has already been taken")
        if method == "POST":
            return CommandResult(command, str(cwd), 0, json.dumps(_issue(20, 2020)), "")
        if endpoint.endswith("/sub_issues?per_page=100"):
            return CommandResult(command, str(cwd), 0, json.dumps([_issue(20, 2020)]), "")
        return CommandResult(command, str(cwd), 0, json.dumps([]), "")

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int | None = None,
        max_bytes: int,
    ) -> bytes:
        del argv, cwd, timeout, max_bytes
        raise AssertionError("binary execution is not used")


def test_issue_mutation_api_pins_version_and_supports_update_and_remove(tmp_path: Path) -> None:
    executor = PublicationApiExecutor()
    gateway = GhCliGateway(executor, ServerConfig(tmp_path / "workspaces", tmp_path / "state"))

    updated = gateway.update_issue(tmp_path, 10, title="Updated", body="Managed body")
    removed_child = gateway.remove_sub_issue(tmp_path, 10, 2020)
    removed_blocker = gateway.remove_blocked_by(tmp_path, 10, 2020)

    assert updated.issue_number == removed_child.issue_number == removed_blocker.issue_number == 10
    api_calls = [call for call in executor.calls if call[:2] == ("gh", "api")]
    assert api_calls
    assert all("Accept: application/vnd.github+json" in call for call in api_calls)
    assert all("X-GitHub-Api-Version: 2022-11-28" in call for call in api_calls)
    assert any("repos/acme/widgets/issues/10/sub_issue" in call for call in api_calls)
    assert any(
        "repos/acme/widgets/issues/10/dependencies/blocked_by/2020" in call for call in api_calls
    )


def test_duplicate_sub_issue_422_requires_authoritative_confirmation(tmp_path: Path) -> None:
    executor = PublicationApiExecutor()
    gateway = GhCliGateway(executor, ServerConfig(tmp_path / "workspaces", tmp_path / "state"))

    reconciled = gateway.add_sub_issue(tmp_path, 10, 2020)

    assert reconciled.issue_number == 20
    assert reconciled.database_id == 2020
    endpoints = [item for call in executor.calls for item in call if item.startswith("repos/")]
    assert "repos/acme/widgets/issues/10/sub_issues" in endpoints
    assert "repos/acme/widgets/issues/10/sub_issues?per_page=100" in endpoints
