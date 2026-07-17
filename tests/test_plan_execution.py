from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.interfaces.mcp.server import create_server
from repoforge.testing.fakes import ManualBackgroundTaskRunner


def _manual_service(env: ForgeEnvironment) -> tuple[CodingService, ManualBackgroundTaskRunner]:
    runner = ManualBackgroundTaskRunner()
    config = load_config(env.config_path)
    app = build_application(config, overrides=AdapterOverrides(background_tasks=runner))
    return CodingService(config, application=app), runner


def _accepted_plan(service: CodingService, *, task_slug: str) -> tuple[str, str]:
    workspace_id = service.workspace_create("demo", task_slug)["workspace_id"]
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed for plan execution\n",
        current["sha256"],
    )
    plan = service.workspace_create_execution_plan(workspace_id, task_id=f"task-{task_slug}")
    service.workspace_accept_execution_plan(
        workspace_id,
        plan["plan_id"],
        task_id=f"task-{task_slug}",
    )
    return workspace_id, plan["plan_id"]


def test_iteration_execution_is_durable_and_never_grants_commit_eligibility(
    forge_env: ForgeEnvironment,
) -> None:
    service, runner = _manual_service(forge_env)
    workspace_id, plan_id = _accepted_plan(service, task_slug="iteration")

    admission = service.workspace_execute_plan(workspace_id, plan_id, through="iteration")
    assert admission["phase"] == "running"
    operation_id = admission["operation_id"]
    assert service.operation_status(operation_id)["kind"] == "workspace_execute_plan"

    runner.run(operation_id)

    final = service.operation_status(operation_id)
    assert final["state"] == "succeeded", (
        final["error_code"],
        final["error_message"],
        final.get("result"),
    )
    assert final["result_reference"] == f"workspace_execute_plan:{operation_id}"
    result = final["result"]
    assert result["plan_id"] == plan_id
    assert result["through"] == "iteration"
    assert result["satisfies_commit_gate"] is False
    assert [receipt["target"] for receipt in result["stage_receipts"]] == ["quick"]
    assert all(receipt["status"] == "succeeded" for receipt in result["stage_receipts"])
    assert service.workspace_status(workspace_id)["last_verification"] is None

    restarted = CodingService(load_config(forge_env.config_path))
    assert restarted.operation_status(operation_id)["result"] == result
    assert (
        restarted.workspace_execution_receipts(plan_id)["stage_receipts"]
        == result["stage_receipts"]
    )


def test_plan_execution_cancellation_before_first_stage_is_terminal_and_receipt_free(
    forge_env: ForgeEnvironment,
) -> None:
    service, runner = _manual_service(forge_env)
    workspace_id, plan_id = _accepted_plan(service, task_slug="cancel")

    admission = service.workspace_execute_plan(workspace_id, plan_id, through="full")
    cancelled = service.operation_cancel(admission["operation_id"])
    assert cancelled["cancellation_requested"] is True
    runner.run(admission["operation_id"])

    final = service.operation_status(admission["operation_id"])
    assert final["state"] == "cancelled"
    assert final["result"] is None
    assert service.workspace_execution_receipts(plan_id)["stage_receipts"] == []
    assert service.workspace_status(workspace_id)["last_verification"] is None


def test_full_execution_runs_final_profile_and_preserves_authoritative_receipt(
    forge_env: ForgeEnvironment,
) -> None:
    service, runner = _manual_service(forge_env)
    workspace_id, plan_id = _accepted_plan(service, task_slug="full")

    admission = service.workspace_execute_plan(workspace_id, plan_id, through="full")
    runner.run(admission["operation_id"])

    final = service.operation_status(admission["operation_id"])
    assert final["state"] == "succeeded", (
        final["error_code"],
        final["error_message"],
        final.get("result"),
    )
    result = final["result"]
    assert result["through"] == "full"
    assert result["satisfies_commit_gate"] is True
    assert [receipt["target"] for receipt in result["stage_receipts"]] == ["quick", "full"]
    assert result["stage_receipts"][-1]["boundary"] == "final"
    verification = service.workspace_status(workspace_id)["last_verification"]
    assert verification is not None
    assert verification["profile"] == "full"
    assert verification["fingerprint_matches"] is True


def test_execute_plan_rejects_unaccepted_or_stale_plan(forge_env: ForgeEnvironment) -> None:
    service, _ = _manual_service(forge_env)
    workspace_id = service.workspace_create("demo", "unaccepted plan")["workspace_id"]
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(workspace_id, "hello.txt", "changed\n", current["sha256"])
    plan = service.workspace_create_execution_plan(workspace_id)

    with pytest.raises(RepoForgeError) as unaccepted:
        service.workspace_execute_plan(workspace_id, plan["plan_id"], through="full")
    assert unaccepted.value.code is ErrorCode.APPROVAL_REQUIRED

    service.workspace_accept_execution_plan(workspace_id, plan["plan_id"])
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed again after acceptance\n",
        current["sha256"],
    )
    with pytest.raises(RepoForgeError) as stale:
        service.workspace_execute_plan(workspace_id, plan["plan_id"], through="full")
    assert stale.value.code is ErrorCode.STATE_STALE


def test_plan_execution_failure_records_partial_receipt_and_stops_required_stage(
    forge_env: ForgeEnvironment,
) -> None:
    service, runner = _manual_service(forge_env)
    workspace_id, _plan_id = _accepted_plan(service, task_slug="failure")
    workspace = service.application.context.store.load(workspace_id)
    path = Path(workspace.path)
    (path / "hello.txt").write_text("does not satisfy full profile\n", encoding="utf-8")

    # Re-create and accept against the current failing tree so execution itself reaches
    # the final profile rather than failing the admission staleness check.
    plan = service.workspace_create_execution_plan(workspace_id, task_id="task-failure-current")
    service.workspace_accept_execution_plan(
        workspace_id,
        plan["plan_id"],
        task_id="task-failure-current",
    )
    admission = service.workspace_execute_plan(workspace_id, plan["plan_id"], through="full")
    runner.run(admission["operation_id"])

    final = service.operation_status(admission["operation_id"])
    assert final["state"] == "failed"
    assert final["error_code"] == ErrorCode.COMMAND_FAILED.value
    receipts = service.workspace_execution_receipts(plan["plan_id"])["stage_receipts"]
    assert [receipt["target"] for receipt in receipts] == ["quick", "full"]
    assert receipts[0]["status"] == "succeeded"
    assert receipts[1]["status"] == "failed"
    assert receipts[1]["result_reference"] is not None
    assert service.workspace_status(workspace_id)["last_verification"] is None


@pytest.mark.anyio
async def test_workspace_execute_plan_is_exposed_through_actual_mcp_session(
    forge_env: ForgeEnvironment,
) -> None:
    service, runner = _manual_service(forge_env)
    workspace_id, plan_id = _accepted_plan(service, task_slug="mcp")
    server = create_server(service=service)

    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        execute = tools["workspace_execute_plan"]
        assert execute.annotations.readOnlyHint is False
        assert execute.annotations.destructiveHint is False
        assert execute.annotations.openWorldHint is False
        assert set(execute.inputSchema["properties"]) == {
            "workspace_id",
            "plan_id",
            "through",
        }
        called = await session.call_tool(
            "workspace_execute_plan",
            {"workspace_id": workspace_id, "plan_id": plan_id, "through": "iteration"},
        )
        assert called.isError is False
        operation_id = called.structuredContent["operation_id"]

    runner.run(operation_id)
    assert service.operation_status(operation_id)["state"] == "succeeded"
