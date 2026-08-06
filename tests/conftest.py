from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from repoforge.adapters.execution.native import NativeReviewedAdapter
from repoforge.application.execution.coordinator import ExecutionCoordinator
from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import AppConfig, load_config
from repoforge.domain.errors import CommandError
from repoforge.domain.mutation_policy import MUTATION_OPS
from repoforge.ports.clock import Clock
from repoforge.ports.workspace_publication import (
    WorkspaceDraftPrPublication,
    WorkspaceDraftPrPublicationEffect,
    WorkspacePushPublication,
    WorkspacePushPublicationEffect,
)
from repoforge.testing import ScriptedCommandExecutor

_REAL_HOME = Path(os.environ.get("HOME", "~")).expanduser()
# Paths the operator's REAL installation owns. A unit test that writes any of these has
# escaped its tmp_path, and the consequence is not hypothetical: an earlier revision that
# provisioned the PATH launcher unconditionally left `~/.local/bin/rf` exec-ing a deleted
# pytest tmpdir, which broke the `rf` command on a developer machine until it was repaired
# by hand. Tests must redirect HOME (or grant an explicit path) instead.
TEST_CONFIG_GENERATION = 1
"""The configuration generation every test environment serves.

Positive on purpose. In production the serving process always knows its generation and
stamps it into durable work; a fixture at the default 0 made the suite agree with itself
while disagreeing with production -- request side and worker side both 0, so the generation
filter always matched and nothing could be unclaimable. That is exactly why #313 (request
side 0, worker side 12) was invisible here.
"""


_PROTECTED_REAL_PATHS = (
    _REAL_HOME / ".local" / "bin" / "rf",
    _REAL_HOME / ".local" / "share" / "repoforge" / "bin",
    _REAL_HOME / ".local" / "share" / "repoforge" / "current",
    _REAL_HOME / ".local" / "share" / "repoforge" / "previous",
    _REAL_HOME / ".local" / "share" / "repoforge" / "runtime",
    _REAL_HOME / "Library" / "LaunchAgents" / "dev.repoforge.supervisor.plist",
    # The state ROOT itself is deliberately not listed: the live runtime rewrites the audit
    # log, metrics and operation records continuously, so its fingerprint would flip no
    # matter what the suite did. This subdirectory is different -- it gains an entry only
    # when something resolves a NEW config path against the real state root, which is
    # exactly the leak that accumulated 640 directories of test residue (#318). Comparing
    # the entry listing catches that without descending into anything the runtime mutates.
    _REAL_HOME / ".local" / "state" / "repoforge" / "config-locks",
)


def _describe_protected(path: Path) -> str:
    """One protected path's observable state, including what a symlink RESOLVES to.

    Recording only the link target would miss the most damaging case: ``~/.local/bin/rf``
    is normally a symlink into a tool venv, so a test writing to that path writes THROUGH
    the link and corrupts the real entry point while the link itself looks untouched.
    """
    try:
        if path.is_symlink():
            target = os.readlink(path)
            return f"symlink:{target}->{_describe_protected(path.resolve())}"
        if path.is_file():
            info = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            return f"file:{info.st_size}:{info.st_mode:o}:{digest}"
        if path.is_dir():
            return f"dir:{sorted(entry.name for entry in path.iterdir())}"
    except OSError as exc:
        return f"unreadable:{exc.errno}"
    return "absent"


def _protected_fingerprint() -> dict[str, str]:
    """State of every protected path, cheap enough to sample around each test."""
    return {str(path): _describe_protected(path) for path in _PROTECTED_REAL_PATHS}


@pytest.fixture(autouse=True)
def _never_touch_the_real_installation() -> Iterator[None]:
    """Fail when the developer's real RepoForge installation changed during a test.

    Deliberately autouse and unconditional: the failure mode is silent -- the test passes,
    and the damage only surfaces later when `rf` no longer runs -- so the only useful place
    to catch it is the moment it happens.

    It cannot tell WHO wrote: a genuine `rf upgrade --activate` running in another terminal
    while the suite runs also trips it, on whichever test happened to be in flight (observed
    exactly once, and the message used to blame that innocent test). So the message names
    both causes; on CI, where nothing else touches the runner's home, only the first applies.
    """
    before = _protected_fingerprint()
    yield
    after = _protected_fingerprint()
    changed = [key for key, value in after.items() if before.get(key) != value]
    assert not changed, (
        "the operator's REAL installation changed while this test ran: "
        + ", ".join(f"{key} ({before.get(key)} -> {after[key]})" for key in changed)
        + ". Either this test escaped tmp_path -- redirect HOME to tmp_path, or pass an "
        "explicit path inside it -- or a real `rf` activation ran concurrently on this "
        "machine, in which case re-run the suite once it has finished."
    )


def git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return completed.stdout.strip()


@dataclass(frozen=True)
class ForgeEnvironment:
    root: Path
    remote: Path
    source: Path
    fake_bin: Path
    gh_state: Path
    config_path: Path
    service: CodingService


class _FixtureWorkspacePublicationService:
    """Exercise exact workspace publication contracts against fixture-only adapters."""

    def __init__(self, context: object) -> None:
        self._context = context

    @staticmethod
    def _identities(seed: str) -> tuple[str, str, str, str]:
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        operation_id = "op-" + digest[:24]
        return (
            "publication-" + digest[24:48],
            operation_id,
            "receipt-" + digest[40:64],
            f"operation-result:{operation_id}",
        )

    def push(self, request: WorkspacePushPublication) -> WorkspacePushPublicationEffect:
        context = self._context
        branch = request.destination_ref.removeprefix("refs/heads/")
        reconciled = False
        output = "fixture exact publication"
        try:
            result = context.git.push(
                request.cwd,
                request.remote,
                branch,
                context.config.server.verification_timeout_seconds,
            )
            output = result.combined or output
        except CommandError:
            remote_after = context.git.remote_branch_sha(
                request.cwd,
                request.remote,
                branch,
                context.config.server.verification_timeout_seconds,
            )
            if remote_after != request.head_sha:
                raise
            reconciled = True
            output = "fixture exact publication reconciled after lost response"
        remote_after = context.git.remote_branch_sha(
            request.cwd,
            request.remote,
            branch,
            context.config.server.verification_timeout_seconds,
        )
        publication_id, operation_id, receipt_id, result_reference = self._identities(
            request.idempotency_key or f"{request.workspace_id}:{request.head_sha}"
        )
        return WorkspacePushPublicationEffect(
            publication_id=publication_id,
            operation_id=operation_id,
            receipt_id=receipt_id,
            result_reference=result_reference,
            head_sha=request.head_sha,
            remote_head_before=request.remote_head_before,
            remote_head_after=remote_after or request.head_sha,
            pushed=True,
            reconciled=reconciled,
            output=output,
        )

    def create_draft_pr(
        self,
        request: WorkspaceDraftPrPublication,
    ) -> WorkspaceDraftPrPublicationEffect:
        context = self._context
        base = request.base_ref.removeprefix("refs/heads/")
        branch = request.head_ref.removeprefix("refs/heads/")
        existing = context.github.find_pr(request.cwd, branch)
        reconciled = existing is not None
        if existing is not None:
            if existing.get("title") != request.title or existing.get("body") != request.body:
                existing = context.github.update(
                    request.cwd,
                    branch,
                    title=request.title,
                    body=request.body,
                )
            url = existing.get("url")
            if not isinstance(url, str) or not url:
                raise AssertionError("fixture pull request has no URL")
        else:
            url = context.github.create_draft(
                request.cwd,
                context.repo(request.repo_id),
                branch=branch,
                base=base,
                title=request.title,
                body=request.body,
            )
        publication_id, operation_id, receipt_id, result_reference = self._identities(
            request.idempotency_key
            or f"{request.workspace_id}:{request.base_ref}:{request.head_ref}:{request.head_sha}"
        )
        return WorkspaceDraftPrPublicationEffect(
            publication_id=publication_id,
            operation_id=operation_id,
            receipt_id=receipt_id,
            result_reference=result_reference,
            url=url,
            reconciled=reconciled,
        )


def execution_coordinator_for_tests() -> ExecutionCoordinator:
    """Provide required deterministic execution wiring to non-execution unit fixtures."""

    return ExecutionCoordinator(NativeReviewedAdapter(ScriptedCommandExecutor()))


class StubWorkHandlers:
    """Record each claimed work item and answer with a caller-supplied result.

    `workspace_verify` admits durable work instead of calling a runner, so a test
    that used to monkeypatch `WorkspaceProfileRunner.execute` now substitutes the
    worker's handler. `claimed` holds the exact `OperationWorkItem`, whose
    `request` is the reviewed command the old assertions inspected.
    """

    def __init__(self, results: dict[str, object]) -> None:
        self._results = results
        self.claimed: list[object] = []

    def execute(self, item, *, cancellation_token, progress):
        del cancellation_token, progress
        self.claimed.append(item)
        kind = item.request.kind
        result = self._results.get(kind)
        if result is None:
            raise AssertionError(f"durable work of kind {kind!r} must not run in this test")
        return result(item) if callable(result) else result


@contextlib.contextmanager
def durable_worker(
    service: CodingService,
    *,
    handlers: object | None = None,
    profile: object | None = None,
    diagnostic: object | None = None,
    adhoc: object | None = None,
) -> Iterator[object]:
    """Run one durable execution worker in a thread for the body of the block.

    A foreground `workspace_verify` bounded-waits on its operation, so something
    must claim the work for the call to return a terminal result. With no
    arguments the worker executes with the production runners against this
    service's own configuration; pass per-kind results or callables to stand in
    for the command instead. The worker is always stopped and joined on exit.
    """

    from repoforge.application.operations.work_executor import VerificationWorkHandlers
    from repoforge.application.operations.work_loop import OperationWorkLoop
    from repoforge.application.workspace.run_adhoc import WorkspaceAdhocRunner
    from repoforge.application.workspace.run_diagnostic import WorkspaceDiagnosticRunner
    from repoforge.application.workspace.run_profile import WorkspaceProfileRunner

    stubbed = {
        key: value
        for key, value in (("profile", profile), ("diagnostic", diagnostic), ("adhoc", adhoc))
        if value is not None
    }
    if handlers is None:
        context = service.application.context
        handlers = (
            StubWorkHandlers(stubbed)
            if stubbed
            else VerificationWorkHandlers(
                WorkspaceProfileRunner(context),
                WorkspaceAdhocRunner(context),
                WorkspaceDiagnosticRunner(context),
            )
        )
    loop = OperationWorkLoop(
        service.application.context,
        service.application.operations,
        handlers,
        idle_poll_seconds=0.01,
        heartbeat_interval_seconds=0.5,
        recovery_interval_seconds=3_600.0,
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=lambda: loop.run_until_stopped(stop),
        name="test-durable-worker",
        daemon=True,
    )
    thread.start()
    try:
        yield handlers
    finally:
        stop.set()
        loop.request_stop()
        thread.join(timeout=60)
        assert not thread.is_alive(), "the durable test worker did not stop"


_TEMPLATE_LOCK = threading.Lock()
_TEMPLATE_ROOT: Path | None = None


def _build_template_repo() -> Path:
    """Build the base remote+source git repos exactly once per process.

    create_forge_environment() previously ran ~10 git subprocesses per call
    (init/clone/config/add/commit/branch/push) -- ~257ms x ~574 tests. The repo
    content is identical across every test (task-specific state is layered on
    later through the service), so we build it once here and copy it per test.
    """
    root = Path(tempfile.mkdtemp(prefix="forge-template-"))
    remote = root / "remote.git"
    git("init", "--bare", str(remote), cwd=root)
    source = root / "source"
    git("clone", str(remote), str(source), cwd=root)
    git("config", "user.name", "Test User", cwd=source)
    git("config", "user.email", "test@example.com", cwd=source)
    (source / "hello.txt").write_text("hello\n", encoding="utf-8")
    (source / "README.md").write_text("# Demo\n\nRepository instructions.\n", encoding="utf-8")
    (source / "AGENTS.md").write_text("Always test changes.\n", encoding="utf-8")
    (source / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "packageManager": "pnpm@10.20.0",
                "engines": {"node": "22.23.1"},
                "scripts": {"test": "echo test", "check": "echo check"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    git("add", ".", cwd=source)
    git("commit", "-m", "initial", cwd=source)
    git("branch", "-M", "main", cwd=source)
    git("push", "-u", "origin", "main", cwd=source)
    return root


def _template_root() -> Path:
    global _TEMPLATE_ROOT
    with _TEMPLATE_LOCK:
        if _TEMPLATE_ROOT is None:
            _TEMPLATE_ROOT = _build_template_repo()
            atexit.register(shutil.rmtree, _TEMPLATE_ROOT, ignore_errors=True)
        return _TEMPLATE_ROOT


def _clone_template_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Copy the prebuilt template into this test's tmp_path and re-point origin.

    A filesystem copy (~10ms) replaces the ~10 git subprocesses the original
    inline setup ran; only `remote set-url` stays as a single git call so the
    copied source pushes/fetches against the copied remote, not the shared
    template.
    """
    template = _template_root()
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    shutil.copytree(template / "remote.git", remote)
    shutil.copytree(template / "source", source)
    git("remote", "set-url", "origin", str(remote), cwd=source)
    return remote, source


def _write_fake_gh(fake_bin: Path, state_path: Path) -> None:
    script = fake_bin / "gh"
    script.write_text(
        f"""#!/usr/bin/env python3
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

STATE = Path({str(state_path)!r})

def load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {{"prs": {{}}}}

def save(data):
    STATE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\\n")

def branch():
    return subprocess.run(
        ["git", "branch", "--show-current"], check=True, capture_output=True, text=True
    ).stdout.strip()

def head_sha():
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

def arg_value(args, flag, default=None):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return default

args = sys.argv[1:]
if args == ["--version"]:
    print("gh version 2.80.0 (fake)")
    raise SystemExit(0)
if args[:2] == ["auth", "status"]:
    print("Logged in to github.com as test-user")
    raise SystemExit(0)
if args[:2] == ["auth", "setup-git"]:
    print("Configured git protocol")
    raise SystemExit(0)
if args[:2] == ["repo", "view"]:
    if "--jq" in args:
        print("owner/demo")
    else:
        print(json.dumps({{"nameWithOwner": "owner/demo"}}))
    raise SystemExit(0)
if args[:2] == ["issue", "view"]:
    number = int(args[2])
    data = load()
    override = (data.get("issues") or {{}}).get(str(number), {{}})
    payload = {{
        "number": number,
        "title": "Implement safer workflow",
        "body": "Issue body",
        "state": "OPEN",
        "author": {{"login": "test-user"}},
        "labels": [{{"name": "enhancement"}}],
        "assignees": [],
        "url": f"https://github.com/owner/demo/issues/{{number}}",
        "comments": [{{
            "body": (
                "context\\n\\nObjective: implement the ticket.\\n"
                "Acceptance criteria: behavior is verified.\\n"
                "Tests: run the production gate."
            ),
            "author": {{"login": "reviewer"}},
        }}],
    }}
    payload.update(override)
    print(json.dumps(payload))
    raise SystemExit(0)
if args[:2] == ["pr", "create"]:
    data = load()
    head = arg_value(args, "--head", branch())
    title = arg_value(args, "--title", "Draft PR")
    body = sys.stdin.read()
    pr = {{
        "number": 42,
        "title": title,
        "body": body,
        "url": "https://github.com/owner/demo/pull/42",
        "state": "OPEN",
        "isDraft": True,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "statusCheckRollup": [],
        "comments": [],
        "reviews": [],
        "updatedAt": "2026-07-21T14:00:00Z",
        "headRefOid": head_sha(),
    }}
    data.setdefault("prs", {{}})[head] = pr
    save(data)
    print(pr["url"])
    raise SystemExit(0)
if args[:2] == ["pr", "edit"]:
    data = load()
    ref = args[2]
    pr = data.setdefault("prs", {{}}).get(ref)
    if not pr:
        print("no pull request found", file=sys.stderr)
        raise SystemExit(1)
    title = arg_value(args, "--title")
    if title is not None:
        pr["title"] = title
    if "--body-file" in args:
        pr["body"] = sys.stdin.read()
    pr["updatedAt"] = "2026-07-21T14:01:00Z"
    save(data)
    raise SystemExit(0)
if args[:2] == ["pr", "checks"]:
    data = load()
    checks = data.get("checks") or [
        {{"name": "unit", "state": "SUCCESS", "bucket": "pass", "link": "https://github.com/owner/demo/actions/runs/1001/job/101", "workflow": "CI", "description": "ok", "startedAt": "", "completedAt": ""}},
        {{"name": "lint", "state": "SKIPPED", "bucket": "skipping", "link": "https://github.com/owner/demo/actions/runs/1002/job/102", "workflow": "CI", "description": "skipped", "startedAt": "", "completedAt": ""}},
    ]
    if "--required" in args:
        checks = [item for item in checks if item.get("required", True)]
    print(json.dumps(checks))
    raise SystemExit(0)
if args[:2] == ["pr", "view"]:
    ref = args[2]
    data = load()
    # Numeric reads represent an existing GitHub PR independent of the workspace-created PR.
    if ref.isdigit():
        number = int(ref)
        print(json.dumps({{
            "number": number,
            "title": "Existing PR",
            "body": "Existing body",
            "state": "OPEN",
            "isDraft": False,
            "author": {{"login": "test-user"}},
            "baseRefName": "main",
            "headRefName": "feature",
            "url": f"https://github.com/owner/demo/pull/{{number}}",
            "files": [{{"path": "hello.txt"}}],
            "commits": [],
            "statusCheckRollup": [],
            "reviews": [],
        }}))
        raise SystemExit(0)
    pr = data.setdefault("prs", {{}}).get(ref)
    if not pr:
        print("no pull request found", file=sys.stderr)
        raise SystemExit(1)
    if "--jq" in args and ".headRefOid" in args:
        print(pr.get("headRefOid", head_sha()))
    else:
        view = dict(pr)
        view["comments"] = list(pr.get("comments", [])) + list(data.get("pr_comments", []))
        view["reviews"] = list(pr.get("reviews", [])) + list(data.get("pr_review_comments", []))
        if data.get("checks") is not None:
            view["statusCheckRollup"] = data["checks"]
        print(json.dumps(view))
    raise SystemExit(0)

if args and args[0] == "api":
    data = load()
    endpoint = ""
    skip_next = False
    for arg in args[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--method", "-H", "--header", "-f", "--raw-field", "-F", "--field", "--input", "--jq", "-q"):
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        endpoint = arg
        break
    method = arg_value(args, "--method", "GET")
    body_field = next((arg.split("=", 1)[1] for arg in args if arg.startswith("body=")), "")
    endpoint_path = endpoint.split("?", 1)[0]
    current_head = head_sha()
    if "/issues/" in endpoint_path and endpoint_path.endswith("/comments"):
        comments = data.setdefault("pr_comments", [])
        if method == "POST":
            item = {{"id": len(comments) + 1001, "body": body_field, "html_url": f"https://github.com/owner/demo/issues/42#issuecomment-{{len(comments) + 1001}}"}}
            comments.append(item)
            save(data)
            print(json.dumps(item))
        else:
            print(json.dumps(comments))
        raise SystemExit(0)
    if "/pulls/" in endpoint_path and endpoint_path.endswith("/comments"):
        print(json.dumps(data.get("pr_review_comments", [])))
        raise SystemExit(0)
    if "/pulls/comments/" in endpoint_path and endpoint_path.endswith("/replies"):
        comments = data.setdefault("pr_review_comments", [])
        item = {{"id": len(comments) + 2001, "body": body_field, "html_url": f"https://github.com/owner/demo/pull/42#discussion_r{{len(comments) + 2001}}"}}
        comments.append(item)
        save(data)
        print(json.dumps(item))
        raise SystemExit(0)
    if endpoint.endswith("/check-runs") and "/commits/" in endpoint:
        runs = data.get("check_runs") or {{
            "101": {{"id": 101, "name": "unit", "head_sha": current_head, "status": "completed", "conclusion": "success", "details_url": "https://github.com/owner/demo/actions/runs/1001/job/101", "html_url": "https://github.com/owner/demo/actions/runs/1001/job/101", "started_at": "", "completed_at": "", "output": {{"title": "", "summary": "", "text": "", "annotations_count": 0}}, "app": {{"name": "GitHub Actions"}}}},
            "102": {{"id": 102, "name": "lint", "head_sha": current_head, "status": "completed", "conclusion": "skipped", "details_url": "https://github.com/owner/demo/actions/runs/1002/job/102", "html_url": "https://github.com/owner/demo/actions/runs/1002/job/102", "started_at": "", "completed_at": "", "output": {{"title": "", "summary": "", "text": "", "annotations_count": 0}}, "app": {{"name": "GitHub Actions"}}}},
        }}
        print(json.dumps({{"total_count": len(runs), "check_runs": list(runs.values())}}))
        raise SystemExit(0)
    if "/check-runs/" in endpoint and endpoint.endswith("/annotations"):
        check_id = endpoint.split("/check-runs/", 1)[1].split("/", 1)[0]
        if data.get("annotations_permission_denied"):
            print("Resource not accessible by integration", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps((data.get("annotations") or {{}}).get(check_id, [])))
        raise SystemExit(0)
    if "/check-runs/" in endpoint:
        check_id = endpoint.rsplit("/", 1)[-1]
        runs = data.get("check_runs") or {{
            "101": {{"id": 101, "name": "unit", "head_sha": current_head, "status": "completed", "conclusion": "success", "details_url": "https://github.com/owner/demo/actions/runs/1001/job/101", "html_url": "https://github.com/owner/demo/actions/runs/1001/job/101", "started_at": "", "completed_at": "", "output": {{"title": "", "summary": "", "text": "", "annotations_count": 0}}, "app": {{"name": "GitHub Actions"}}}},
            "102": {{"id": 102, "name": "lint", "head_sha": current_head, "status": "completed", "conclusion": "skipped", "details_url": "https://github.com/owner/demo/actions/runs/1002/job/102", "html_url": "https://github.com/owner/demo/actions/runs/1002/job/102", "started_at": "", "completed_at": "", "output": {{"title": "", "summary": "", "text": "", "annotations_count": 0}}, "app": {{"name": "GitHub Actions"}}}},
        }}
        item = runs.get(check_id)
        if item is None:
            print("check run not found", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(item))
        raise SystemExit(0)
    if "/actions/jobs/" in endpoint and endpoint.endswith("/logs"):
        job_id = endpoint.split("/actions/jobs/", 1)[1].split("/", 1)[0]
        if data.get("logs_permission_denied"):
            print("Resource not accessible by integration", file=sys.stderr)
            raise SystemExit(1)
        log = (data.get("logs") or {{}}).get(job_id)
        if log is None:
            print("job log not found", file=sys.stderr)
            raise SystemExit(1)
        print(log, end="")
        raise SystemExit(0)
    if "/actions/jobs/" in endpoint:
        job_id = endpoint.rsplit("/", 1)[-1]
        job = (data.get("jobs") or {{}}).get(job_id)
        if job is None:
            print("job not found", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(job))
        raise SystemExit(0)

print("unsupported fake gh invocation: " + " ".join(args), file=sys.stderr)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)


def build_test_service(config: AppConfig, *, clock: Clock | None = None) -> CodingService:
    """Compose explicit fixture publication effects without changing production defaults."""

    application = build_application(
        config,
        overrides=AdapterOverrides(clock=clock) if clock is not None else None,
        config_generation=TEST_CONFIG_GENERATION,
    )
    object.__setattr__(
        application.context,
        "publications",
        _FixtureWorkspacePublicationService(application.context),
    )
    return CodingService(config, application=application)


def create_forge_environment(
    tmp_path: Path,
    *,
    max_batch_files: int = 20,
    max_changed_files: int = 20,
    require_verification: bool = True,
    clock: Clock | None = None,
    execution_mode: str = "strict",
    adhoc_runners: tuple[str, ...] = (),
    adhoc_shell_runners: tuple[str, ...] = (),
    execution_profiles: tuple[str, ...] = (),
    credential_profiles: tuple[str, ...] = (),
    allowed_mutation_ops: tuple[str, ...] = MUTATION_OPS,
    trusted_external_checkouts: dict[str, str] | None = None,
    trusted_host_enabled: bool = False,
    trusted_host_max_lease_ttl_seconds: int = 14_400,
) -> ForgeEnvironment:
    remote, source = _clone_template_repo(tmp_path)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    gh_state = tmp_path / "fake-gh-state.json"
    _write_fake_gh(fake_bin, gh_state)

    trusted_checkouts_lines = ["", "[repositories.demo.trusted_external_checkouts]"]
    for alias, checkout_path in (trusted_external_checkouts or {}).items():
        trusted_checkouts_lines.append(f"{json.dumps(alias)} = {json.dumps(checkout_path)}")
    trusted_checkouts_section = (
        "\n".join(trusted_checkouts_lines) if trusted_external_checkouts else ""
    )

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"
max_batch_files = {max_batch_files}
path_prefixes = ["{fake_bin}", "/usr/local/bin", "/usr/bin", "/bin"]

[repositories.demo]
path = "{source}"
display_name = "Demo Repository"
remote = "origin"
default_base = "main"
allowed_base_branches = ["main"]
branch_prefix = "ai/"
protected_branches = ["main", "master"]
require_verification_before_commit = {str(require_verification).lower()}
fetch_before_workspace = true
default_verification_profile = "full"
max_changed_files = {max_changed_files}
max_diff_lines = 1000
max_total_changed_bytes = 1000000
allowed_paths = []
denied_paths = [".git", ".git/**", ".env", ".github/workflows/**", "**/*.pem"]
allowed_mutation_ops = {json.dumps(list(allowed_mutation_ops))}
pr_labels = ["agent"]
pr_reviewers = ["reviewer"]
no_maintainer_edit = true
execution_mode = "{execution_mode}"
adhoc_runners = {json.dumps(list(adhoc_runners))}
adhoc_shell_runners = {json.dumps(list(adhoc_shell_runners))}
execution_profiles = {json.dumps(list(execution_profiles))}
credential_profiles = {json.dumps(list(credential_profiles))}
trusted_host_enabled = {str(trusted_host_enabled).lower()}
trusted_host_max_lease_ttl_seconds = {trusted_host_max_lease_ttl_seconds}
{trusted_checkouts_section}

[repositories.demo.profiles.quick]
description = "Fast non-gating check"
verification = false
commands = [["python3", "-c", "from pathlib import Path; assert Path('hello.txt').exists()"]]

[repositories.demo.profiles.full]
description = "Full verification"
verification = true
commands = [["python3", "-c", "from pathlib import Path; assert Path('hello.txt').read_text().startswith('changed')"]]

[repositories.demo.diagnostics.pytest-target]
summary = "Run one tracked pytest target"
argv = ["python3", "-c", "print('1 passed in 0.01s')", "{{selector}}"]
selector_kind = "pytest_node"
timeout_seconds = 30
network_policy = "local_only"
mutability = "read_only"
parser = "pytest"
output_limit = 2000

[repositories.demo.formatters.test-format]
summary = "Format changed text fixtures"
check_argv = ["python3", "-c", "import sys; from pathlib import Path; bad=[p for p in sys.argv[1:] if 'needs-format' in Path(p).read_text()]; [print('Would reformat: ' + p) for p in bad]; raise SystemExit(1 if bad else 0)"]
fix_argv = ["python3", "-c", "import sys; from pathlib import Path; [(lambda p: p.write_text(p.read_text().replace('needs-format', 'formatted')))(Path(x)) for x in sys.argv[1:]]"]
include_globs = ["*.txt", "**/*.txt"]
timeout_seconds = 30
output_limit = 2000
max_paths = 20
baseline_cache_ttl_seconds = 3600
network_policy = "local_only"
parser = "ruff_format"
''',
        encoding="utf-8",
    )
    config = load_config(config_path)
    # A positive generation, because in production the serving process ALWAYS knows the
    # generation it serves, and durable work is stamped with it. A fixture that built at
    # the default 0 made every test agree with itself while disagreeing with production:
    # both the request side and the worker side ran at 0, so the generation filter matched
    # and nothing was ever unclaimable. That is precisely why #313 -- request side 0,
    # worker side 12 -- was invisible to this suite.
    service = build_test_service(config, clock=clock)
    return ForgeEnvironment(
        root=tmp_path,
        remote=remote,
        source=source,
        fake_bin=fake_bin,
        gh_state=gh_state,
        config_path=config_path,
        service=service,
    )


@pytest.fixture
def forge_env(tmp_path: Path) -> ForgeEnvironment:
    return create_forge_environment(tmp_path)
