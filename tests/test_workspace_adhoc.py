"""Coverage for issue #169: per-repository relaxed execution mode and the audited
ad-hoc command runner.

Hard constraint under test throughout this module: an ad-hoc run must never
populate ``last_verification`` or satisfy ``require_verification_before_commit``
-- only an enrolled verification profile can do that
(``src/repoforge/application/workspace/commit.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment

from repoforge.config import load_config
from repoforge.domain.errors import ConfigError, ErrorCode, RepoForgeError


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


def _audit_events(root: Path, action: str) -> list[dict[str, object]]:
    audit_path = root / "state" / "audit.jsonl"
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line
    ]
    return [event for event in events if event["action"] == action]


# ---------------------------------------------------------------------------
# Config parsing and validation
# ---------------------------------------------------------------------------


def test_execution_mode_defaults_to_strict(forge_env: ForgeEnvironment) -> None:
    config = load_config(forge_env.config_path)
    repo = config.repositories["demo"]
    assert repo.execution_mode.value == "strict"
    assert repo.adhoc_runners == ()


def test_relaxed_mode_requires_nonempty_adhoc_runners(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="adhoc_runners"):
        create_forge_environment(tmp_path, execution_mode="relaxed", adhoc_runners=())


@pytest.mark.parametrize(
    "runners",
    [
        ("../evil",),
        ("/usr/bin/python3",),
        ("py thon",),
        ("",),
    ],
)
def test_adhoc_runners_rejects_invalid_basenames(tmp_path: Path, runners: tuple[str, ...]) -> None:
    with pytest.raises(ConfigError):
        create_forge_environment(tmp_path, execution_mode="relaxed", adhoc_runners=runners)


def test_adhoc_runners_rejects_duplicates(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="duplicate"):
        create_forge_environment(
            tmp_path, execution_mode="relaxed", adhoc_runners=("python3", "python3")
        )


def test_relaxed_mode_loads_with_valid_allowlist(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("python3", "uv"))
    config = load_config(env.config_path)
    repo = config.repositories["demo"]
    assert repo.execution_mode.value == "relaxed"
    assert repo.adhoc_runners == ("python3", "uv")


# ---------------------------------------------------------------------------
# Strict-mode refusal
# ---------------------------------------------------------------------------


def test_strict_repo_refuses_adhoc_run(forge_env: ForgeEnvironment) -> None:
    created = forge_env.service.workspace_create("demo", "strict adhoc refusal")
    workspace_id = created["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        forge_env.service.workspace_run_adhoc(workspace_id, ["python3", "--version"])
    assert exc.value.code is ErrorCode.EXECUTION_MODE_STRICT
    assert "execution_mode" in (exc.value.safe_next_action or "")


# ---------------------------------------------------------------------------
# Relaxed-mode execution, evidence, and validation
# ---------------------------------------------------------------------------


def test_relaxed_repo_runs_allowlisted_command_as_evidence_only(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    created = env.service.workspace_create("demo", "adhoc happy path")
    workspace_id = created["workspace_id"]

    result = env.service.workspace_run_adhoc(
        workspace_id,
        ["python3", "-c", "from pathlib import Path; assert Path('hello.txt').exists()"],
    )
    assert result["returncode"] == 0
    assert result["execution_evidence"]["requested_filesystem"] == "workspace_write"
    assert result["execution_evidence"]["effective_filesystem"] == "host_account_access"
    assert result["runner"] == "python3"
    assert result["evidence_only"] is True
    assert result["satisfies_commit_gate"] is False
    assert result["network_policy"] == "advisory_local_only"
    assert result["fingerprint_changed"] is False
    assert "fingerprint_before" in result and "fingerprint_after" in result
    assert result.get("gate_guidance")


@pytest.mark.parametrize(
    ("argv", "expected_code"),
    [
        (["node", "--version"], ErrorCode.ADHOC_RUNNER_NOT_ALLOWED),
        (["./python3", "-c", "1"], ErrorCode.ADHOC_RUNNER_NOT_ALLOWED),
        (["/usr/bin/python3", "-c", "1"], ErrorCode.ADHOC_RUNNER_NOT_ALLOWED),
    ],
)
def test_disallowed_runner_yields_structured_error(
    tmp_path: Path, argv: list[str], expected_code: ErrorCode
) -> None:
    env = _relaxed_env(tmp_path)
    created = env.service.workspace_create("demo", "disallowed runner")
    workspace_id = created["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        env.service.workspace_run_adhoc(workspace_id, argv)
    assert exc.value.code is expected_code


def test_argv_over_bound_is_rejected(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    created = env.service.workspace_create("demo", "argv too long")
    workspace_id = created["workspace_id"]
    argv = ["python3", *[f"--flag{i}" for i in range(40)]]
    with pytest.raises(RepoForgeError) as exc:
        env.service.workspace_run_adhoc(workspace_id, argv)
    assert exc.value.code is ErrorCode.ADHOC_ARGV_INVALID


def test_empty_argv_is_rejected(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    created = env.service.workspace_create("demo", "argv empty")
    workspace_id = created["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        env.service.workspace_run_adhoc(workspace_id, [])
    assert exc.value.code is ErrorCode.ADHOC_ARGV_INVALID


def test_mutating_adhoc_command_reports_fingerprint_change_and_paths(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    created = env.service.workspace_create("demo", "mutating adhoc")
    workspace_id = created["workspace_id"]
    workspace_path = Path(created["path"])

    result = env.service.workspace_run_adhoc(
        workspace_id,
        [
            "python3",
            "-c",
            "from pathlib import Path; Path('hello.txt').write_text('changed\\n')",
        ],
    )
    assert result["fingerprint_changed"] is True
    assert "hello.txt" in result["changed_paths"]
    assert (workspace_path / "hello.txt").read_text() == "changed\n"


# ---------------------------------------------------------------------------
# The hard constraint: ad-hoc never satisfies the verification-before-commit gate.
# ---------------------------------------------------------------------------


def test_adhoc_run_never_satisfies_commit_gate(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, require_verification=True)
    created = env.service.workspace_create("demo", "adhoc gate regression")
    workspace_id = created["workspace_id"]

    # A successful, mutating ad-hoc run that makes the exact change the "full"
    # profile's assertion checks for.
    result = env.service.workspace_run_adhoc(
        workspace_id,
        [
            "python3",
            "-c",
            "from pathlib import Path; Path('hello.txt').write_text('changed\\n')",
        ],
    )
    assert result["returncode"] == 0

    # The commit gate must still demand a real verification profile -- ad-hoc
    # evidence, however successful, is not enough.
    with pytest.raises(RepoForgeError) as exc:
        env.service.workspace_commit(workspace_id, "attempt commit without verification")
    assert "verification" in str(exc.value).lower()

    # Only after the enrolled "full" verification profile actually runs (and
    # passes on this exact tree) does the commit gate open.
    env.service.workspace_run_profile(workspace_id, "full")
    committed = env.service.workspace_commit(workspace_id, "verified commit")
    assert committed["verified_profile"] == "full"


def test_adhoc_run_invalidates_stale_verification_receipt(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, require_verification=True)
    created = env.service.workspace_create("demo", "adhoc invalidates verification")
    workspace_id = created["workspace_id"]

    # Establish a passing verification receipt first.
    Path(created["path"], "hello.txt").write_text("changed\n", encoding="utf-8")
    env.service.workspace_run_profile(workspace_id, "full")

    # An ad-hoc run that mutates the tree again must invalidate that receipt.
    env.service.workspace_run_adhoc(
        workspace_id,
        [
            "python3",
            "-c",
            "from pathlib import Path; Path('hello.txt').write_text('changed twice\\n')",
        ],
    )
    with pytest.raises(RepoForgeError):
        env.service.workspace_commit(workspace_id, "should still require re-verification")


# ---------------------------------------------------------------------------
# Reviewed exec escape hatch: git guards, exact-state lock, mutability modes
# ---------------------------------------------------------------------------


def test_read_only_git_command_runs_and_is_classified(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "git status")["workspace_id"]
    result = env.service.workspace_run_adhoc(workspace_id, ["git", "status", "--porcelain=v2"])
    assert result["returncode"] == 0
    assert result["command_class"] == "read_only"
    assert result["mutability"] == "read_only"
    assert result["fingerprint_changed"] is False
    assert result["read_only_violation"] is False


def test_blocked_git_form_fails_before_execution(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "force push blocked")["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        env.service.workspace_run_adhoc(workspace_id, ["git", "push", "--force", "origin", "main"])
    assert exc.value.code is ErrorCode.ADHOC_COMMAND_FORBIDDEN
    # No audit event is written because the guard rejects before the audited body runs.
    assert _audit_events(env.root, "workspace_run_adhoc") == []


def test_mutating_git_command_under_read_only_is_rejected(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "merge needs lock")["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        env.service.workspace_run_adhoc(workspace_id, ["git", "merge", "origin/main"])
    assert exc.value.code is ErrorCode.ADHOC_ARGV_INVALID
    assert "mutability='workspace'" in str(exc.value)


def test_workspace_mutability_requires_exact_state_lock(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "missing lock")["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        env.service.workspace_run_adhoc(
            workspace_id, ["git", "merge", "origin/main"], mutability="workspace"
        )
    assert exc.value.code is ErrorCode.ADHOC_ARGV_INVALID
    assert "exact-state lock" in str(exc.value)


def test_stale_expected_head_sha_fails_closed(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "stale head")["workspace_id"]
    status = env.service.workspace_status(workspace_id)
    with pytest.raises(RepoForgeError) as exc:
        env.service.workspace_run_adhoc(
            workspace_id,
            ["git", "checkout", "-b", "ai/x"],
            mutability="workspace",
            expected_head_sha="0" * 40,
            expected_fingerprint=status["workspace_fingerprint"],
        )
    assert exc.value.code is ErrorCode.STALE_STATE


def test_mutating_git_command_runs_with_correct_lock(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",), require_verification=False)
    workspace_id = env.service.workspace_create("demo", "locked mutation")["workspace_id"]
    status = env.service.workspace_status(workspace_id)
    result = env.service.workspace_run_adhoc(
        workspace_id,
        ["git", "checkout", "-b", "ai/scratch"],
        mutability="workspace",
        expected_head_sha=status["head_sha"],
        expected_fingerprint=status["workspace_fingerprint"],
    )
    assert result["returncode"] == 0
    assert result["command_class"] == "mutating"
    assert result["mutability"] == "workspace"
    assert result["head_sha_before"] == status["head_sha"]


def test_invalid_mutability_value_is_rejected(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path, runners=("git",))
    workspace_id = env.service.workspace_create("demo", "bad mutability")["workspace_id"]
    with pytest.raises(RepoForgeError) as exc:
        env.service.workspace_run_adhoc(workspace_id, ["git", "status"], mutability="destroy")
    assert exc.value.code is ErrorCode.ADHOC_ARGV_INVALID


# ---------------------------------------------------------------------------
# Audit and enrollment nudge
# ---------------------------------------------------------------------------


def test_audit_records_full_argv_and_runner(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    created = env.service.workspace_create("demo", "adhoc audit")
    workspace_id = created["workspace_id"]
    env.service.workspace_run_adhoc(workspace_id, ["python3", "--version"])

    events = _audit_events(env.root, "workspace_run_adhoc")
    assert events, "expected exactly one workspace_run_adhoc audit event"
    details = events[-1]["details"]
    assert details["runner"] == "python3"
    assert "duration_ms" in details
    assert details["network_policy"] == "advisory_local_only"


def test_repeated_identical_argv_triggers_enrollment_nudge(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    created = env.service.workspace_create("demo", "adhoc nudge")
    workspace_id = created["workspace_id"]
    argv = ["python3", "-c", "print('same shape every time')"]

    results = [env.service.workspace_run_adhoc(workspace_id, list(argv)) for _ in range(3)]
    assert results[0]["enrollment_nudge"] is None
    assert results[1]["enrollment_nudge"] is None
    assert results[2]["enrollment_nudge"] is not None
    assert "diagnostic" in results[2]["enrollment_nudge"].lower()


def test_different_argv_shapes_do_not_cross_contaminate_nudge_counts(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    created = env.service.workspace_create("demo", "adhoc nudge distinct shapes")
    workspace_id = created["workspace_id"]

    for value in ("one", "two", "three"):
        result = env.service.workspace_run_adhoc(
            workspace_id, ["python3", "-c", f"print({value!r})"]
        )
        assert result["enrollment_nudge"] is None
