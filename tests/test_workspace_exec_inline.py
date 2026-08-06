"""Coverage for issue #378: the `workspace_exec` inline execution fast path.

Before #378, every `workspace_exec` call -- even `background=False` -- was admitted to
the durable work queue and the calling thread polled for a separate worker to claim and
run it. This file pins down the fast path that replaces that round trip for a foreground
call: `WorkspaceExecutor` now calls `WorkspaceAdhocRunner` directly, skipping
`DurableWorkAdmission` entirely, while every existing check (argv/script validation,
circuit breakers, protected-ref, credential/lease/sandbox resolution) still applies
unchanged -- see `WorkspaceAdhocRunner.execute()`'s own docstring.

`background=True` is untouched by #378 and still goes through the durable queue; this
file also has the one regression test proving that split stayed intact.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment, durable_worker

from repoforge.domain.errors import ErrorCode, RepoForgeError


def _relaxed_env(
    tmp_path: Path,
    *,
    runners: tuple[str, ...] = ("python3",),
    adhoc_inline_max_seconds: int | None = None,
) -> ForgeEnvironment:
    return create_forge_environment(
        tmp_path,
        execution_mode="relaxed",
        adhoc_runners=runners,
        adhoc_inline_max_seconds=adhoc_inline_max_seconds,
    )


def _record_count(env: ForgeEnvironment) -> int:
    return len(env.service.operations.list_records(max_records=200).records)


def test_inline_path_creates_no_durable_record(tmp_path: Path) -> None:
    """AC3: a default (background=False) call must not create a durable operation
    record at all -- not "one that finishes fast", none whatsoever."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "inline no durable record")["workspace_id"]
    before = _record_count(env)

    result = env.service.workspace_exec(workspace_id, ("python3", "--version"))

    assert result["outcome"] == "passed"
    assert _record_count(env) == before


def test_rejected_runner_fails_before_any_durable_admission(tmp_path: Path) -> None:
    """AC1: a disallowed runner must fail closed -- and, concretely for the inline
    path, without ever writing a durable record for the rejected request."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "rejected runner inline")["workspace_id"]
    before = _record_count(env)

    with pytest.raises(RepoForgeError) as exc:
        env.service.workspace_exec(workspace_id, ("node", "--version"))

    assert exc.value.code is ErrorCode.ADHOC_RUNNER_NOT_ALLOWED
    assert _record_count(env) == before


def test_background_true_still_uses_the_durable_path(tmp_path: Path) -> None:
    """Regression: #378 only changes the background=False default. An explicit
    background=True call must still admit to the durable queue and return immediately
    with a pollable operation -- exactly as before the inline fast path existed."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "background still durable")["workspace_id"]
    before = _record_count(env)

    with durable_worker(env.service):
        result = env.service.workspace_exec(workspace_id, ("python3", "--version"), background=True)

        assert result["outcome"] == "running"
        assert result["operation"] is not None
        operation_id = result["operation"]["operation_id"]
        assert result["next_action"]
        assert _record_count(env) == before + 1

        status = env.service.operation_status(operation_id)
        assert status["kind"] == "workspace_run_adhoc"


def test_inline_ceiling_exceeded_promotes_instead_of_failing(tmp_path: Path) -> None:
    """Superseded by #379: a foreground call bounded by the smaller
    adhoc_inline_max_seconds ceiling used to fail fast (COMMAND_TIMEOUT) when #378 was
    the only issue landed. #379 replaces that with promotion -- the exact same
    already-running process is durably tracked instead of killed, so the caller gets
    outcome="running" with a real operation_id, not an exception. See
    tests/test_workspace_exec_promotion.py for the deeper same-process/no-restart and
    race-window proofs; this test only pins down workspace_exec's own observable
    outcome shape at the ceiling boundary."""
    env = _relaxed_env(tmp_path, adhoc_inline_max_seconds=1)
    workspace_id = env.service.workspace_create("demo", "inline ceiling exceeded")["workspace_id"]
    before = _record_count(env)

    result = env.service.workspace_exec(
        workspace_id, ("python3", "-c", "import time; time.sleep(2)")
    )

    assert result["outcome"] == "running"
    assert result["operation"] is not None
    assert result["operation"]["kind"] == "workspace_run_adhoc"
    assert _record_count(env) == before + 1


def test_framework_overhead_ms_present_only_for_inline(tmp_path: Path) -> None:
    """framework_overhead_ms is populated for the inline path (a non-negative float)
    and null for a background=True/durable result, whose queue+worker round trip it
    was never meant to measure (it would misrepresent that cost as framework cost)."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "framework overhead evidence")[
        "workspace_id"
    ]

    inline_result = env.service.workspace_exec(workspace_id, ("python3", "--version"))
    overhead = inline_result["framework_overhead_ms"]
    assert isinstance(overhead, float)
    assert overhead >= 0.0

    with durable_worker(env.service):
        background_result = env.service.workspace_exec(
            workspace_id, ("python3", "--version"), background=True
        )
    assert background_result["framework_overhead_ms"] is None


def test_framework_overhead_ms_is_a_sane_magnitude(tmp_path: Path) -> None:
    """AC2 targets a p95 framework overhead under 300ms for a short local command.
    This deliberately does NOT enforce that tight figure as a CI assertion -- a wall-
    clock bound that close to zero measures the test runner's own load, not the code,
    and has broken this suite's `main` before on a shared/loaded CI runner (see the
    #378 issue thread and the repo's own testing guidance against tight wall-clock
    assertions). Instead this asserts a generous structural ceiling: the durable path
    this replaces involves a JSON-file admission write plus a 0.1s-interval poll loop,
    so anything above a couple of seconds would mean the inline path regressed to that
    shape, not a runner-load false positive at 300ms. The actual sub-300ms figure is
    evidenced by a manual benchmark run recorded in the #378 closing evidence, not
    enforced here."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "framework overhead magnitude")[
        "workspace_id"
    ]

    samples = [
        env.service.workspace_exec(workspace_id, ("python3", "--version"))["framework_overhead_ms"]
        for _ in range(10)
    ]

    assert all(isinstance(sample, float) and sample >= 0.0 for sample in samples)
    assert all(sample < 5_000 for sample in samples)
