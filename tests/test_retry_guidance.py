"""Coverage for issue #167: repeated identical profile-failure retry-burst guidance."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.domain.errors import RepoForgeError
from repoforge.domain.retry_guidance import (
    FAILURE_REUSE_METADATA_KEY,
    MAX_REUSABLE_FAILURE_BYTES,
    MAX_TRACKED_TARGETS,
    FailureReuseBinding,
    FailureSignature,
    clear,
    fast_fail_guidance,
    record_and_compare,
    record_reusable_failure,
    reusable_failure,
)

_ALWAYS_FAIL_PROFILE = (
    "\n[repositories.demo.profiles.always_fail]\n"
    'description = "Deterministically failing verification profile"\n'
    "verification = true\n"
    'commands = [["python3", "-c", "import sys; sys.exit(1)"]]\n'
)


def _counting_typed_failure_profile(
    counter: Path,
    *,
    profile_name: str,
    step_kind: str,
    message: str = "",
) -> str:
    script = (
        "from pathlib import Path; import sys; "
        f"p=Path({str(counter)!r}); "
        "p.write_text(str((int(p.read_text()) if p.exists() else 0)+1)); "
        f"print({message!r}); "
        "sys.exit(7)"
    )
    command = json.dumps(["python3", "-c", script])
    commands = json.dumps([["python3", "-c", script]])
    return (
        f"\n[repositories.demo.profiles.{profile_name}]\n"
        'description = "Deterministic counted failure"\n'
        "verification = true\n"
        f"commands = {commands}\n"
        f"\n[[repositories.demo.profiles.{profile_name}.steps]]\n"
        'id = "step"\n'
        f"kind = {json.dumps(step_kind)}\n"
        f"command = {command}\n"
    )


def _counting_fail_profile(counter: Path) -> str:
    return _counting_typed_failure_profile(
        counter,
        profile_name="counting_fail",
        step_kind="static_analysis",
    )


_MISSING_TOOL_PROFILE = (
    "\n[repositories.demo.profiles.missing_tool]\n"
    'description = "References a nonexistent executable"\n'
    "verification = true\n"
    'commands = [["this-command-does-not-exist-xyz"]]\n'
)


def _append(env: ForgeEnvironment, text: str) -> None:
    current = env.config_path.read_text(encoding="utf-8")
    env.config_path.write_text(current + text, encoding="utf-8")


def _set_fast_fail_threshold(env: ForgeEnvironment, seconds: float) -> None:
    text = env.config_path.read_text(encoding="utf-8")
    assert "path_prefixes = " in text
    text = text.replace(
        "path_prefixes = ", f"fast_fail_threshold_seconds = {seconds}\npath_prefixes = ", 1
    )
    env.config_path.write_text(text, encoding="utf-8")


def _reload_service(env: ForgeEnvironment) -> CodingService:
    """Reload the service after mutating config.toml on disk mid-test."""
    return CodingService(load_config(env.config_path))


# ---------------------------------------------------------------------------
# Pure domain-level unit tests
# ---------------------------------------------------------------------------


def test_first_failure_has_no_repeat_guidance() -> None:
    metadata: dict[str, object] = {}
    repeat, guidance = record_and_compare(
        metadata,
        target="profile:full",
        fingerprint="fp-1",
        signature=FailureSignature("COMMAND_FAILED", 0, 1),
    )
    assert repeat == 1
    assert guidance is None


def test_second_identical_failure_with_unchanged_fingerprint_yields_guidance() -> None:
    metadata: dict[str, object] = {}
    record_and_compare(
        metadata,
        target="profile:full",
        fingerprint="fp-1",
        signature=FailureSignature("COMMAND_FAILED", 0, 1),
    )
    repeat, guidance = record_and_compare(
        metadata,
        target="profile:full",
        fingerprint="fp-1",
        signature=FailureSignature("COMMAND_FAILED", 0, 1),
    )
    assert repeat == 2
    assert guidance is not None
    assert guidance.identical_failure_repeat == 2


def test_fingerprint_change_resets_detection() -> None:
    metadata: dict[str, object] = {}
    record_and_compare(
        metadata,
        target="profile:full",
        fingerprint="fp-1",
        signature=FailureSignature("COMMAND_FAILED", 0, 1),
    )
    repeat, guidance = record_and_compare(
        metadata,
        target="profile:full",
        fingerprint="fp-2",  # a mutation happened between runs
        signature=FailureSignature("COMMAND_FAILED", 0, 1),
    )
    assert repeat == 1
    assert guidance is None


def test_different_signature_resets_detection_even_with_same_fingerprint() -> None:
    metadata: dict[str, object] = {}
    record_and_compare(
        metadata,
        target="profile:full",
        fingerprint="fp-1",
        signature=FailureSignature("COMMAND_FAILED", 0, 1),
    )
    repeat, guidance = record_and_compare(
        metadata,
        target="profile:full",
        fingerprint="fp-1",
        signature=FailureSignature("COMMAND_FAILED", 1, 1),  # different failed_step
    )
    assert repeat == 1
    assert guidance is None


def test_clear_removes_tracked_target_and_reports_whether_anything_changed() -> None:
    metadata: dict[str, object] = {}
    assert clear(metadata, target="profile:full") is False
    record_and_compare(
        metadata,
        target="profile:full",
        fingerprint="fp-1",
        signature=FailureSignature("COMMAND_FAILED", 0, 1),
    )
    assert clear(metadata, target="profile:full") is True
    assert clear(metadata, target="profile:full") is False


def test_history_is_bounded_to_max_tracked_targets() -> None:
    metadata: dict[str, object] = {}
    for index in range(MAX_TRACKED_TARGETS + 5):
        record_and_compare(
            metadata,
            target=f"profile:p{index}",
            fingerprint="fp",
            signature=FailureSignature("COMMAND_FAILED", 0, 1),
        )
    assert len(metadata["retry_guidance_history"]) <= MAX_TRACKED_TARGETS


def test_corrupt_history_degrades_to_fresh_tracking_instead_of_crashing() -> None:
    metadata: dict[str, object] = {"retry_guidance_history": "not-a-dict"}
    repeat, guidance = record_and_compare(
        metadata,
        target="profile:full",
        fingerprint="fp-1",
        signature=FailureSignature("COMMAND_FAILED", 0, 1),
    )
    assert repeat == 1
    assert guidance is None
    assert isinstance(metadata["retry_guidance_history"], dict)


def test_fast_fail_guidance_only_below_threshold() -> None:
    assert fast_fail_guidance(5.0, threshold_seconds=10.0) is not None
    assert fast_fail_guidance(15.0, threshold_seconds=10.0) is None


def _reuse_binding() -> FailureReuseBinding:
    return FailureReuseBinding(
        fingerprint="1" * 64,
        target_identity="2" * 64,
        command_source_identity="3" * 64,
        config_identity="4" * 64,
        environment_identity="5" * 64,
    )


def test_reusable_failure_requires_exact_complete_binding() -> None:
    metadata: dict[str, object] = {}
    binding = _reuse_binding()
    evidence = {
        "error_code": "COMMAND_FAILED",
        "exit_code": 1,
        "failure_domain": "static_analysis",
        "complete": True,
    }

    assert record_reusable_failure(
        metadata,
        target="profile:full",
        binding=binding,
        evidence=evidence,
    )
    assert (
        reusable_failure(
            metadata,
            target="profile:full",
            binding=binding,
        )
        == evidence
    )

    for field in (
        "fingerprint",
        "target_identity",
        "command_source_identity",
        "config_identity",
        "environment_identity",
    ):
        assert (
            reusable_failure(
                metadata,
                target="profile:full",
                binding=replace(binding, **{field: "f" * 64}),
            )
            is None
        )


def test_reusable_failure_rejects_incomplete_oversized_and_corrupt_evidence() -> None:
    binding = _reuse_binding()
    metadata: dict[str, object] = {}

    assert not record_reusable_failure(
        metadata,
        target="profile:full",
        binding=binding,
        evidence={"error_code": "COMMAND_FAILED", "complete": False},
    )
    assert not record_reusable_failure(
        metadata,
        target="profile:full",
        binding=binding,
        evidence={
            "error_code": "COMMAND_FAILED",
            "complete": True,
            "chunks": ["x" * MAX_REUSABLE_FAILURE_BYTES] * 3,
        },
    )

    metadata[FAILURE_REUSE_METADATA_KEY] = {
        "profile:full": {"version": 999, "binding": {}, "evidence": "broken"}
    }
    assert reusable_failure(metadata, target="profile:full", binding=binding) is None


# ---------------------------------------------------------------------------
# Real-workspace integration tests
# ---------------------------------------------------------------------------


def test_unchanged_deterministic_profile_failure_is_reused_and_forceable(
    forge_env: ForgeEnvironment,
    tmp_path: Path,
) -> None:
    counter = tmp_path / "profile-attempts.txt"
    _append(forge_env, _counting_fail_profile(counter))
    _set_fast_fail_threshold(forge_env, 0)
    service = _reload_service(forge_env)
    created = service.workspace_create("demo", "dedupe-profile-failure")
    workspace_id = created["workspace_id"]

    with pytest.raises(RepoForgeError) as first:
        service.workspace_run_profile(workspace_id, "counting_fail")
    assert counter.read_text(encoding="utf-8") == "1"
    assert first.value.details.get("failure_reused") is not True
    assert service.workspace_status(workspace_id)["last_verification"] is None

    with pytest.raises(RepoForgeError) as second:
        service.workspace_run_profile(workspace_id, "counting_fail")
    assert counter.read_text(encoding="utf-8") == "1"
    assert second.value.details["failure_reused"] is True
    assert second.value.details["reuse_binding"]
    assert service.workspace_status(workspace_id)["last_verification"] is None

    with pytest.raises(RepoForgeError):
        service.workspace_run_profile(workspace_id, "counting_fail", force_rerun=True)
    assert counter.read_text(encoding="utf-8") == "2"

    Path(created["path"], "scratch.txt").write_text("mutation\n", encoding="utf-8")
    with pytest.raises(RepoForgeError):
        service.workspace_run_profile(workspace_id, "counting_fail")
    assert counter.read_text(encoding="utf-8") == "3"


@pytest.mark.parametrize(
    ("profile_name", "step_kind", "message"),
    [
        ("counting_business", "business_tests", "1 failed in 0.01s"),
        ("counting_network", "static_analysis", "connection refused"),
    ],
)
def test_flaky_or_network_shaped_profile_failure_is_not_reused(
    forge_env: ForgeEnvironment,
    tmp_path: Path,
    profile_name: str,
    step_kind: str,
    message: str,
) -> None:
    counter = tmp_path / f"{profile_name}-attempts.txt"
    _append(
        forge_env,
        _counting_typed_failure_profile(
            counter,
            profile_name=profile_name,
            step_kind=step_kind,
            message=message,
        ),
    )
    _set_fast_fail_threshold(forge_env, 0)
    service = _reload_service(forge_env)
    workspace_id = service.workspace_create("demo", profile_name)["workspace_id"]

    for _ in range(2):
        with pytest.raises(RepoForgeError) as failure:
            service.workspace_run_profile(workspace_id, profile_name)
        assert failure.value.details.get("failure_reused") is not True
    assert counter.read_text(encoding="utf-8") == "2"


def test_two_consecutive_failures_without_edits_carry_retry_guidance(
    forge_env: ForgeEnvironment,
) -> None:
    _append(forge_env, _ALWAYS_FAIL_PROFILE)
    _set_fast_fail_threshold(forge_env, 0)  # isolate the repeat mechanism from fast-fail
    service = _reload_service(forge_env)
    created = service.workspace_create("demo", "retry guidance repeat")
    workspace_id = created["workspace_id"]

    with pytest.raises(RepoForgeError) as first:
        service.workspace_run_profile(workspace_id, "always_fail")
    assert "retry_guidance" not in first.value.details
    assert first.value.details["steps_completed"] == 0
    failed_step = first.value.details["failed_step"]
    assert failed_step["duration_ms"] >= 0
    assert failed_step["cumulative_duration_ms"] >= failed_step["duration_ms"]

    with pytest.raises(RepoForgeError) as second:
        service.workspace_run_profile(workspace_id, "always_fail")
    guidance = second.value.details["retry_guidance"]
    assert guidance["identical_failure_repeat"] == 2
    assert "workspace_run_diagnostic" in second.value.safe_next_action


def test_edit_between_failures_resets_retry_guidance(forge_env: ForgeEnvironment) -> None:
    _append(forge_env, _ALWAYS_FAIL_PROFILE)
    _set_fast_fail_threshold(forge_env, 0)
    service = _reload_service(forge_env)
    created = service.workspace_create("demo", "retry guidance reset")
    workspace_id = created["workspace_id"]
    workspace_path = Path(created["path"])

    with pytest.raises(RepoForgeError):
        service.workspace_run_profile(workspace_id, "always_fail")
    (workspace_path / "scratch.txt").write_text("edited\n", encoding="utf-8")

    with pytest.raises(RepoForgeError) as second:
        service.workspace_run_profile(workspace_id, "always_fail")
    assert "retry_guidance" not in second.value.details


def test_not_found_carries_missing_dependency_guidance_on_first_failure(
    forge_env: ForgeEnvironment,
) -> None:
    _append(forge_env, _MISSING_TOOL_PROFILE)
    service = _reload_service(forge_env)
    created = service.workspace_create("demo", "retry guidance not found")
    workspace_id = created["workspace_id"]

    with pytest.raises(RepoForgeError) as exc:
        service.workspace_run_profile(workspace_id, "missing_tool")
    guidance = exc.value.details["retry_guidance"]
    assert "missing" in guidance["statements"][0].lower()
    assert "setup" in exc.value.safe_next_action.lower()


def test_fast_full_profile_failure_suggests_quick_or_diagnostic(
    forge_env: ForgeEnvironment,
) -> None:
    _append(forge_env, _ALWAYS_FAIL_PROFILE)
    service = _reload_service(forge_env)  # default 10s threshold
    created = service.workspace_create("demo", "retry guidance fast fail")
    workspace_id = created["workspace_id"]

    with pytest.raises(RepoForgeError) as exc:
        service.workspace_run_profile(workspace_id, "always_fail")
    guidance = exc.value.details["retry_guidance"]
    assert any("fast-fail" in statement for statement in guidance["statements"])
    assert "quick" in exc.value.safe_next_action.lower()


def test_success_path_is_unaffected_by_retry_guidance(forge_env: ForgeEnvironment) -> None:
    created = forge_env.service.workspace_create("demo", "retry guidance success")
    workspace_id = created["workspace_id"]
    Path(created["path"], "hello.txt").write_text("changed\n", encoding="utf-8")
    result = forge_env.service.workspace_run_profile(workspace_id, "full")
    assert result["commands"]
    assert all(command["stage_index"] >= 0 for command in result["commands"])
    assert all(command["duration_ms"] >= 0 for command in result["commands"])
    assert result["commands"][-1]["cumulative_duration_ms"] >= result["commands"][-1]["duration_ms"]
    assert set(result) == {
        "workspace_id",
        "repo_id",
        "profile",
        "description",
        "verification",
        "fingerprint",
        "commands",
        "change_metrics",
        "satisfies_commit_gate",
        "used_default",
        "head_sha",
        "command_source_dirty",
        "command_source_dirty_paths",
        "command_source_warning",
        "completed_steps",
        "failed_step",
        "failure_domain",
        "not_run_steps",
        "business_tests_ran",
        "valid_tdd_red_evidence",
        "hygiene_receipt",
        "working_directory",
    }


def test_failure_then_success_then_repeat_failure_starts_fresh(
    forge_env: ForgeEnvironment,
) -> None:
    _append(forge_env, _ALWAYS_FAIL_PROFILE)
    _set_fast_fail_threshold(forge_env, 0)
    service = _reload_service(forge_env)
    created = service.workspace_create("demo", "retry guidance success then fail")
    workspace_id = created["workspace_id"]

    with pytest.raises(RepoForgeError):
        service.workspace_run_profile(workspace_id, "always_fail")
    Path(created["path"], "hello.txt").write_text("changed\n", encoding="utf-8")
    service.workspace_run_profile(workspace_id, "full")  # a success in between

    with pytest.raises(RepoForgeError) as exc:
        service.workspace_run_profile(workspace_id, "always_fail")
    assert "retry_guidance" not in exc.value.details
