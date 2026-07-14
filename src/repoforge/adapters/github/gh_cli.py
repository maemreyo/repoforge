from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from ...config import RepositoryConfig, ServerConfig
from ...domain.errors import CommandError
from ...ports.command import CommandExecutor, CommandResult
from ...ports.github import (
    GitHubActionsJob,
    GitHubActionsStep,
    GitHubCheckAnnotation,
    GitHubCheckRun,
    GitHubJobLog,
)

_ACTIONS_JOB_URL = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/"
    r"([1-9][0-9]*)(?:/attempts/([1-9][0-9]*))?/job/([1-9][0-9]*)(?:[/?#].*)?$"
)
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?")


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

    @staticmethod
    def _list(result: CommandResult, context: str) -> list[dict[str, Any]]:
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise CommandError(f"{context} returned invalid or oversized JSON") from exc
        if not isinstance(payload, list):
            raise CommandError(f"{context} returned a non-list JSON value")
        return [cast(dict[str, Any], item) for item in payload if isinstance(item, dict)]

    @staticmethod
    def _string(value: object) -> str:
        return value if isinstance(value, str) else ""

    @staticmethod
    def _integer(value: object) -> int | None:
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
        )

    def _slug(self, cwd: Path) -> str:
        slug = self.executor.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=cwd,
        ).stdout.strip()
        if not re.fullmatch("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
            raise CommandError(f"Unexpected GitHub repository name: {slug!r}")
        return slug

    def _api_result(
        self,
        cwd: Path,
        endpoint: str,
        *,
        fields: tuple[tuple[str, str], ...] = (),
        output_limit: int,
        check: bool = True,
    ) -> CommandResult:
        argv = ["gh", "api", "--method", "GET", endpoint]
        for key, value in fields:
            argv.extend(["-f", f"{key}={value}"])
        return self.executor.run(
            argv,
            cwd=cwd,
            check=check,
            output_limit=output_limit,
        )

    def _api_object(
        self,
        cwd: Path,
        endpoint: str,
        *,
        fields: tuple[tuple[str, str], ...] = (),
        context: str,
        output_limit: int = 5_000_000,
    ) -> dict[str, Any]:
        return self._object(
            self._api_result(
                cwd,
                endpoint,
                fields=fields,
                output_limit=output_limit,
            ),
            context,
        )

    def _api_list(
        self,
        cwd: Path,
        endpoint: str,
        *,
        fields: tuple[tuple[str, str], ...] = (),
        context: str,
        output_limit: int = 5_000_000,
    ) -> list[dict[str, Any]]:
        return self._list(
            self._api_result(
                cwd,
                endpoint,
                fields=fields,
                output_limit=output_limit,
            ),
            context,
        )

    @staticmethod
    def _trim(value: Any, limit: int) -> Any:
        return (
            value
            if not isinstance(value, str) or len(value) <= limit
            else f"{value[:limit]}\n... <{len(value) - limit} characters omitted>"
        )

    def auth_status(self, cwd: Path) -> tuple[bool, str]:
        result = self.executor.run(["gh", "auth", "status"], cwd=cwd, check=False)
        return (result.returncode == 0, result.combined)

    def issue_read(self, cwd: Path, issue_number: int) -> dict[str, Any]:
        result = self.executor.run(
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
            output_limit=10_000_000,
        )
        payload = self._object(result, "gh issue view")
        payload["body"] = self._trim(payload.get("body"), 50_000)
        comments = payload.get("comments")
        if isinstance(comments, list):
            payload["comment_count"] = len(comments)
            payload["comments"] = [
                dict(item, body=self._trim(item.get("body"), 8_000))
                if isinstance(item, dict)
                else item
                for item in comments[-20:]
            ]
            payload["comments_truncated"] = len(comments) > 20
        return payload

    def _trim_pr(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload["body"] = self._trim(payload.get("body"), 50_000)
        for key, limit in {
            "files": 300,
            "commits": 100,
            "statusCheckRollup": 100,
            "reviews": 50,
        }.items():
            value = payload.get(key)
            if isinstance(value, list) and len(value) > limit:
                payload[f"{key}_count"] = len(value)
                payload[key] = value[:limit]
                payload[f"{key}_truncated"] = True
        return payload

    def pr_read(self, cwd: Path, pr_number: int) -> dict[str, Any]:
        result = self.executor.run(
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
            output_limit=10_000_000,
        )
        return self._trim_pr(self._object(result, "gh pr view"))

    def find_pr(self, cwd: Path, branch: str) -> dict[str, Any] | None:
        result = self.executor.run(
            ["gh", "pr", "view", branch, "--json", "number,url,isDraft,state"],
            cwd=cwd,
            check=False,
        )
        return self._object(result, "gh pr view") if result.returncode == 0 else None

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
        for label in repo.pr_labels:
            argv.extend(["--label", label])
        for reviewer in repo.pr_reviewers:
            argv.extend(["--reviewer", reviewer])
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
                output_limit=2_000_000,
            ),
            "gh pr view",
        )

    def status(self, cwd: Path, branch: str) -> dict[str, Any]:
        result = self.executor.run(
            [
                "gh",
                "pr",
                "view",
                branch,
                "--json",
                "number,title,url,state,isDraft,mergeable,reviewDecision,statusCheckRollup",
            ],
            cwd=cwd,
            output_limit=10_000_000,
        )
        return self._trim_pr(self._object(result, "gh pr view"))

    def _pr_head_sha(self, cwd: Path, branch: str) -> str | None:
        result = self.executor.run(
            ["gh", "pr", "view", branch, "--json", "headRefOid", "--jq", ".headRefOid"],
            cwd=cwd,
            check=False,
            output_limit=512,
        )
        value = result.stdout.strip()
        return value.lower() if result.returncode == 0 and _FULL_SHA.fullmatch(value) else None

    def _check_runs_for_head(self, cwd: Path, head_sha: str) -> list[GitHubCheckRun]:
        payload = self._api_object(
            cwd,
            f"repos/{self._slug(cwd)}/commits/{head_sha}/check-runs",
            fields=(("per_page", "100"), ("filter", "latest")),
            context="GitHub commit check runs",
        )
        raw_runs = payload.get("check_runs")
        if not isinstance(raw_runs, list):
            raise CommandError("GitHub commit check runs returned no check_runs list")
        return [self._parse_check_run(item) for item in raw_runs if isinstance(item, dict)]

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
        result = self.executor.run(argv, cwd=cwd, check=False, output_limit=5_000_000)
        if result.returncode not in (0, 1, 8):
            raise CommandError(result.combined)
        checks = self._list(result, "gh pr checks")
        head_sha = self._pr_head_sha(cwd, branch)
        if head_sha is None:
            return [dict(item, selector_available=False) for item in checks]
        try:
            runs = self._check_runs_for_head(cwd, head_sha)
        except CommandError:
            return [dict(item, head_sha=head_sha, selector_available=False) for item in checks]

        by_url = {candidate.details_url: candidate for candidate in runs if candidate.details_url}
        by_name: dict[str, list[GitHubCheckRun]] = {}
        for candidate in runs:
            by_name.setdefault(candidate.name, []).append(candidate)

        enriched: list[dict[str, Any]] = []
        for item in checks:
            link = self._string(item.get("link"))
            name = self._string(item.get("name"))
            matched_run: GitHubCheckRun | None = by_url.get(link)
            if matched_run is None and len(by_name.get(name, [])) == 1:
                matched_run = by_name[name][0]
            updated = dict(item, head_sha=head_sha, selector_available=matched_run is not None)
            if matched_run is not None:
                updated.update(
                    {
                        "selector": f"check-run:{matched_run.check_run_id}",
                        "check_run_id": matched_run.check_run_id,
                        "head_sha": matched_run.head_sha,
                        "stale": matched_run.head_sha != head_sha,
                    }
                )
            enriched.append(updated)
        return enriched

    @classmethod
    def _parse_check_run(cls, payload: dict[str, Any]) -> GitHubCheckRun:
        check_run_id = cls._integer(payload.get("id"))
        if check_run_id is None:
            raise CommandError("GitHub Check Run returned an invalid id")
        output_raw = payload.get("output")
        output: dict[str, Any] = output_raw if isinstance(output_raw, dict) else {}
        app_raw = payload.get("app")
        app: dict[str, Any] = app_raw if isinstance(app_raw, dict) else {}
        details_url = cls._string(payload.get("details_url"))
        match = _ACTIONS_JOB_URL.fullmatch(details_url)
        run_id = int(match.group(1)) if match else None
        job_id = int(match.group(3)) if match else None
        annotations_count = cls._integer(output.get("annotations_count")) or 0
        conclusion_raw = payload.get("conclusion")
        conclusion = conclusion_raw if isinstance(conclusion_raw, str) else None
        return GitHubCheckRun(
            check_run_id=check_run_id,
            name=cls._string(payload.get("name")),
            head_sha=cls._string(payload.get("head_sha")).lower(),
            status=cls._string(payload.get("status")),
            conclusion=conclusion,
            details_url=details_url,
            source_url=cls._string(payload.get("html_url")) or details_url,
            started_at=cls._string(payload.get("started_at")),
            completed_at=cls._string(payload.get("completed_at")),
            app_name=cls._string(app.get("name")),
            output_title=cls._string(output.get("title")),
            output_summary=cls._string(output.get("summary")),
            output_text=cls._string(output.get("text")),
            annotations_count=annotations_count,
            run_id=run_id,
            job_id=job_id,
        )

    def check_run(self, cwd: Path, check_run_id: int) -> GitHubCheckRun:
        payload = self._api_object(
            cwd,
            f"repos/{self._slug(cwd)}/check-runs/{check_run_id}",
            context="GitHub Check Run",
        )
        return self._parse_check_run(payload)

    def check_annotations(
        self,
        cwd: Path,
        check_run_id: int,
        *,
        max_annotations: int,
    ) -> tuple[list[GitHubCheckAnnotation], bool]:
        payload = self._api_list(
            cwd,
            f"repos/{self._slug(cwd)}/check-runs/{check_run_id}/annotations",
            fields=(("per_page", "100"),),
            context="GitHub Check Run annotations",
        )
        annotations: list[GitHubCheckAnnotation] = []
        for item in payload[:max_annotations]:
            start_line = self._integer(item.get("start_line"))
            end_line = self._integer(item.get("end_line"))
            annotations.append(
                GitHubCheckAnnotation(
                    path=self._string(item.get("path")),
                    start_line=start_line,
                    end_line=end_line,
                    level=self._string(item.get("annotation_level")),
                    title=self._string(item.get("title")),
                    message=self._string(item.get("message")),
                    raw_details=self._string(item.get("raw_details")),
                )
            )
        return annotations, len(payload) > max_annotations

    def actions_job(self, cwd: Path, job_id: int) -> GitHubActionsJob:
        payload = self._api_object(
            cwd,
            f"repos/{self._slug(cwd)}/actions/jobs/{job_id}",
            context="GitHub Actions job",
        )
        parsed_job_id = self._integer(payload.get("id"))
        if parsed_job_id is None:
            raise CommandError("GitHub Actions job returned an invalid id")
        raw_steps = payload.get("steps")
        steps: list[GitHubActionsStep] = []
        if isinstance(raw_steps, list):
            for item in raw_steps[:100]:
                if not isinstance(item, dict):
                    continue
                conclusion_raw = item.get("conclusion")
                steps.append(
                    GitHubActionsStep(
                        number=self._integer(item.get("number")),
                        name=self._string(item.get("name")),
                        status=self._string(item.get("status")),
                        conclusion=conclusion_raw if isinstance(conclusion_raw, str) else None,
                    )
                )
        conclusion_raw = payload.get("conclusion")
        return GitHubActionsJob(
            job_id=parsed_job_id,
            run_id=self._integer(payload.get("run_id")),
            attempt=self._integer(payload.get("run_attempt")),
            name=self._string(payload.get("name")),
            status=self._string(payload.get("status")),
            conclusion=conclusion_raw if isinstance(conclusion_raw, str) else None,
            source_url=self._string(payload.get("html_url")),
            steps=tuple(steps),
        )

    def actions_job_log(self, cwd: Path, job_id: int, *, max_chars: int) -> GitHubJobLog:
        result = self._api_result(
            cwd,
            f"repos/{self._slug(cwd)}/actions/jobs/{job_id}/logs",
            output_limit=max_chars,
            check=False,
        )
        if result.returncode != 0:
            raise CommandError(result.combined or "GitHub Actions job log is unavailable")
        truncated = "characters omitted" in result.stdout
        return GitHubJobLog(result.stdout, truncated)
