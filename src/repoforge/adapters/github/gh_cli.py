from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from ...config import RepositoryConfig, ServerConfig
from ...domain.errors import CommandError
from ...ports.command import CommandExecutor, CommandResult


class GhCliGateway:
    def __init__(self, executor: CommandExecutor, server: ServerConfig):
        self.executor = executor
        self.server = server

    @staticmethod
    def _object(result: CommandResult, context: str) -> dict[str, Any]:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CommandError(f"{context} returned invalid or oversized JSON") from exc
        if not isinstance(payload, dict):
            raise CommandError(f"{context} returned a non-object JSON value")
        return cast(dict[str, Any], payload)

    def _slug(self, cwd: Path) -> str:
        slug = self.executor.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=cwd,
        ).stdout.strip()
        if not re.fullmatch("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
            raise CommandError(f"Unexpected GitHub repository name: {slug!r}")
        return slug

    @staticmethod
    def _trim(value: Any, limit: int) -> Any:
        return (
            value
            if not isinstance(value, str) or len(value) <= limit
            else f"{value[:limit]}\n... <{len(value) - limit} characters omitted>"
        )

    def auth_status(self, cwd: Path) -> tuple[bool, str]:
        r = self.executor.run(["gh", "auth", "status"], cwd=cwd, check=False)
        return (r.returncode == 0, r.combined)

    def issue_read(self, cwd: Path, issue_number: int) -> dict[str, Any]:
        r = self.executor.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                self._slug(cwd),
                "--json",
                "number,title,body,state,author,labels,assignees,url,comments",
            ],
            cwd=cwd,
            output_limit=10000000,
        )
        p = self._object(r, "gh issue view")
        p["body"] = self._trim(p.get("body"), 50000)
        comments = p.get("comments")
        if isinstance(comments, list):
            p["comment_count"] = len(comments)
            p["comments"] = [
                dict(x, body=self._trim(x.get("body"), 8000)) if isinstance(x, dict) else x
                for x in comments[-20:]
            ]
            p["comments_truncated"] = len(comments) > 20
        return p

    def _trim_pr(self, p: dict[str, Any]) -> dict[str, Any]:
        p["body"] = self._trim(p.get("body"), 50000)
        for key, limit in {
            "files": 300,
            "commits": 100,
            "statusCheckRollup": 100,
            "reviews": 50,
        }.items():
            value = p.get(key)
            if isinstance(value, list) and len(value) > limit:
                p[f"{key}_count"] = len(value)
                p[key] = value[:limit]
                p[f"{key}_truncated"] = True
        return p

    def pr_read(self, cwd: Path, pr_number: int) -> dict[str, Any]:
        r = self.executor.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                self._slug(cwd),
                "--json",
                "number,title,body,state,isDraft,author,baseRefName,headRefName,url,files,commits,statusCheckRollup,reviews",
            ],
            cwd=cwd,
            output_limit=10000000,
        )
        return self._trim_pr(self._object(r, "gh pr view"))

    def find_pr(self, cwd: Path, branch: str) -> dict[str, Any] | None:
        r = self.executor.run(
            ["gh", "pr", "view", branch, "--json", "number,url,isDraft,state"],
            cwd=cwd,
            check=False,
        )
        return self._object(r, "gh pr view") if r.returncode == 0 else None

    def create_draft(
        self,
        cwd: Path,
        repo: RepositoryConfig,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        argv = [
            "gh",
            "pr",
            "create",
            "--draft",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            "-",
        ]
        if repo.no_maintainer_edit:
            argv.append("--no-maintainer-edit")
        for x in repo.pr_labels:
            argv.extend(["--label", x])
        for x in repo.pr_reviewers:
            argv.extend(["--reviewer", x])
        return (
            self.executor.run(
                argv,
                cwd=cwd,
                input_text=body,
                timeout=self.server.verification_timeout_seconds,
            )
            .stdout.strip()
            .splitlines()[-1]
        )

    def update(
        self, cwd: Path, branch: str, *, title: str | None, body: str | None
    ) -> dict[str, Any]:
        argv = ["gh", "pr", "edit", branch]
        input_text = None
        if title is not None:
            argv.extend(["--title", title])
        if body is not None:
            argv.extend(["--body-file", "-"])
            input_text = body
        self.executor.run(
            argv,
            cwd=cwd,
            input_text=input_text,
            timeout=self.server.verification_timeout_seconds,
        )
        return self._object(
            self.executor.run(
                [
                    "gh",
                    "pr",
                    "view",
                    branch,
                    "--json",
                    "number,title,url,state,isDraft,body",
                ],
                cwd=cwd,
                output_limit=2000000,
            ),
            "gh pr view",
        )

    def status(self, cwd: Path, branch: str) -> dict[str, Any]:
        r = self.executor.run(
            [
                "gh",
                "pr",
                "view",
                branch,
                "--json",
                "number,title,url,state,isDraft,mergeable,reviewDecision,statusCheckRollup",
            ],
            cwd=cwd,
            output_limit=10000000,
        )
        return self._trim_pr(self._object(r, "gh pr view"))

    def checks(self, cwd: Path, branch: str, *, required_only: bool) -> list[dict[str, Any]]:
        argv = [
            "gh",
            "pr",
            "checks",
            branch,
            "--json",
            "name,state,bucket,link,workflow,description,startedAt,completedAt",
        ]
        if required_only:
            argv.append("--required")
        r = self.executor.run(argv, cwd=cwd, check=False, output_limit=5000000)
        if r.returncode not in (0, 1, 8):
            raise CommandError(r.combined)
        try:
            p = json.loads(r.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise CommandError("gh pr checks returned invalid JSON") from exc
        if not isinstance(p, list):
            raise CommandError("gh pr checks returned a non-list JSON value")
        return [x for x in p if isinstance(x, dict)]
