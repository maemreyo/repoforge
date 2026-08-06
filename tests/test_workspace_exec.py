"""Coverage for issue #376: `workspace_exec`, a first-class ad-hoc command tool
superseding `workspace_verify(mode="adhoc")` for the run-a-command intent. Also covers
#377 (reviewed shell-script execution, output-artifact references, non-interactive
default) and #443 (bounded fail-fast argv sequences), both additive extensions of the
same tool's contract.

`workspace_exec` promotes the SAME underlying machinery `tests/test_workspace_adhoc.py`
already covers in depth (`WorkspaceAdhocRunner`, `classify_adhoc_command`, config
parsing/validation) rather than reimplementing it, so this file does not re-prove that
machinery from scratch. It exists to prove the NEW surface: that `workspace_exec` reaches
the same guards it always did, and that its own leaner output contract projects the right
evidence.

Since #378, a default (background=False) call runs through the inline fast path -- it
calls `WorkspaceAdhocRunner` directly and never touches the durable work queue at all,
so `durable_worker` (still used below, exactly like `test_workspace_adhoc.py`'s own
`_verify_adhoc` calls) is a harmless no-op for these calls: nothing is ever queued for it
to claim. Only an explicit `background=True` call still goes through admission + a real
worker claim -- see `tests/test_workspace_exec_inline.py` for the tests that pin down the
inline/durable split itself (no durable record, ceiling fail-fast, framework_overhead_ms).
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment, durable_worker

from repoforge.adapters.execution.docker_adapter import DockerExecutionAdapter
from repoforge.adapters.execution.native import NativeReviewedAdapter
from repoforge.adapters.persistence.json_lease_store import JsonHostBypassLeaseStore
from repoforge.application.execution.routing import RoutingExecutionEnvironment
from repoforge.bootstrap import build_lock_manager
from repoforge.contracts.v2 import WorkspaceExecInput
from repoforge.domain.adhoc import MAX_ADHOC_STDIN_LENGTH
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.host_bypass_lease import HostBypassLease, mint_lease_token
from repoforge.ports.command import CommandResult


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


def _relaxed_shell_env(
    tmp_path: Path,
    *,
    shell_runners: tuple[str, ...] = ("sh",),
    runners: tuple[str, ...] = ("python3",),
) -> ForgeEnvironment:
    return create_forge_environment(
        tmp_path,
        execution_mode="relaxed",
        adhoc_runners=runners,
        adhoc_shell_runners=shell_runners,
    )


def _exec_script(
    env: ForgeEnvironment, workspace_id: str, script: str, shell: str, **kwargs: object
) -> dict:
    with durable_worker(env.service):
        return env.service.workspace_exec(workspace_id, script=script, shell=shell, **kwargs)


def _exec_sequence(
    env: ForgeEnvironment,
    workspace_id: str,
    argv_sequence: tuple[tuple[str, ...], ...],
    **kwargs: object,
) -> dict:
    with durable_worker(env.service):
        return env.service.workspace_exec(workspace_id, argv_sequence=argv_sequence, **kwargs)


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


def test_git_fetch_reports_credentialed_network_effect_and_default_mismatch(
    tmp_path: Path,
) -> None:
    """#382 AC1/AC2: fetch stays CommandClass.READ_ONLY (unchanged exact-state-lock
    behavior) but its EffectClass is credentialed_network, not read_only -- and under
    the default declared_effect (derived from mutability='read_only'), that is a
    mismatch worth reporting, not a silent match."""
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "git fetch")["workspace_id"]
    result = _exec(env, workspace_id, ("git", "fetch", "origin"))
    assert result["commands"][0]["returncode"] == 0
    evidence = result["adhoc_evidence"]
    assert evidence["command_class"] == "read_only"
    assert evidence["declared_effect"] == "read_only"
    assert evidence["observed_effect"] == "credentialed_network"
    assert evidence["effect_mismatch"] is True


def test_declaring_credentialed_network_for_fetch_clears_the_mismatch_flag(
    tmp_path: Path,
) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "git fetch declared")["workspace_id"]
    result = _exec(
        env, workspace_id, ("git", "fetch", "origin"), declared_effect="credentialed_network"
    )
    evidence = result["adhoc_evidence"]
    assert evidence["declared_effect"] == "credentialed_network"
    assert evidence["observed_effect"] == "credentialed_network"
    assert evidence["effect_mismatch"] is False


def test_git_push_reports_remote_write_effect_and_mismatch_under_workspace_declaration(
    tmp_path: Path,
) -> None:
    """#382 AC2: a plain mutability='workspace' declaration (EffectClass.WORKSPACE by
    default) does not cover a command that actually reaches a remote -- push must still
    surface as a mismatch unless the caller declares remote_write (or broader)."""
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "git push declared workspace")[
        "workspace_id"
    ]
    env_source_head = env.service.workspace_status(workspace_id)
    result = _exec(
        env,
        workspace_id,
        ("git", "push", "origin", "HEAD:refs/heads/ai/exec-push-test"),
        mutability="workspace",
        expected_head_sha=env_source_head["head_sha"],
        expected_fingerprint=env_source_head["workspace_fingerprint"],
    )
    evidence = result["adhoc_evidence"]
    assert evidence["declared_effect"] == "workspace"
    assert evidence["observed_effect"] == "remote_write"
    assert evidence["effect_mismatch"] is True


def test_declared_effect_does_not_change_which_commands_are_admitted_or_blocked(
    tmp_path: Path,
) -> None:
    """#382 AC4, end to end: the same argv is admitted or blocked identically regardless
    of declared_effect. Declaring destructive_remote for git status does not gate it
    behind anything new, and declaring read_only for a blocked force-push does not let
    it through -- declared_effect is reporting, never authorization."""
    env = _relaxed_env(tmp_path, runners=("git",))
    allowed_id = env.service.workspace_create("demo", "status any declared effect")["workspace_id"]
    for declared in ("read_only", "destructive_remote"):
        result = _exec(
            env, allowed_id, ("git", "status", "--porcelain=v2"), declared_effect=declared
        )
        assert result["commands"][0]["returncode"] == 0

    blocked_id = env.service.workspace_create("demo", "blocked any declared effect")["workspace_id"]
    for declared in ("read_only", "destructive_remote"):
        with pytest.raises(RepoForgeError) as exc:
            _exec(
                env,
                blocked_id,
                ("git", "push", "--force", "origin", "main"),
                declared_effect=declared,
            )
        assert exc.value.code is ErrorCode.DESTRUCTIVE_REMOTE_OPERATION_BLOCKED


def test_blocked_git_form_fails_via_the_durable_path(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "force push blocked")["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, ("git", "push", "--force", "origin", "main"))
    assert exc.value.code is ErrorCode.DESTRUCTIVE_REMOTE_OPERATION_BLOCKED


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
    """A default (background=False) call now runs through the #378 inline fast path,
    which validates stdin length before anything is persisted -- exactly like
    `workspace_run_adhoc`'s self-admitting path (test_workspace_adhoc.py's
    `test_adhoc_stdin_is_bounded`), raising `ADHOC_ARGV_INVALID`. An explicit
    `background=true` call still goes through the durable work queue, whose own
    state-size ceiling (`STATE_TOO_LARGE`) is reached first since the oversized
    payload is persisted before the ad-hoc guard ever runs."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "exec stdin bound")["workspace_id"]

    with pytest.raises(RepoForgeError) as exc:
        _exec(
            env,
            workspace_id,
            ("python3", "-c", "pass"),
            stdin_text="x" * (MAX_ADHOC_STDIN_LENGTH + 1),
        )
    assert exc.value.code is ErrorCode.ADHOC_ARGV_INVALID

    with pytest.raises(RepoForgeError) as background_exc:
        _exec(
            env,
            workspace_id,
            ("python3", "-c", "pass"),
            stdin_text="x" * (MAX_ADHOC_STDIN_LENGTH + 1),
            background=True,
        )
    assert background_exc.value.code is ErrorCode.STATE_TOO_LARGE


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


# ---------------------------------------------------------------------------
# Non-interactive default (#377 AC): a command with no stdin_text gets
# immediate EOF, never a hang, whether or not it was even expecting input.
# ---------------------------------------------------------------------------


def test_exec_with_no_stdin_text_gets_immediate_eof_not_a_hang(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "no stdin_text")["workspace_id"]

    result = _exec(
        env,
        workspace_id,
        ("python3", "-c", "import sys; data = sys.stdin.read(); print(repr(data))"),
    )

    assert result["outcome"] == "passed"
    assert result["commands"][0]["returncode"] == 0
    assert "''" in result["commands"][0]["output_excerpt"]


# ---------------------------------------------------------------------------
# Reviewed shell-script execution (#377): mutually exclusive with argv and
# argv_sequence, gated behind a separate, empty-by-default adhoc_shell_runners
# allowlist. Not authoritatively content-inspected for git forms the way argv is,
# though #407 layers a best-effort scan on top (see test_workspace_adhoc.py).
# ---------------------------------------------------------------------------


def test_contract_requires_exactly_one_of_argv_script_or_argv_sequence() -> None:
    with pytest.raises(ValueError, match="Exactly one of argv, argv_sequence, or script"):
        WorkspaceExecInput(workspace_id="ws-1")
    with pytest.raises(ValueError, match="Exactly one of argv, argv_sequence, or script"):
        WorkspaceExecInput(
            workspace_id="ws-1", argv=("git", "status"), script="echo hi", shell="sh"
        )


def test_contract_script_requires_shell() -> None:
    with pytest.raises(ValueError, match="shell is required with script"):
        WorkspaceExecInput(workspace_id="ws-1", script="echo hi")
    with pytest.raises(ValueError, match="shell is required with script"):
        WorkspaceExecInput(workspace_id="ws-1", argv=("git", "status"), shell="sh")


def test_contract_argv_sequence_rejects_stdin_text() -> None:
    """Review finding: stdin_text was accepted alongside argv_sequence, persisted into
    the durable work request, but silently dropped -- execute_sequence hardcodes
    stdin_text=None per element. Rejected at the contract instead of dropped."""
    with pytest.raises(ValueError, match="not supported with argv_sequence"):
        WorkspaceExecInput(
            workspace_id="ws-1",
            argv_sequence=(("ruff", "check"),),
            stdin_text="some input",
        )


def test_script_form_disabled_by_default(tmp_path: Path) -> None:
    """adhoc_shell_runners defaults to empty even under execution_mode='relaxed' with a
    populated adhoc_runners -- enabling the script form is a separate, explicit decision."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "script disabled")["workspace_id"]

    with pytest.raises(RepoForgeError) as exc:
        _exec_script(env, workspace_id, "echo hi", "sh")

    assert exc.value.code is ErrorCode.ADHOC_RUNNER_NOT_ALLOWED
    assert "adhoc_shell_runners" in str(exc.value)


def test_script_form_refuses_an_unlisted_shell(tmp_path: Path) -> None:
    env = _relaxed_shell_env(tmp_path, shell_runners=("sh",))
    workspace_id = env.service.workspace_create("demo", "unlisted shell")["workspace_id"]

    with pytest.raises(RepoForgeError) as exc:
        _exec_script(env, workspace_id, "echo hi", "bash")

    assert exc.value.code is ErrorCode.ADHOC_RUNNER_NOT_ALLOWED


def test_script_form_runs_through_the_allowlisted_shell(tmp_path: Path) -> None:
    env = _relaxed_shell_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "script happy path")["workspace_id"]

    result = _exec_script(env, workspace_id, "echo hello from script", "sh")

    assert result["outcome"] == "passed"
    command = result["commands"][0]
    assert command["returncode"] == 0
    assert command["argv"] == ["sh", "-c", "echo hello from script"]
    assert "hello from script" in command["output_excerpt"]


def test_script_form_supports_pipes_and_globbing(tmp_path: Path) -> None:
    """The whole point of the script form over argv: shell syntax argv structurally
    cannot carry (pipes, redirects, globbing, command chaining)."""
    env = _relaxed_shell_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "script pipes")["workspace_id"]

    result = _exec_script(
        env,
        workspace_id,
        "echo one two three | tr ' ' '\\n' | grep two && echo done",
        "sh",
    )

    assert result["outcome"] == "passed"
    excerpt = result["commands"][0]["output_excerpt"]
    assert "two" in excerpt
    assert "done" in excerpt


def test_script_form_is_never_content_inspected(tmp_path: Path) -> None:
    env = _relaxed_shell_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "script no inspection")["workspace_id"]

    result = _exec_script(env, workspace_id, "echo hi", "sh")

    assert result["adhoc_evidence"]["command_class"] is None
    assert result["adhoc_evidence"]["content_inspected"] is False


def test_script_form_catches_a_straightforward_blocked_git_command(tmp_path: Path) -> None:
    """#407: a blocked git form copy-pasted directly into a script body is now caught by
    scan_script_for_blocked_git_forms's best-effort scan, raising the same dedicated
    circuit-breaker code the equivalent argv form would (domain/adhoc.py). This is the
    gap #407 closes, superseding the earlier "not structurally blocked" behavior."""
    env = _relaxed_shell_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "script git clean force")["workspace_id"]

    with pytest.raises(RepoForgeError) as exc:
        _exec_script(env, workspace_id, "git clean --force", "sh")
    assert exc.value.code is ErrorCode.IRREVERSIBLE_LOCAL_OPERATION_BLOCKED


def test_script_form_scan_is_best_effort_not_a_shell_parser(tmp_path: Path) -> None:
    """The scan is a heuristic token scan over the literal script text, not a shell
    parser or interpreter (#407's own documented limit): a flag assembled through shell
    variable substitution never appears as a literal "--force" token, so it evades the
    scan exactly as domain/adhoc.py's docstrings say it can. This is the honest
    boundary, not a claim the scan defeats every obfuscation."""
    env = _relaxed_shell_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "script obfuscated git clean force")[
        "workspace_id"
    ]

    result = _exec_script(env, workspace_id, 'FLAG="--force"; git clean "$FLAG"', "sh")

    assert result["outcome"] in {"passed", "failed"}


def test_script_body_over_bound_is_rejected() -> None:
    from repoforge.domain.adhoc import MAX_ADHOC_SCRIPT_LENGTH

    with pytest.raises(ValueError):
        WorkspaceExecInput(
            workspace_id="ws-1", script="x" * (MAX_ADHOC_SCRIPT_LENGTH + 1), shell="sh"
        )


# ---------------------------------------------------------------------------
# Bounded fail-fast argv sequences (#443): additive to the single-argv form,
# not a substitute for shell syntax -- every element still goes through the
# same content inspection a single argv command would.
# ---------------------------------------------------------------------------


def test_argv_sequence_runs_every_element_in_order(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "sequence happy path")["workspace_id"]

    result = _exec_sequence(
        env,
        workspace_id,
        (("python3", "--version"), ("python3", "-c", "print('two')")),
    )

    assert result["outcome"] == "passed"
    assert len(result["commands"]) == 2
    assert all(command["returncode"] == 0 for command in result["commands"])
    assert "two" in result["commands"][1]["output_excerpt"]


def test_argv_sequence_element_receives_a_credential_profile_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#381 AC1/AC2 through the argv_sequence path specifically -- a separate call site
    in run_adhoc.py from the single-command form, so it needs its own coverage."""
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    env = create_forge_environment(
        tmp_path,
        execution_mode="relaxed",
        adhoc_runners=("python3",),
        credential_profiles=("docker",),
    )
    workspace_id = env.service.workspace_create("demo", "sequence credential profile")[
        "workspace_id"
    ]

    result = _exec_sequence(
        env,
        workspace_id,
        (
            (
                "python3",
                "-c",
                "import os; print('DOCKER_HOST=' + os.environ.get('DOCKER_HOST', '<absent>'))",
            ),
        ),
    )

    assert result["outcome"] == "passed"
    assert "DOCKER_HOST=unix:///var/run/docker.sock" in result["commands"][0]["output_excerpt"]


def test_argv_sequence_stops_at_the_first_failure(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "sequence fail fast")["workspace_id"]

    result = _exec_sequence(
        env,
        workspace_id,
        (
            ("python3", "-c", "import sys; sys.exit(3)"),
            ("python3", "-c", "print('should not run')"),
        ),
    )

    assert result["outcome"] == "failed"
    # Fail-fast: only the failing element's result is present, not the second one.
    assert len(result["commands"]) == 1
    assert result["commands"][0]["returncode"] == 3


def test_argv_sequence_mutating_element_requires_exact_state_lock(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "sequence needs lock")["workspace_id"]

    with pytest.raises(RepoForgeError) as exc:
        _exec_sequence(
            env,
            workspace_id,
            (("git", "status"), ("git", "checkout", "-b", "ai/seq-branch")),
        )

    assert exc.value.code is ErrorCode.ADHOC_ARGV_INVALID
    assert "mutability='workspace'" in str(exc.value)


def test_argv_sequence_mutating_element_runs_with_correct_lock(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "sequence correct lock")["workspace_id"]
    status = env.service.workspace_status(workspace_id)

    result = _exec_sequence(
        env,
        workspace_id,
        (("git", "status"), ("git", "checkout", "-b", "ai/seq-branch")),
        mutability="workspace",
        expected_head_sha=status["head_sha"],
        expected_fingerprint=status["workspace_fingerprint"],
    )

    assert result["outcome"] == "passed"
    assert len(result["commands"]) == 2


def test_argv_sequence_validates_every_element_before_running_any(tmp_path: Path) -> None:
    """A sequence either runs entirely reviewed or not at all: an invalid element 2 must
    refuse before element 1 -- which would otherwise have run and mutated the tree --
    ever starts."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "sequence validates upfront")[
        "workspace_id"
    ]

    with pytest.raises(RepoForgeError) as exc:
        _exec_sequence(
            env,
            workspace_id,
            (
                ("python3", "-c", "open('should-not-exist.txt', 'w').close()"),
                ("node", "--version"),  # not in adhoc_runners
            ),
        )

    assert exc.value.code is ErrorCode.ADHOC_RUNNER_NOT_ALLOWED
    path = Path(env.service.workspace_status(workspace_id)["path"])
    assert not (path / "should-not-exist.txt").exists()


def test_argv_sequence_over_bound_is_rejected() -> None:
    from repoforge.domain.adhoc import MAX_ADHOC_SEQUENCE_LENGTH

    with pytest.raises(ValueError):
        WorkspaceExecInput(
            workspace_id="ws-1",
            argv_sequence=tuple(
                ("python3", "--version") for _ in range(MAX_ADHOC_SEQUENCE_LENGTH + 1)
            ),
        )


def test_argv_sequence_reports_per_element_evidence_independently(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "sequence per-element evidence")[
        "workspace_id"
    ]

    result = _exec_sequence(
        env,
        workspace_id,
        (("python3", "-c", "print('first')"), ("python3", "-c", "print('second')")),
    )

    assert result["commands"][0]["argv"] == ["python3", "-c", "print('first')"]
    assert "first" in result["commands"][0]["output_excerpt"]
    assert "second" not in result["commands"][0]["output_excerpt"]
    assert result["commands"][1]["argv"] == ["python3", "-c", "print('second')"]
    assert "second" in result["commands"][1]["output_excerpt"]


def test_argv_sequence_reports_execution_evidence(tmp_path: Path) -> None:
    """Review finding (F-007): the sequence result never set execution_evidence, unlike
    the single-command path -- a caller had no way to see effective backend policy
    (containment degradation, network/filesystem posture) for a sequence run."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "sequence execution evidence")[
        "workspace_id"
    ]

    result = _exec_sequence(
        env,
        workspace_id,
        (("python3", "--version"), ("python3", "-c", "print('two')")),
    )

    assert result["execution_evidence"]
    assert result["execution_evidence"]["requested_filesystem"] == "workspace_write"
    assert result["execution_evidence"]["effective_filesystem"] == "host_account_access"


# ---------------------------------------------------------------------------
# #383: operator-issued trusted_host lease widens the runner allowlist only for
# an exact repository+branch match, presented via an opaque token -- never a
# model-mintable authorization.
# ---------------------------------------------------------------------------


def _grant_lease(
    env: ForgeEnvironment,
    *,
    repo_id: str,
    checkout_identity: str,
    branch: str,
    ttl_seconds: int = 1_800,
    issued_at: datetime | None = None,
) -> str:
    """Directly mint and persist a lease (bypassing the CLI), returning the raw token."""
    lease_store = JsonHostBypassLeaseStore(
        env.root / "state", build_lock_manager(env.root / "state")
    )
    raw_token, token_hash = mint_lease_token()
    now = issued_at if issued_at is not None else datetime.now(timezone.utc)
    lease_store.create(
        HostBypassLease(
            lease_id="lease-" + "a" * 24,
            repository_identity=repo_id,
            checkout_identity=checkout_identity,
            workspace_kind="managed_worktree",
            branch_or_ref=branch,
            allowed_effects=("broad_shell",),
            host_effect_scope=(),
            credential_profile_ids=(),
            granted_by="test-operator",
            principal_token_hash=token_hash,
            config_generation="1",
            policy_digest="digest",
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
    )
    return raw_token


def _relaxed_env_with_trust(tmp_path: Path, *, runners: tuple[str, ...] = ()) -> ForgeEnvironment:
    return create_forge_environment(
        tmp_path,
        execution_mode="strict",
        adhoc_runners=runners,
        trusted_host_enabled=True,
    )


def test_lease_widens_runner_allowlist_beyond_strict_mode(tmp_path: Path) -> None:
    """#383 AC1: an active lease lets a normally-disabled (strict) repository run an
    arbitrary command for the lease's exact branch, without the operator making the
    repository relaxed or listing the runner in adhoc_runners at all."""
    env = _relaxed_env_with_trust(tmp_path)
    created = env.service.workspace_create("demo", "trusted host lease widens allowlist")
    workspace_id = created["workspace_id"]
    token = _grant_lease(
        env, repo_id="demo", checkout_identity=workspace_id, branch=created["branch"]
    )

    result = _exec(env, workspace_id, ("python3", "--version"), lease_token=token)

    assert result["outcome"] == "passed"
    assert result["commands"][0]["returncode"] == 0


def test_lease_never_widens_circuit_breakers(tmp_path: Path) -> None:
    """#383: a lease widens the runner allowlist only -- #385's circuit breakers and
    #407's protected-ref check still apply unconditionally."""
    env = _relaxed_env_with_trust(tmp_path, runners=("git",))
    created = env.service.workspace_create("demo", "trusted host lease still blocked")
    workspace_id = created["workspace_id"]
    token = _grant_lease(
        env, repo_id="demo", checkout_identity=workspace_id, branch=created["branch"]
    )

    with pytest.raises(RepoForgeError) as exc:
        _exec(
            env,
            workspace_id,
            ("git", "push", "--force", "origin", "main"),
            lease_token=token,
        )
    assert exc.value.code is ErrorCode.DESTRUCTIVE_REMOTE_OPERATION_BLOCKED


def test_lease_scoped_to_a_different_branch_does_not_apply(tmp_path: Path) -> None:
    """#383 AC3: a lease cannot be replayed against a broader scope than granted --
    here, a different branch than the one it names."""
    env = _relaxed_env_with_trust(tmp_path)
    created = env.service.workspace_create("demo", "trusted host lease wrong branch")
    workspace_id = created["workspace_id"]
    token = _grant_lease(
        env, repo_id="demo", checkout_identity=workspace_id, branch="ai/some-other-branch"
    )

    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, ("python3", "--version"), lease_token=token)
    assert exc.value.code is ErrorCode.EXECUTION_MODE_STRICT


def test_lease_scoped_to_a_different_checkout_does_not_apply(tmp_path: Path) -> None:
    """#383 AC3: a lease cannot be replayed against a different checkout than the one
    it names, even for the exact same repository and branch (e.g. a second worktree)."""
    env = _relaxed_env_with_trust(tmp_path)
    created = env.service.workspace_create("demo", "trusted host lease wrong checkout")
    workspace_id = created["workspace_id"]
    token = _grant_lease(
        env, repo_id="demo", checkout_identity="a-different-checkout", branch=created["branch"]
    )

    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, ("python3", "--version"), lease_token=token)
    assert exc.value.code is ErrorCode.EXECUTION_MODE_STRICT


def test_expired_lease_does_not_apply(tmp_path: Path) -> None:
    """#383 AC2: an expired lease fails before process launch -- ordinary admission
    applies as if no token had been presented at all."""
    env = _relaxed_env_with_trust(tmp_path)
    created = env.service.workspace_create("demo", "trusted host lease expired")
    workspace_id = created["workspace_id"]
    token = _grant_lease(
        env,
        repo_id="demo",
        checkout_identity=workspace_id,
        branch=created["branch"],
        ttl_seconds=60,
        issued_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, ("python3", "--version"), lease_token=token)
    assert exc.value.code is ErrorCode.EXECUTION_MODE_STRICT


def test_wrong_token_does_not_apply(tmp_path: Path) -> None:
    """A token that does not hash-match any stored lease is indistinguishable from no
    token at all -- never a partial or degraded bypass."""
    env = _relaxed_env_with_trust(tmp_path)
    created = env.service.workspace_create("demo", "trusted host lease wrong token")
    workspace_id = created["workspace_id"]
    _grant_lease(env, repo_id="demo", checkout_identity=workspace_id, branch=created["branch"])

    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, ("python3", "--version"), lease_token="not-the-real-token")
    assert exc.value.code is ErrorCode.EXECUTION_MODE_STRICT


def test_lease_ignored_when_repository_not_enrolled(tmp_path: Path) -> None:
    """#383: trusted_host_enabled=False (the default) means a presented token is
    inert, even if a lease record happens to exist for this repository/branch --
    ordinary relaxed-mode admission (here, its runner allowlist) still applies."""
    env = _relaxed_env(tmp_path)  # trusted_host_enabled defaults to False
    created = env.service.workspace_create("demo", "trusted host lease not enrolled")
    workspace_id = created["workspace_id"]
    token = _grant_lease(
        env, repo_id="demo", checkout_identity=workspace_id, branch=created["branch"]
    )

    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, ("node", "--version"), lease_token=token)
    assert exc.value.code is ErrorCode.ADHOC_RUNNER_NOT_ALLOWED


# ---------------------------------------------------------------------------
# #384: `sandboxed_turbo` execution backend widens the runner allowlist only for a
# repository enrolled in `sandbox_backend_enabled`, exactly like a #383 lease --
# never a model-mintable authorization, never a bypass of #385's circuit breakers.
# ---------------------------------------------------------------------------


class _DirectCommandExecutor:
    """Minimal CommandExecutor for these admission-wiring tests: runs real subprocesses
    directly on the host, with no PATH/environment shaping. Only ever wired into a
    test-local execution-environment override, never the production-wired executor."""

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        if extra:
            env.update(extra)
        return env

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: dict[str, str] | None = None,
        output_limit: int | None = None,
        cancel_token: object | None = None,
    ) -> CommandResult:
        proc = subprocess.run(
            list(argv),
            cwd=str(cwd),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(extra_env or {})},
        )
        return CommandResult(
            argv=tuple(argv),
            cwd=str(cwd),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    def run_isolated(self, *args: object, **kwargs: object) -> CommandResult:
        raise NotImplementedError

    def run_bytes(
        self, argv: tuple[str, ...], *, cwd: Path, timeout: int | None = None, max_bytes: int
    ) -> bytes:
        proc = subprocess.run(list(argv), cwd=str(cwd), capture_output=True, timeout=timeout)
        return proc.stdout[:max_bytes]


class _LocalSandboxAdapter(DockerExecutionAdapter):
    """Test double: inherits DockerExecutionAdapter's real policy/identity/enforcement
    logic unchanged (proving the *admission* wiring reaches the sandboxed backend and
    gets back a distinct, honest identity), but runs the reviewed argv directly on the
    host instead of inside a real container, so these tests need no Docker daemon.
    Real containment enforcement is proven separately by Docker-gated integration
    tests exercising the real adapter end-to-end."""

    def __init__(self, executor: _DirectCommandExecutor) -> None:
        super().__init__(executor)
        self._reachable = True  # skip the docker-reachability probe entirely

    def execute_in_session(
        self,
        session: object,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout: int,
        output_limit: int,
        check: bool,
        cancel_token: object | None = None,
        stdin_text: str | None = None,
        extra_env: tuple[tuple[str, str], ...] = (),
    ) -> CommandResult:
        self._session_context(session.session_id)  # type: ignore[attr-defined]
        return self._executor.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            check=check,
            output_limit=output_limit,
            input_text=stdin_text,
            extra_env=dict(extra_env) or None,
        )


def _sandbox_env(tmp_path: Path, *, runners: tuple[str, ...] = ()) -> ForgeEnvironment:
    executor = _DirectCommandExecutor()
    routing = RoutingExecutionEnvironment(
        native=NativeReviewedAdapter(executor),  # type: ignore[arg-type]
        sandboxed=_LocalSandboxAdapter(executor),
    )
    return create_forge_environment(
        tmp_path,
        execution_mode="strict",
        adhoc_runners=runners,
        sandbox_backend_enabled=True,
        execution_environment=routing,
    )


def test_sandbox_widens_runner_allowlist_beyond_strict_mode(tmp_path: Path) -> None:
    """#384 AC1: a sandboxed_turbo request lets a normally-disabled (strict) repository
    run an arbitrary command, without the operator making the repository relaxed or
    listing the runner in adhoc_runners at all."""
    env = _sandbox_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "sandboxed_turbo widens allowlist")[
        "workspace_id"
    ]

    result = _exec(env, workspace_id, ("python3", "--version"), sandbox_requested=True)

    assert result["outcome"] == "passed"
    assert result["commands"][0]["returncode"] == 0


def test_sandbox_never_widens_circuit_breakers(tmp_path: Path) -> None:
    """#384: sandboxed_turbo widens the runner allowlist only -- #385's circuit
    breakers and #407's protected-ref check still apply unconditionally."""
    env = _sandbox_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "sandboxed_turbo still blocked")[
        "workspace_id"
    ]

    with pytest.raises(RepoForgeError) as exc:
        _exec(
            env,
            workspace_id,
            ("git", "push", "--force", "origin", "main"),
            sandbox_requested=True,
        )
    assert exc.value.code is ErrorCode.DESTRUCTIVE_REMOTE_OPERATION_BLOCKED


def test_sandbox_ignored_when_repository_not_enrolled(tmp_path: Path) -> None:
    """#384: sandbox_backend_enabled=False (the default) means sandbox_requested=True
    is inert -- ordinary relaxed-mode admission (here, its runner allowlist) still
    applies, never a partial or degraded bypass."""
    env = _relaxed_env(tmp_path)  # sandbox_backend_enabled defaults to False

    workspace_id = env.service.workspace_create("demo", "sandboxed_turbo not enrolled")[
        "workspace_id"
    ]

    with pytest.raises(RepoForgeError) as exc:
        _exec(env, workspace_id, ("node", "--version"), sandbox_requested=True)
    assert exc.value.code is ErrorCode.ADHOC_RUNNER_NOT_ALLOWED


def test_sandbox_evidence_is_distinct_from_native_and_lease(tmp_path: Path) -> None:
    """#384 AC3: sandboxed_turbo evidence differs structurally from both the ordinary
    native path and a #383 host-bypass lease -- a distinct adapter_kind, and an
    explicit sandbox_backend_requested marker mirroring the lease's own
    trusted_host_lease_id marker."""
    env = _sandbox_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "sandboxed_turbo evidence distinctness")[
        "workspace_id"
    ]

    result = _exec(env, workspace_id, ("python3", "--version"), sandbox_requested=True)

    assert result["execution_evidence"]["adapter_kind"] == "hermetic_container"

    events = _audit_events(env.root, "workspace_run_adhoc")
    assert events, "expected at least one workspace_run_adhoc audit event"
    details = events[-1]["details"]
    assert isinstance(details, dict)
    assert details["sandbox_backend_requested"] is True
    assert details["trusted_host_lease_id"] is None
