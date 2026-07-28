"""An expensive profile must not be repeatable at will by a model (#324).

On this installation an agent started the `full` profile -- the authoritative production
gate, 30-minute timeout, the whole test suite with coverage -- to sanity-check an unrelated
runtime fix. That is a reasonable-looking call and the wrong one, and nothing stopped it:

* admission had no notion of identical work already running, so N calls meant N runs;
* `force_rerun=true` bypassed receipt reuse freely, which is exactly what the agent passed;
* the configuration could not express that a profile is too expensive for the model to
  start on its own judgement.

Three guards, in increasing order of policy: join identical in-flight work, let the
reviewed configuration reserve a profile for the operator, and bound how often a model may
repeat one against the same workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import TEST_CONFIG_GENERATION, create_forge_environment, durable_worker

from repoforge.application.audit_context import bind_audit_attribution
from repoforge.application.operations.work_admission import DurableWorkAdmission
from repoforge.bootstrap import build_application
from repoforge.config import load_config
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.operation_work import OperationWorkRequest


def _request(*, profile: str = "quick", fingerprint: str = "b" * 64) -> OperationWorkRequest:
    return OperationWorkRequest.profile(
        workspace_id="workspace-1",
        profile_name=profile,
        expected_head_sha="a" * 40,
        expected_fingerprint=fingerprint,
        config_generation=TEST_CONFIG_GENERATION,
    )


def _admission(tmp_path: Path):
    env = create_forge_environment(tmp_path)
    application = build_application(
        load_config(env.config_path), config_generation=TEST_CONFIG_GENERATION
    )
    queue = application.context.operation_work_queue
    assert queue is not None
    return DurableWorkAdmission(application.operations, queue), application, queue


# ------------------------------------------------- single-flight admission


def test_identical_work_already_running_is_joined_not_duplicated(tmp_path: Path) -> None:
    admission, _application, queue = _admission(tmp_path)

    first = admission.admit(_request(), operation_kind="workspace_run_profile")
    second = admission.admit(_request(), operation_kind="workspace_run_profile")

    assert second.operation_id == first.operation_id, "a duplicate run was admitted"
    assert len(queue.list_records(max_records=10).records) == 1


def test_a_changed_snapshot_is_different_work_and_still_runs(tmp_path: Path) -> None:
    """Joining must never swallow a run against a tree that actually changed."""
    admission, _application, queue = _admission(tmp_path)

    first = admission.admit(_request(fingerprint="b" * 64), operation_kind="workspace_run_profile")
    second = admission.admit(_request(fingerprint="c" * 64), operation_kind="workspace_run_profile")

    assert second.operation_id != first.operation_id
    assert len(queue.list_records(max_records=10).records) == 2


def test_a_different_profile_is_different_work(tmp_path: Path) -> None:
    admission, _application, queue = _admission(tmp_path)

    first = admission.admit(_request(profile="quick"), operation_kind="workspace_run_profile")
    second = admission.admit(_request(profile="full"), operation_kind="workspace_run_profile")

    assert second.operation_id != first.operation_id
    assert len(queue.list_records(max_records=10).records) == 2


def test_a_terminal_operation_does_not_block_a_fresh_run(tmp_path: Path) -> None:
    """Joining a finished operation would hand back yesterday's answer as today's run."""
    admission, application, _queue = _admission(tmp_path)

    first = admission.admit(_request(), operation_kind="workspace_run_profile")
    application.operations.fail(first.operation_id, error_code="TEST_TERMINAL")

    second = admission.admit(_request(), operation_kind="workspace_run_profile")

    assert second.operation_id != first.operation_id


# ------------------------------------------- configuration policy on profiles


def test_a_reserved_profile_is_refused_for_the_model(tmp_path: Path) -> None:
    from repoforge.application.service import CodingService
    from repoforge.config import ProfileConfig

    env = create_forge_environment(tmp_path)
    config = load_config(env.config_path)
    service = CodingService(
        config,
        application=build_application(config, config_generation=TEST_CONFIG_GENERATION),
    )
    repo = service.config.repositories["demo"]
    repo.profiles["gate"] = ProfileConfig(
        name="gate",
        description="authoritative gate",
        commands=(("python3", "-c", "print('gate')"),),
        verification=True,
        model_invocable=False,
    )
    workspace_id = service.workspace_create("demo", "reserved profile")["workspace_id"]

    with bind_audit_attribution(origin="model"), pytest.raises(RepoForgeError) as raised:
        service.workspace_verify(workspace_id, mode="profile", profile_name="gate")

    assert raised.value.code is ErrorCode.PROFILE_NOT_MODEL_INVOCABLE
    assert "operator" in str(raised.value.safe_next_action or "")


def test_the_operator_can_still_run_a_reserved_profile(tmp_path: Path) -> None:
    """Reserving a profile from the model must not take it away from the operator."""
    from repoforge.application.service import CodingService
    from repoforge.config import ProfileConfig

    env = create_forge_environment(tmp_path)
    config = load_config(env.config_path)
    service = CodingService(
        config,
        application=build_application(config, config_generation=TEST_CONFIG_GENERATION),
    )
    service.config.repositories["demo"].profiles["gate"] = ProfileConfig(
        name="gate",
        description="authoritative gate",
        commands=(("python3", "-c", "print('gate ok')"),),
        verification=True,
        model_invocable=False,
    )
    workspace_id = service.workspace_create("demo", "operator run")["workspace_id"]

    # No model attribution bound: this is the CLI/operator path.
    with durable_worker(service):
        result = service.workspace_verify(workspace_id, mode="profile", profile_name="gate")

    # The reservation must not have refused it; the shape of a completed verify is enough.
    assert result.get("error_code") is None
    assert "operation" in result or "verification" in result or "profile" in result


def test_model_invocable_defaults_to_true_so_configurations_do_not_change(
    tmp_path: Path,
) -> None:
    env = create_forge_environment(tmp_path)
    config = load_config(env.config_path)

    for repo in config.repositories.values():
        for profile in repo.profiles.values():
            assert profile.model_invocable is True
            assert profile.min_interval_seconds == 0


def test_the_configuration_rejects_a_non_boolean_reservation(tmp_path: Path) -> None:
    from repoforge.domain.errors import ConfigError

    env = create_forge_environment(tmp_path)
    text = env.config_path.read_text(encoding="utf-8")
    text += '\n[repositories.demo.profiles.broken]\ncommands = [["true"]]\nmodel_invocable = "no"\n'
    env.config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="model_invocable must be a boolean"):
        load_config(env.config_path)


def test_the_configuration_bounds_the_rerun_interval(tmp_path: Path) -> None:
    from repoforge.domain.errors import ConfigError

    env = create_forge_environment(tmp_path)
    text = env.config_path.read_text(encoding="utf-8")
    text += (
        '\n[repositories.demo.profiles.slow]\ncommands = [["true"]]\n'
        "min_interval_seconds = 999999\n"
    )
    env.config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="min_interval_seconds"):
        load_config(env.config_path)


# ---------------- the policy must survive the durable patch it lives in


def test_a_policy_patch_carries_the_reservation_into_the_resolved_config() -> None:
    """On a real installation the profiles a model can reach come from the durable patch.

    A policy the patch cannot express is unreachable, and the patch rejects unknown keys --
    so adding the fields to `ProfileConfig` alone would have looked complete while doing
    nothing for the configuration that actually matters.
    """
    from repoforge.domain.policy_patch import ProfilePatch

    patch = ProfilePatch.from_table(
        "gate",
        {
            "description": "authoritative gate",
            "verification": True,
            "commands": [["scripts/verify-production.sh", "--allow-dirty"]],
            "timeout_seconds": 1800,
            "model_invocable": False,
            "min_interval_seconds": 900,
        },
    )

    assert patch.model_invocable is False
    assert patch.min_interval_seconds == 900
    table = patch.as_table()
    assert table["model_invocable"] is False
    assert table["min_interval_seconds"] == 900


def test_the_patch_omits_the_fields_when_they_are_default() -> None:
    """Rendering a default would churn every existing resolved generation."""
    from repoforge.domain.policy_patch import ProfilePatch

    table = ProfilePatch.from_table(
        "quick", {"commands": [["true"]], "description": "cheap"}
    ).as_table()

    assert "model_invocable" not in table
    assert "min_interval_seconds" not in table


def test_the_patch_rejects_an_invalid_reservation() -> None:
    from repoforge.domain.policy_patch import PolicyPatchError, ProfilePatch

    with pytest.raises(PolicyPatchError, match="model_invocable must be a boolean"):
        ProfilePatch.from_table("gate", {"commands": [["true"]], "model_invocable": "false"})
    with pytest.raises(PolicyPatchError, match="min_interval_seconds"):
        ProfilePatch.from_table("gate", {"commands": [["true"]], "min_interval_seconds": -1})


def test_an_old_release_would_have_rejected_these_keys() -> None:
    """Why the ordering matters: the patch is strict about unknown keys.

    Adding the fields to a live configuration before the runtime understands them would
    make the whole patch unparseable, which is why the config edit has to follow the
    activation rather than lead it.
    """
    from repoforge.domain.policy_patch import PolicyPatchError, ProfilePatch

    with pytest.raises(PolicyPatchError, match="unsupported keys"):
        ProfilePatch.from_table("gate", {"commands": [["true"]], "not_a_field": 1})
