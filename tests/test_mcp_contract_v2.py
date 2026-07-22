from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import ValidationError

from repoforge.contracts import generated_contract_identity
from repoforge.contracts.registry import (
    V2_TOOL_NAMES,
    V2_TOOL_SPECS,
    contract_schema_digests,
    render_contract_identity_artifact,
    render_v2_schema_bundle,
    validate_generated_contract_artifact,
)
from repoforge.domain.errors import ConfigError, ErrorCode, RepoForgeError
from repoforge.domain.runtime_contract import RuntimeContractIdentity, changed_contract_fields
from repoforge.interfaces.mcp.grace import (
    FORGE_V1_IDENTITY,
    create_grace_server,
)
from repoforge.interfaces.mcp.server import (
    FORGE_V2_IDENTITY,
    _compute_server_build_sha,
    create_server,
    tool_surface_hash,
)


def _failure_payload() -> dict[str, object]:
    return {
        "status": "failed",
        "summary": "Request failed",
        "error": {
            "code": "NOT_FOUND",
            "message": "Repository not found",
            "why": "The repository id is not enrolled.",
            "retryable": False,
            "safe_next_action": "Choose an enrolled repository.",
            "details": {"correlation_id": "corr-1"},
            "unchanged_state": ["No state changed."],
            "automatic_retry_allowed": False,
        },
    }


def test_every_advertised_output_schema_accepts_shared_failure() -> None:
    for spec in V2_TOOL_SPECS.values():
        validated = spec.validate_output(_failure_payload())
        assert validated.status == "failed"
        assert "anyOf" in spec.output_schema()


@pytest.mark.anyio
async def test_forge_v2_identity_publishes_exact_authoritative_roster_and_schemas(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)

    assert server.name == FORGE_V2_IDENTITY == "forge_v2"
    tools = await server.list_tools()
    assert tuple(tool.name for tool in tools) == V2_TOOL_NAMES
    assert len(tools) == 28
    assert {"repo_status", "workspace_run_profile", "repo_policy_apply"}.isdisjoint(
        tool.name for tool in tools
    )

    for tool in tools:
        spec = V2_TOOL_SPECS[tool.name]
        assert tool.inputSchema == spec.input_model.model_json_schema(mode="validation")
        assert tool.outputSchema == spec.output_schema()
        assert tool.annotations is not None


@pytest.mark.anyio
async def test_protocol_rejects_undeclared_input_before_dispatch(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {"undeclared": True})

    assert result.isError is True
    rendered = "\n".join(getattr(item, "text", "") for item in result.content)
    assert "extra_forbidden" in rendered or "Extra inputs are not permitted" in rendered
    assert result.structuredContent is not None
    assert result.structuredContent["status"] == "failed"


@pytest.mark.anyio
async def test_protocol_validates_output_against_authoritative_model() -> None:
    class InvalidOutputService:
        config: Any = None
        metrics: Any = None

        def repo_list_v2(self, **_: object) -> dict[str, object]:
            return {"status": "ok", "unexpected": True}

    server = create_server(service=InvalidOutputService())  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {})

    assert result.isError is True
    rendered = "\n".join(getattr(item, "text", "") for item in result.content)
    assert "unexpected" in rendered
    assert "extra_forbidden" in rendered or "Extra inputs are not permitted" in rendered
    assert result.structuredContent is not None
    assert result.structuredContent["status"] == "failed"


@pytest.mark.anyio
async def test_protocol_workspace_mutate_surfaces_parse_failure_in_same_response(
    forge_env: ForgeEnvironment,
) -> None:
    workspace_id = forge_env.service.workspace_create("demo", "mcp syntax gate")["workspace_id"]
    status = forge_env.service.workspace_status(workspace_id)
    server = create_server(service=forge_env.service)

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "workspace_mutate",
            {
                "workspace_id": workspace_id,
                "operations": [
                    {
                        "op": "create",
                        "path": "src/broken.py",
                        "content": "def broken(:\n",
                    }
                ],
                "expected_head_sha": status["head_sha"],
                "expected_workspace_fingerprint": status["workspace_fingerprint"],
            },
        )

    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None
    assert "parse_ok=false" in payload["summary"]
    assert payload["syntax_diagnostics"]["state"] == "error"
    assert payload["syntax_diagnostics"]["parse_ok"] is False
    assert payload["syntax_diagnostics"]["diagnostics"][0]["path"] == "src/broken.py"
    V2_TOOL_SPECS["workspace_mutate"].validate_output(payload)


@pytest.mark.anyio
async def test_protocol_error_is_one_redacted_typed_envelope(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "repo_read",
            {"repo_id": "missing", "files": [{"path": "README.md"}]},
        )

    assert result.isError is True
    rendered = "\n".join(getattr(item, "text", "") for item in result.content)
    envelope = json.loads(rendered)
    assert envelope["status"] == "failed"
    assert isinstance(envelope["summary"], str) and envelope["summary"]
    error = envelope["error"]
    assert set(error) >= {
        "code",
        "message",
        "why",
        "retryable",
        "safe_next_action",
        "details",
        "unchanged_state",
        "automatic_retry_allowed",
    }
    assert (
        isinstance(error["details"]["correlation_id"], str) and error["details"]["correlation_id"]
    )
    # The typed error envelope must be real structuredContent, not only
    # recoverable by parsing JSON out of the text block (#225 review), and it
    # must conform to the same {status, summary, error: ToolError} contract
    # every one of the 28 tools' own output model inherits from ToolResponse
    # -- not an ad-hoc shape a client cannot validate against any advertised
    # output schema.
    assert result.structuredContent == envelope
    validated = V2_TOOL_SPECS["repo_read"].validate_output(envelope)
    assert validated.status == "failed"


@pytest.mark.anyio
async def test_protocol_pr_remote_version_stale_preserves_current_token() -> None:
    expected = "prv2:" + "a" * 64
    actual = "prv2:" + "b" * 64

    class StalePrService:
        config: Any = None
        metrics: Any = None

        def workspace_pr(self, **_: object) -> dict[str, object]:
            raise RepoForgeError(
                "PR_REMOTE_VERSION_STALE: pull request changed since review",
                code=ErrorCode.PR_REMOTE_VERSION_STALE,
                retryable=False,
                safe_next_action="Read workspace_pr_evidence overview and retry with its remote_version.",
                unchanged_state=("No pull-request write was attempted.",),
                details={
                    "field": "expected_remote_version",
                    "expected": expected,
                    "actual": actual,
                    "current_remote_version": actual,
                    "current_head_sha": "c" * 40,
                    "current_updated_at": "2026-07-21T14:00:00Z",
                    "remote_delta": [
                        "current_title=Concurrent title",
                        "comments=1",
                    ],
                    "recovery_action": "reread_pr_overview",
                    "result_reference": "workspace_pr_evidence:overview",
                },
            )

    server = create_server(service=StalePrService())  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "workspace_pr",
            {
                "workspace_id": "workspace-1",
                "action": "update",
                "title": "Reviewed update",
                "idempotency_key": "stale-pr-version-0001",
                "expected_remote_version": expected,
            },
        )

    assert result.isError is True
    assert result.structuredContent is not None
    error = result.structuredContent["error"]
    assert error["code"] == "PR_REMOTE_VERSION_STALE"
    assert error["retryable"] is False
    assert error["automatic_retry_allowed"] is False
    assert error["details"]["field"] == "expected_remote_version"
    assert error["details"]["expected"] == expected
    assert error["details"]["actual"] == actual
    assert error["details"]["current_remote_version"] == actual
    assert error["details"]["current_head_sha"] == "c" * 40
    assert error["details"]["current_updated_at"] == "2026-07-21T14:00:00Z"
    assert error["details"]["remote_delta"] == [
        "current_title=Concurrent title",
        "comments=1",
    ]
    assert error["details"]["recovery_action"] == "reread_pr_overview"
    assert error["details"]["result_reference"] == "workspace_pr_evidence:overview"
    V2_TOOL_SPECS["workspace_pr"].validate_output(result.structuredContent)


@pytest.mark.anyio
async def test_protocol_pr_remote_version_incomplete_preserves_missing_coverage() -> None:
    class IncompletePrService:
        config: Any = None
        metrics: Any = None

        def workspace_pr_evidence(self, **_: object) -> dict[str, object]:
            raise RepoForgeError(
                "PR_REMOTE_VERSION_INCOMPLETE: provider snapshot was truncated",
                code=ErrorCode.PR_REMOTE_VERSION_INCOMPLETE,
                retryable=False,
                details={
                    "field": "remote_version",
                    "actual": "comments:truncated,reviews:truncated",
                    "missing_coverage": ["comments:truncated", "reviews:truncated"],
                    "result_reference": "workspace_pr_evidence:overview",
                },
            )

    server = create_server(service=IncompletePrService())  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "workspace_pr_evidence",
            {"workspace_id": "workspace-1", "detail": "overview"},
        )

    assert result.isError is True
    assert result.structuredContent is not None
    error = result.structuredContent["error"]
    assert error["code"] == "PR_REMOTE_VERSION_INCOMPLETE"
    assert error["details"]["missing_coverage"] == [
        "comments:truncated",
        "reviews:truncated",
    ]
    V2_TOOL_SPECS["workspace_pr_evidence"].validate_output(result.structuredContent)


@pytest.mark.anyio
async def test_protocol_operation_wait_returns_typed_terminal_evidence(
    forge_env: ForgeEnvironment,
) -> None:
    manager = forge_env.service.operations
    task = manager.create(kind="watch", phase="queued", cancel_supported=False)
    running = manager.start(task.operation_id)
    manager.succeed(task.operation_id, result_reference="watch:done")
    server = create_server(service=forge_env.service)

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "operation",
            {
                "action": "wait",
                "operation_id": task.operation_id,
                "since_updated_at": running.updated_at,
                "timeout_seconds": 60,
            },
        )

    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None
    assert payload["action"] == "wait"
    assert payload["changed_since"] is True
    assert payload["timed_out"] is False
    operation = payload["operation"]
    assert operation["terminal"] is True
    assert operation["suggested_poll_after_s"] is None
    assert operation["eta_seconds"] == 0.0
    V2_TOOL_SPECS["operation"].validate_output(payload)


def test_surface_hash_is_v2_identity_bound_and_version_negotiation_is_gone() -> None:
    assert tool_surface_hash() == tool_surface_hash()
    assert len(tool_surface_hash()) == 64
    with pytest.raises(ValueError, match="only supports contract v2"):
        tool_surface_hash(1)


@pytest.mark.anyio
async def test_retired_forge_v1_identity_exposes_one_typed_grace_error() -> None:
    stale_calls: list[dict[str, object]] = []
    shutdown_requests: list[bool] = []
    server = create_grace_server(
        on_stale_caller=stale_calls.append,
        request_shutdown=lambda: shutdown_requests.append(True),
    )

    assert server.name == FORGE_V1_IDENTITY == "forge_v1"
    tools = await server.list_tools()
    assert [tool.name for tool in tools] == ["migration_required"]

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "migration_required",
            {"reported_surface_hash": "stale-surface"},
        )

    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None
    assert payload["status"] == "failed"
    assert payload["error_code"] == "CONNECTOR_RETIRED"
    assert payload["retired_identity"] == FORGE_V1_IDENTITY
    assert payload["new_identity"] == FORGE_V2_IDENTITY
    assert payload["expected_surface_hash"] == tool_surface_hash()
    assert payload["reported_surface_hash"] == "stale-surface"
    assert payload["shutdown_required"] is True
    assert stale_calls and stale_calls[0]["surface_mismatch"] is True
    assert shutdown_requests == [True]


def test_authoritative_models_still_fail_closed_independently() -> None:
    with pytest.raises(ValidationError):
        V2_TOOL_SPECS["repo_list"].validate_input({"undeclared": True})


def test_config_inspect_remains_compatible_with_legacy_success_payloads() -> None:
    legacy = {
        "status": "ok",
        "summary": "Inspected accepted configuration generation 1",
        "accepted": {
            "generation": 1,
            "state": "accepted",
            "digest": "a" * 64,
            "changed_sections": ["repositories"],
        },
        "active": None,
        "pending": [],
        "capability_delta": "equivalent",
        "restart_required": True,
        "repo_facts": [],
    }

    validated = V2_TOOL_SPECS["config_inspect"].validate_success_output(legacy)

    assert validated.contract_identity is None
    assert validated.config_projection is None


def _contract_identity(**changes: object) -> RuntimeContractIdentity:
    base = RuntimeContractIdentity(
        server_build_sha="a" * 64,
        server_version="2.0.0",
        active_generation=7,
        tool_surface_hash="b" * 64,
        input_contract_digest="c" * 64,
        output_contract_digest="d" * 64,
        runtime_protocol_version=1,
        process_start_identity="e" * 64,
    )
    return replace(base, **changes)


def test_contract_schema_digests_are_deterministic_and_separate_input_from_output() -> None:
    first = contract_schema_digests()
    second = contract_schema_digests()

    assert first == second
    assert first.input_digest != first.output_digest
    assert len(first.input_digest) == len(first.output_digest) == 64
    assert first.tool_count == 28


def test_server_build_sha_fingerprints_package_bytes_and_ignores_bytecode(tmp_path: Path) -> None:
    package = tmp_path / "repoforge"
    package.mkdir()
    module = package / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")

    first = _compute_server_build_sha(package)

    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-313.pyc").write_bytes(b"ignored bytecode")
    assert _compute_server_build_sha(package) == first

    module.write_text("VALUE = 2\n", encoding="utf-8")
    assert _compute_server_build_sha(package) != first


def test_changed_contract_fields_reports_exact_digest_or_generation_skew() -> None:
    expected = _contract_identity()
    actual = _contract_identity(active_generation=8, output_contract_digest="f" * 64)

    assert changed_contract_fields(expected, actual) == (
        "active_generation",
        "output_contract_digest",
    )


def test_packaged_contract_identity_matches_the_live_registry() -> None:
    assert render_contract_identity_artifact() == generated_contract_identity.CONTRACT_IDENTITY


def test_generated_contract_artifact_mismatch_fails_closed_without_host_path(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "tool-schemas-v2.json"
    tampered = render_v2_schema_bundle()
    tampered["tool_count"] = 27
    artifact.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(ConfigError, match="CONTRACT_ARTIFACT_MISMATCH") as captured:
        validate_generated_contract_artifact(artifact)

    assert str(tmp_path) not in str(captured.value)


@pytest.mark.anyio
async def test_discovery_and_success_response_expose_the_same_runtime_identity(
    forge_env: ForgeEnvironment,
) -> None:
    identity = _contract_identity()
    server = create_server(
        service=forge_env.service,
        contract_identity_provider=lambda: identity,
    )

    tools = await server.list_tools()
    assert len(tools) == 28
    direct_discovery = [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools
    ]
    for dumped in direct_discovery:
        assert dumped["_meta"]["repoforge_contract_identity"] == identity.as_dict()

    async with create_connected_server_and_client_session(server) as session:
        protocol_discovery = await session.list_tools()
        protocol_tools = [
            tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in protocol_discovery.tools
        ]
        assert protocol_tools == direct_discovery
        result = await session.call_tool("repo_list", {})

    assert result.isError is False
    dumped_result = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert dumped_result["_meta"]["repoforge_contract_identity"] == identity.as_dict()


@pytest.mark.anyio
async def test_stale_discovery_identity_is_rejected_before_mutation_handler() -> None:
    current = [_contract_identity()]

    class Service:
        config: Any = None
        metrics: Any = None

        def __init__(self) -> None:
            self.calls = 0

        def workspace_create_v2(self, **_: object) -> dict[str, object]:
            self.calls += 1
            raise AssertionError("stale contract must fail before the application use case")

    service = Service()
    server = create_server(
        service=service,  # type: ignore[arg-type]
        contract_identity_provider=lambda: current[0],
    )
    async with create_connected_server_and_client_session(server) as session:
        discovered = await session.list_tools()
        assert len(discovered.tools) == 28
        current[0] = _contract_identity(input_contract_digest="f" * 64)
        result = await session.call_tool(
            "workspace_create",
            {"repo_id": "demo", "task_slug": "contract-skew"},
        )

    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["error"]["code"] == "CLIENT_CONTRACT_STALE"
    assert result.structuredContent["error"]["retryable"] is False
    assert result.structuredContent["error"]["automatic_retry_allowed"] is False
    assert "input_contract_digest" in result.structuredContent["error"]["message"]
    assert "reconnect" in result.structuredContent["error"]["safe_next_action"].lower()
    assert service.calls == 0


@pytest.mark.anyio
async def test_repo_selection_is_pinned_and_reused_within_one_session(
    forge_env: ForgeEnvironment,
) -> None:
    identity = _contract_identity()
    server = create_server(
        service=forge_env.service,
        contract_identity_provider=lambda: identity,
    )

    async with create_connected_server_and_client_session(server) as session:
        discovered = await session.list_tools()
        assert len(discovered.tools) == 28

        selected = await session.call_tool("repo_list", {"requested_repo": "demo"})
        assert selected.isError is False
        assert selected.structuredContent is not None
        selection = selected.structuredContent["selection"]
        selection_id = selection["repo_selection_id"]
        assert selection_id.startswith("selection:")
        assert selection["selection_generation"] == identity.active_generation
        assert len(selection["capability_digest"]) == 64
        assert selection["expires_at"]

        reused = await session.call_tool("repo_tree", {"repo_id": "demo"})

    assert reused.isError is False
    dumped = reused.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert dumped["_meta"]["repoforge_repository_selection"] == {
        "repo_selection_id": selection_id,
        "repo_id": "demo",
        "selection_generation": identity.active_generation,
        "capability_digest": selection["capability_digest"],
        "expires_at": selection["expires_at"],
    }

    audit_events = [
        json.loads(line)
        for line in (forge_env.root / "state" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    repo_list_event = next(event for event in audit_events if event["action"] == "repo_list")
    assert repo_list_event["details"]["origin"] == "model"
    assert len(repo_list_event["details"]["session_hash"]) == 24
    assert forge_env.service.metrics is not None
    calls_by_origin = forge_env.service.metrics.snapshot()["calls_by_origin"]
    assert calls_by_origin["repo_list"]["model"]["count"] == 1
    assert calls_by_origin["repo_tree_v2"]["model"]["count"] == 1


@pytest.mark.anyio
async def test_generation_change_invalidates_the_session_repository_selection(
    forge_env: ForgeEnvironment,
) -> None:
    current = [_contract_identity()]
    server = create_server(
        service=forge_env.service,
        contract_identity_provider=lambda: current[0],
    )

    async with create_connected_server_and_client_session(server) as session:
        await session.list_tools()
        selected = await session.call_tool("repo_list", {"requested_repo": "demo"})
        assert selected.isError is False
        assert server._session_repository_selections

        current[0] = replace(
            current[0],
            active_generation=current[0].active_generation + 1,
        )
        stale = await session.call_tool("repo_tree", {"repo_id": "demo"})

    assert stale.isError is True
    assert stale.structuredContent is not None
    assert stale.structuredContent["error"]["code"] == "CLIENT_CONTRACT_STALE"
    assert "repo_list" in stale.structuredContent["error"]["safe_next_action"]
    assert server._session_repository_selections == {}


@pytest.mark.anyio
async def test_concurrent_sessions_keep_different_repository_selections_isolated(
    forge_env: ForgeEnvironment,
) -> None:
    repositories = forge_env.service.config.repositories
    original = repositories["demo"]
    repositories["other"] = replace(
        original,
        repo_id="other",
        display_name="Other Repository",
    )
    try:
        identity = _contract_identity()
        server = create_server(
            service=forge_env.service,
            contract_identity_provider=lambda: identity,
        )
        async with (
            create_connected_server_and_client_session(server) as first_session,
            create_connected_server_and_client_session(server) as second_session,
        ):
            await first_session.list_tools()
            await second_session.list_tools()
            first = await first_session.call_tool("repo_list", {"requested_repo": "demo"})
            second = await second_session.call_tool("repo_list", {"requested_repo": "other"})
            first_tree = await first_session.call_tool("repo_tree", {"repo_id": "demo"})
            second_tree = await second_session.call_tool("repo_tree", {"repo_id": "other"})
    finally:
        repositories.pop("other", None)

    assert first.isError is False
    assert second.isError is False
    assert first.structuredContent is not None
    assert second.structuredContent is not None
    first_selection = first.structuredContent["selection"]
    second_selection = second.structuredContent["selection"]
    assert first_selection["repo_id"] == "demo"
    assert second_selection["repo_id"] == "other"
    assert first_selection["repo_selection_id"] != second_selection["repo_selection_id"]
    assert first_tree.isError is False
    assert second_tree.isError is False


@pytest.mark.anyio
async def test_capability_change_invalidates_the_session_repository_selection(
    forge_env: ForgeEnvironment,
) -> None:
    repositories = forge_env.service.config.repositories
    original = repositories["demo"]
    identity = _contract_identity()
    server = create_server(
        service=forge_env.service,
        contract_identity_provider=lambda: identity,
    )
    try:
        async with create_connected_server_and_client_session(server) as session:
            await session.list_tools()
            selected = await session.call_tool("repo_list", {"requested_repo": "demo"})
            assert selected.isError is False
            repositories["demo"] = replace(
                original,
                publish_enabled=not original.publish_enabled,
            )
            stale = await session.call_tool("repo_tree", {"repo_id": "demo"})
    finally:
        repositories["demo"] = original

    assert stale.isError is True
    assert stale.structuredContent is not None
    assert stale.structuredContent["error"]["code"] == "STALE_STATE"
    assert "capability" in stale.structuredContent["error"]["message"]
    assert "repo_list" in stale.structuredContent["error"]["safe_next_action"]
    assert server._session_repository_selections == {}


@pytest.mark.anyio
async def test_expired_session_repository_selection_is_rejected_before_dispatch(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _contract_identity()
    server = create_server(
        service=forge_env.service,
        contract_identity_provider=lambda: identity,
    )

    async with create_connected_server_and_client_session(server) as session:
        await session.list_tools()
        selected = await session.call_tool("repo_list", {"requested_repo": "demo"})
        assert selected.isError is False
        pin = next(iter(server._session_repository_selections.values()))
        monkeypatch.setattr(
            "repoforge.interfaces.mcp.server.time.time",
            lambda: pin.expires_at_epoch + 1.0,
        )
        stale = await session.call_tool("repo_tree", {"repo_id": "demo"})

    assert stale.isError is True
    assert stale.structuredContent is not None
    assert stale.structuredContent["error"]["code"] == "STALE_STATE"
    assert "expiry" in stale.structuredContent["error"]["message"]
    assert server._session_repository_selections == {}


@pytest.mark.anyio
async def test_unscoped_tool_is_not_blocked_by_an_expired_repository_selection(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _contract_identity()
    server = create_server(
        service=forge_env.service,
        contract_identity_provider=lambda: identity,
    )

    async with create_connected_server_and_client_session(server) as session:
        await session.list_tools()
        selected = await session.call_tool("repo_list", {"requested_repo": "demo"})
        assert selected.isError is False
        pin = next(iter(server._session_repository_selections.values()))
        monkeypatch.setattr(
            "repoforge.interfaces.mcp.server.time.time",
            lambda: pin.expires_at_epoch + 1.0,
        )
        unscoped = await session.call_tool(
            "operation",
            {"action": "list", "limit": 1},
        )

    assert unscoped.isError is False
    assert server._session_repository_selections
