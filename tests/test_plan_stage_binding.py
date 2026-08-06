from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys

from conftest import ForgeEnvironment

from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.domain.operation_task import OperationState


def _running_plan_operation(forge_env: ForgeEnvironment) -> str:
    operations = forge_env.service.operations
    task = operations.create(
        kind="workspace_execute_plan",
        phase="queued",
        cancel_supported=True,
        workspace_id="workspace-plan-binding",
    )
    return operations.start(task.operation_id).operation_id


def test_plan_stage_token_persists_binding_before_launch_and_deletes_exact_binding_on_release(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    bindings = service.application.context.worker_bindings
    assert bindings is not None
    operation_id = _running_plan_operation(forge_env)
    token = service._plan_executor._durable_stage_token(operation_id)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        token.before_spawn()
        token.bind(child)

        binding = bindings.get(operation_id)
        assert binding is not None
        assert binding.child_pid == child.pid
        assert binding.child_pgid == child.pid
        assert binding.child_start_token is not None

        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=3)
        token.release()

        assert bindings.get(operation_id) is None
    finally:
        if child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)


def test_restarted_service_cancels_parent_plan_through_durable_stage_binding(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    bindings = service.application.context.worker_bindings
    assert bindings is not None
    operation_id = _running_plan_operation(forge_env)
    token = service._plan_executor._durable_stage_token(operation_id)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        token.before_spawn()
        token.bind(child)
        assert bindings.get(operation_id) is not None

        restarted = CodingService(load_config(forge_env.config_path))
        result = restarted.operation_cancel(operation_id)
        child.wait(timeout=3)

        assert result["operation"]["state"] == "cancelled"
        assert restarted.operations.status(operation_id).state is OperationState.CANCELLED
        assert restarted.application.context.worker_bindings is not None
        assert restarted.application.context.worker_bindings.get(operation_id) is None
    finally:
        token.release()
        if child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)
