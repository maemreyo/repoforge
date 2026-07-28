"""The serving process must know which configuration generation it serves (#313).

The managed MCP runtime built its per-generation service container without passing
`config_generation`, so the container ran at the default 0 while the execution worker ran
the real generation. Every durable verification admitted through the connector was stamped
with generation 0, no worker could claim it, and ~30s later recovery terminalized it:

    OPERATION_GENERATION_STALE
    operation admitted at generation 0
    active worker generation 12

Observed on a live installation, and invisible to the whole test corpus because tests build
BOTH sides at generation 0 -- so the filter matched and everything ran. These tests assert
the production topology instead: the admitting side carries a real generation, and a worker
on that same generation can actually claim what it admitted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import create_forge_environment

from repoforge.application.operations.work_admission import DurableWorkAdmission
from repoforge.application.service import CodingService
from repoforge.bootstrap import build_application
from repoforge.config import load_config
from repoforge.domain.errors import ConfigError
from repoforge.domain.operation_work import OperationWorkRequest
from repoforge.interfaces.cli.main import _ensure_generation, build_generation_service_container


def _store_and_generation(config_path: Path) -> tuple[object, int]:
    """Resolve the generation the way the runtime does at startup.

    `_ensure_generation` is what `rf serve` calls: the fixture writes a bare config, and the
    runtime imports it into an immutable generation on first use. Reading the store directly
    would find no generation at all -- which is precisely how the corpus ended up exercising
    generation 0 everywhere.
    """
    store = _ensure_generation(config_path)
    generation = store.activation_target() or store.active() or store.current()
    assert generation is not None
    return store, generation.generation


def test_the_managed_runtime_container_carries_its_generation(tmp_path: Path) -> None:
    """The exact bug site: the container is built FROM a generation, so it must carry it."""
    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)
    assert generation > 0, "precondition: the fixture has an accepted generation"

    container = build_generation_service_container(store, generation, allow_incompatible=True)

    assert container.generation == generation
    # The load-bearing assertion: 0 here is what broke every durable verification.
    assert container.service.application.context.config_generation == generation


def test_work_admitted_by_the_runtime_is_claimable_by_a_worker_on_that_generation(
    tmp_path: Path,
) -> None:
    """The production topology the corpus never exercised: request side and worker side
    built independently, each with the REAL generation, must agree."""
    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)

    # Request side: exactly how the managed MCP runtime composes itself.
    request_side = build_generation_service_container(
        store, generation, allow_incompatible=True
    ).service
    queue = request_side.application.context.operation_work_queue
    assert queue is not None
    admitted = DurableWorkAdmission(request_side.application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
            config_generation=request_side.application.context.config_generation,
        ),
        operation_kind="workspace_run_profile",
    )

    work = queue.read(admitted.operation_id)
    assert work is not None
    assert work.request.config_generation == generation

    # Worker side: composed the way the execution worker is, with the same generation.
    worker_application = build_application(
        load_config(store.resolved_path(generation)), config_generation=generation
    )
    claimed = worker_application.context.operation_work_queue.claim_next(
        owner_id="worker-test",
        now="2026-07-28T10:00:00+00:00",
        lease_expires_at="2026-07-28T10:01:30+00:00",
        compatible_kinds=frozenset({"profile", "adhoc", "diagnostic"}),
        config_generation=worker_application.context.config_generation,
    )

    assert claimed is not None, "the worker could not claim work the runtime admitted"
    assert claimed.operation_id == admitted.operation_id


def test_a_generation_zero_request_side_is_what_broke(tmp_path: Path) -> None:
    """Pins the mechanism, so a future reader can see why 0 is not a harmless default.

    A worker running a real generation claims only matching work, so an item stamped 0 is
    invisible to it -- which is why the caller waited and recovery later terminalized it.
    """
    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)
    config = load_config(store.resolved_path(generation))

    # The old composition: no generation reaches the application.
    broken = CodingService(config, application=build_application(config))
    assert broken.application.context.config_generation == 0
    queue = broken.application.context.operation_work_queue
    assert queue is not None
    admitted = DurableWorkAdmission(broken.application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
            config_generation=broken.application.context.config_generation,
        ),
        operation_kind="workspace_run_profile",
    )
    assert queue.read(admitted.operation_id).request.config_generation == 0

    # A worker on the real generation cannot see it.
    worker_application = build_application(config, config_generation=generation)
    assert (
        worker_application.context.operation_work_queue.claim_next(
            owner_id="worker-test",
            now="2026-07-28T10:00:00+00:00",
            lease_expires_at="2026-07-28T10:01:30+00:00",
            compatible_kinds=frozenset({"profile", "adhoc", "diagnostic"}),
            config_generation=generation,
        )
        is None
    )


def test_recovery_terminalizes_the_unclaimable_item_with_both_generations(
    tmp_path: Path,
) -> None:
    """And the error an operator actually sees names both sides of the mismatch.

    No explicit recovery call: composing the worker application runs the startup sweep
    itself, which is exactly how the live installation's operations were terminalized.
    """
    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)
    config = load_config(store.resolved_path(generation))
    broken = CodingService(config, application=build_application(config))
    queue = broken.application.context.operation_work_queue
    assert queue is not None
    admitted = DurableWorkAdmission(broken.application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
            config_generation=0,
        ),
        operation_kind="workspace_run_profile",
    )

    worker_application = build_application(config, config_generation=generation)

    terminal = worker_application.operations.status(admitted.operation_id)
    assert terminal.error_code == "OPERATION_GENERATION_STALE"
    assert "generation 0" in (terminal.error_message or "")
    assert f"generation {generation}" in (terminal.error_message or "")
    # The sidecar is gone too, so nothing keeps retrying work that can never match.
    assert worker_application.context.operation_work_queue.read(admitted.operation_id) is None


def test_an_unknown_generation_is_still_refused(tmp_path: Path) -> None:
    """The extraction must not have dropped the factory's own guards."""
    env = create_forge_environment(tmp_path)
    store, _ = _store_and_generation(env.config_path)

    with pytest.raises(ConfigError, match="Unknown configuration generation"):
        build_generation_service_container(store, 9_999)


def test_work_from_a_newer_generation_is_left_for_the_replacement_worker(
    tmp_path: Path,
) -> None:
    """A hot reload swaps the request side before the supervisor replaces this worker.

    For those seconds the request side admits against generation N+1 while the worker still
    runs N. That work is valid -- just not this worker's to claim -- so recovery must leave
    it alone. A symmetric `!=` test failed it, destroying good work and reporting the
    operator's own config change as an error.
    """
    from repoforge.application.operations.recovery import recover_operation_work

    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)
    config = load_config(store.resolved_path(generation))
    application = build_application(config, config_generation=generation)
    queue = application.context.operation_work_queue
    assert queue is not None
    newer = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
            config_generation=generation + 1,
        ),
        operation_kind="workspace_run_profile",
    )

    report = recover_operation_work(
        application.operations,
        queue,
        now="2026-07-28T10:00:00+00:00",
        expected_config_generation=generation,
    )

    assert report.stale_generation == 0
    assert queue.read(newer.operation_id) is not None
    assert application.operations.status(newer.operation_id).error_code is None


def test_work_from_an_older_generation_is_still_terminalized(tmp_path: Path) -> None:
    """The rule this branch exists for must keep working: an older generation cannot run."""
    from repoforge.application.operations.recovery import recover_operation_work

    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)
    config = load_config(store.resolved_path(generation))
    application = build_application(config, config_generation=generation)
    queue = application.context.operation_work_queue
    assert queue is not None
    older = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
            config_generation=generation - 1,
        ),
        operation_kind="workspace_run_profile",
    )

    report = recover_operation_work(
        application.operations,
        queue,
        now="2026-07-28T10:00:00+00:00",
        expected_config_generation=generation,
    )

    assert report.stale_generation == 1
    assert application.operations.status(older.operation_id).error_code == (
        "OPERATION_GENERATION_STALE"
    )
