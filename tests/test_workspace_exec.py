"""Coverage for issue #376: `workspace_exec`, a first-class ad-hoc command tool
superseding `workspace_verify(mode="adhoc")` for the run-a-command intent.

`workspace_exec` promotes the SAME underlying machinery `tests/test_workspace_adhoc.py`
already covers in depth (`WorkspaceAdhocRunner`, `classify_adhoc_command`, config
parsing/validation) rather than reimplementing it, so this file does not re-prove that
machinery from scratch. It exists to prove the NEW surface: that `workspace_exec` reaches
the same guards through the durable admission queue (like `workspace_verify(mode="adhoc")`,
unlike the self-admitting `workspace_run_adhoc` service method), and that its own leaner
output contract projects the right evidence.

Every call goes through the durable work queue, exactly like `workspace_verify(mode="adhoc")`
-- a foreground call bounded-waits on its operation, so a worker has to claim the item for the
call to return a terminal result (`durable_worker`, same helper `test_workspace_adhoc.py`
uses for its own `_verify_adhoc` calls).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment, durable_worker

from repoforge.contracts.v2 import WorkspaceExecInput
from repoforge.domain.adhoc import MAX_ADHOC_STDIN_LENGTH
from repoforge.domain.errors import ErrorCode, RepoForgeError


def _relaxed_env(
    tmp_path: Path,
    *,
    runners: tuple[str, ...] = ("python3",),
    require_verification: bool = True,
) -> ForgeEnvironment:
    return create_forge_environment(
        tmp_path,
        require_verification=require_verification,
        execution_mode="relaxed",
        adhoc_runners=runners,
    )


def _exec(
    env: ForgeEnvironment, workspace_id: str, argv: tuple[str, ...], **kwargs: object
) -> dict:
    with durable_worker(env.service):
        return env.service.workspace_exec(workspace_id, argv, **kwargs)


def _audit_events(root: Path, action: str) -> list[dict[str, object]]:
    audit_path = root / "state" / "audit.jsonl"
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line
    ]
    return [event for event in events if event["action"] == action]


# ---------------------------------------------------------------------------
# Contract-level exact-state-lock validation (the enforcement point for
# workspace_exec differs from workspace_run_adhoc's: the durable admission
# layer always substitutes a freshly captured head_sha/fingerprint into the
# queued work request regardless of what the caller passed, so there is no
# application-layer "missing lock" refusal to reach through the MCP surface --
# the pydantic model is what requires the caller to have looked at all.)
# ---------------------------------------------------------------------------


def test_mutability_workspace_requires_exact_state_lock_fields() -> None:
    with pytest.raises(ValueError, match="requires expected_head_sha and expected_fingerprint"):
        WorkspaceExecInput(workspace_id="ws-1", argv=("git", "status"), mutability="workspace")


def test_mutability_workspace_accepts_the_lock_fields() -> None:
    model = WorkspaceExecInput(
        workspace_id="ws-1",
        argv=("git", "status"),
        mutability="workspace",
        expected_head_sha="a" * 40,
        expected_fingerprint="b" * 64,
    )
    assert model.mutability == "workspace"


# ---------------------------------------------------------------------------
# Strict-mode refusal
# ---------------------------------------------------------------------------


def test_strict_repo_refuses_exec(forge_env: ForgeEnvironment) -> None:
    created = forge_env.service.workspace_create("demo", "strict exec refusal")
    workspace_id = created["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        _exec(forge_env, workspace_id, ("python3", "--version"))
    assert exc.value.code is ErrorCode.EXECUTION_MODE_STRICT


# ---------------------------------------------------------------------------
# Relaxed-mode execution and evidence
# ---------------------------------------------------------------------------


def test_relaxed_repo_runs_allowlisted_command_as_evidence_only(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "exec happy path")["workspace_id"]

    result = _exec(
        env,
        workspace_id,
        ("python3", "-c", "from pathlib import Path; assert Path('hello.txt').exists()"),
    )

    assert result["outcome"] == "passed"
    assert result["commands"][0]["returncode"] == 0
    assert result["execution_evidence"]["requested_filesystem"] == "workspace_write"
    assert result["execution_evidence"]["effective_filesystem"] == "host_account_access"
    assert result["satisfies_commit_gate"] is False
    assert result["adhoc_evidence"]["network_policy"] == "advisory_local_only"
    assert result["adhoc_evidence"]["fingerprint_changed"] is False


def test_disallowed_runner_yields_structured_error(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "disallowed runner")["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, ("node", "--version"))
    assert exc.value.code is ErrorCode.ADHOC_RUNNER_NOT_ALLOWED


def test_argv_over_bound_is_rejected(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "argv too long")["workspace_id"]
    argv = ("python3", *[f"--flag{i}" for i in range(40)])
    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, argv)
    assert exc.value.code is ErrorCode.ADHOC_ARGV_INVALID


def test_empty_argv_is_rejected(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "argv empty")["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, ())
    assert exc.value.code is ErrorCode.ADHOC_ARGV_INVALID


def test_mutating_command_reports_fingerprint_change_and_paths(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    created = env.service.workspace_create("demo", "mutating exec")
    workspace_id = created["workspace_id"]
    workspace_path = Path(created["path"])

    result = _exec(
        env,
        workspace_id,
        ("python3", "-c", "from pathlib import Path; Path('hello.txt').write_text('changed\\n')"),
    )

    assert result["adhoc_evidence"]["fingerprint_changed"] is True
    assert "hello.txt" in result["adhoc_evidence"]["changed_paths"]
    assert (workspace_path / "hello.txt").read_text() == "changed\n"


# ---------------------------------------------------------------------------
# The hard constraint: exec never satisfies the verification-before-commit gate.
# ---------------------------------------------------------------------------


def test_exec_never_satisfies_commit_gate(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, require_verification=True)
    created = env.service.workspace_create("demo", "exec gate regression")
    workspace_id = created["workspace_id"]

    result = _exec(
        env,
        workspace_id,
        ("python3", "-c", "from pathlib import Path; Path('hello.txt').write_text('changed\\n')"),
    )
    assert result["outcome"] == "passed"
    assert result["satisfies_commit_gate"] is False

    with pytest.raises(RepoForgeError) as exc:
        env.service.workspace_commit(workspace_id, "attempt commit without verification")
    assert "verification" in str(exc.value).lower()

    env.service.workspace_run_profile(workspace_id, "full")
    committed = env.service.workspace_commit(workspace_id, "verified commit")
    assert committed["verified_profile"] == "full"


def test_exec_invalidates_stale_verification_receipt(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, require_verification=True)
    created = env.service.workspace_create("demo", "exec invalidates verification")
    workspace_id = created["workspace_id"]

    Path(created["path"], "hello.txt").write_text("changed\n", encoding="utf-8")
    env.service.workspace_run_profile(workspace_id, "full")

    result = _exec(
        env,
        workspace_id,
        (
            "python3",
            "-c",
            "from pathlib import Path; Path('hello.txt').write_text('changed twice\\n')",
        ),
    )
    assert result["adhoc_evidence"]["verification_invalidated"] is True
    with pytest.raises(RepoForgeError):
        env.service.workspace_commit(workspace_id, "should still require re-verification")


# ---------------------------------------------------------------------------
# Git content-inspection, exact-state lock, mutability modes
# ---------------------------------------------------------------------------


def test_read_only_git_command_runs_and_is_classified(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "git status")["workspace_id"]
    result = _exec(env, workspace_id, ("git", "status", "--porcelain=v2"))
    assert result["commands"][0]["returncode"] == 0
    evidence = result["adhoc_evidence"]
    assert evidence["command_class"] == "read_only"
    assert evidence["mutability"] == "read_only"
    assert evidence["content_inspected"] is True
    assert evidence["read_only_violation"] is False


def test_blocked_git_form_fails_via_the_durable_path(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "force push blocked")["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, ("git", "push", "--force", "origin", "main"))
    assert exc.value.code is ErrorCode.ADHOC_COMMAND_FORBIDDEN


def test_mutating_git_command_under_read_only_is_rejected(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "merge needs lock")["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, ("git", "merge", "origin/main"))
    assert exc.value.code is ErrorCode.ADHOC_ARGV_INVALID
    assert "mutability='workspace'" in str(exc.value)


def test_stale_expected_head_sha_fails_closed(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "stale head")["workspace_id"]
    status = env.service.workspace_status(workspace_id)
    with pytest.raises(RepoForgeError) as exc:
        _exec(
            env,
            workspace_id,
            ("git", "checkout", "-b", "ai/x"),
            mutability="workspace",
            expected_head_sha="0" * 40,
            expected_fingerprint=status["workspace_fingerprint"],
        )
    assert exc.value.code is ErrorCode.STALE_STATE


def test_mutating_git_command_runs_with_correct_lock(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",), require_verification=False)
    workspace_id = env.service.workspace_create("demo", "locked mutation")["workspace_id"]
    status = env.service.workspace_status(workspace_id)
    result = _exec(
        env,
        workspace_id,
        ("git", "checkout", "-b", "ai/scratch"),
        mutability="workspace",
        expected_head_sha=status["head_sha"],
        expected_fingerprint=status["workspace_fingerprint"],
    )
    assert result["commands"][0]["returncode"] == 0
    assert result["adhoc_evidence"]["command_class"] == "mutating"
    assert result["adhoc_evidence"]["mutability"] == "workspace"


def test_invalid_mutability_value_is_rejected(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "bad mutability")["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, ("git", "status"), mutability="destroy")
    assert exc.value.code is ErrorCode.ADHOC_ARGV_INVALID


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_exec_call_is_audited(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "exec audit")["workspace_id"]
    _exec(env, workspace_id, ("python3", "--version"))

    exec_events = _audit_events(env.root, "workspace_exec")
    assert exec_events, "expected at least one workspace_exec audit event"
    assert exec_events[-1]["details"]["mutability"] == "read_only"

    # The detailed argv/runner/stdin evidence is recorded once, under the shared
    # "workspace_run_adhoc" action every surface that reaches WorkspaceAdhocRunner
    # writes to (workspace_run_adhoc, workspace_verify mode=adhoc, and workspace_exec
    # alike) -- workspace_exec's own audit event is the outer envelope, not a second
    # copy of that detail.
    adhoc_events = _audit_events(env.root, "workspace_run_adhoc")
    assert adhoc_events, "expected the shared adhoc runner to record its own detailed event"
    assert adhoc_events[-1]["details"]["runner"] == "python3"


# ---------------------------------------------------------------------------
# Standard input
# ---------------------------------------------------------------------------


def test_exec_can_read_supplied_standard_input(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "exec stdin")["workspace_id"]

    result = _exec(
        env,
        workspace_id,
        ("python3", "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"),
        stdin_text="fed through stdin\n",
    )

    assert result["commands"][0]["returncode"] == 0
    assert "FED THROUGH STDIN" in result["commands"][0]["output_excerpt"]


def test_exec_stdin_is_bounded(tmp_path: Path) -> None:
    """The durable work queue's own state-size ceiling is reached before the
    adhoc-specific stdin-length guard (`ADHOC_ARGV_INVALID`) ever runs -- unlike
    `workspace_run_adhoc`'s self-admitting path (test_workspace_adhoc.py's
    `test_adhoc_stdin_is_bounded`), which validates before anything is persisted.
    Both fail closed; this asserts what is actually true for the durable path
    rather than assuming parity with the other one."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "exec stdin bound")["workspace_id"]

    with pytest.raises(RepoForgeError) as exc:
        _exec(
            env,
            workspace_id,
            ("python3", "-c", "pass"),
            stdin_text="x" * (MAX_ADHOC_STDIN_LENGTH + 1),
        )

    assert exc.value.code is ErrorCode.STATE_TOO_LARGE


def test_exec_audit_records_stdin_length_but_never_its_content(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "exec stdin audit")["workspace_id"]
    secret = "correct-horse-battery-staple"

    _exec(
        env,
        workspace_id,
        ("python3", "-c", "import sys; sys.stdin.read()"),
        stdin_text=secret,
    )

    events = _audit_events(env.root, "workspace_run_adhoc")
    assert events, "the exec run must be audited under the shared adhoc-runner event"
    assert events[-1]["details"]["stdin_length"] == len(secret)
    assert secret not in json.dumps(events[-1])
