from __future__ import annotations

import json
from dataclasses import replace

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, Implementation, TextContent

from repoforge.application.runtime.hot_reload import (
    AtomicServiceRouter,
    GenerationServiceContainer,
)
from repoforge.application.service import CodingService
from repoforge.interfaces.mcp.server import create_server


def _text(result: CallToolResult) -> str:
    return "\n".join(item.text for item in result.content if isinstance(item, TextContent))


def _meta(result: CallToolResult) -> dict[str, object]:
    dumped = result.model_dump(by_alias=True)
    raw = dumped.get("_meta")
    assert isinstance(raw, dict)
    return raw


@pytest.mark.anyio
async def test_default_payload_keeps_structured_content_authoritative(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {})

    assert result.isError is False
    assert result.structuredContent is not None
    text = _text(result)
    assert 1 <= len(text.splitlines()) <= 2
    assert len(text.encode("utf-8")) <= 500
    assert '"repositories"' not in text
    assert text != json.dumps(result.structuredContent, sort_keys=True, ensure_ascii=False)

    trace = _meta(result)["repoforge_trace"]
    assert trace["tool_name"] == "repo_list"
    assert trace["engine"]["status"] == "observed"
    assert trace["engine"]["duration_ms"] >= 0
    assert trace["connector"]["status"] == "unobserved"
    assert trace["client_round_trip"]["status"] == "unobserved"
    assert trace["payload"]["legacy_text_duplication"] is False
    assert trace["payload"]["emitted_bytes"] >= trace["payload"]["structured_bytes"]


@pytest.mark.anyio
async def test_deployment_compat_config_restores_full_json_text(
    forge_env: ForgeEnvironment,
) -> None:
    compat = replace(
        forge_env.service.config,
        server=replace(
            forge_env.service.config.server,
            legacy_text_result_duplication=True,
        ),
    )
    server = create_server(service=CodingService(compat))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {})

    assert result.isError is False
    assert result.structuredContent is not None
    assert json.loads(_text(result)) == result.structuredContent
    trace = _meta(result)["repoforge_trace"]
    assert trace["payload"]["legacy_text_duplication"] is True
    assert trace["payload"]["text_bytes"] >= trace["payload"]["structured_bytes"]


@pytest.mark.anyio
@pytest.mark.parametrize("client_name", ["ChatGPT", "Claude Desktop"])
async def test_primary_connector_fixtures_consume_structured_content(
    forge_env: ForgeEnvironment,
    client_name: str,
) -> None:
    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(
        server,
        client_info=Implementation(name=client_name, version="fixture-1"),
    ) as session:
        result = await session.call_tool("repo_list", {})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["repositories"][0]["repo_id"] == "demo"
    trace = _meta(result)["repoforge_trace"]
    assert trace["client_name"] == client_name
    assert trace["payload"]["legacy_text_duplication"] is False


@pytest.mark.anyio
async def test_hot_reload_generation_stays_pinned_through_render_and_metrics(
    forge_env: ForgeEnvironment,
) -> None:
    disposed = {"old": False}
    old_service = forge_env.service
    candidate_service = CodingService(old_service.config)
    router = AtomicServiceRouter(
        GenerationServiceContainer(
            generation=1,
            service=old_service,
            gate=old_service.gate,
            repository_ids=frozenset({"demo"}),
            dispose=lambda: disposed.__setitem__("old", True),
        )
    )
    candidate = GenerationServiceContainer(
        generation=2,
        service=candidate_service,
        gate=candidate_service.gate,
        repository_ids=frozenset({"demo"}),
    )

    class SwapOnLatencyRecord:
        disposed_during_record: bool | None = None

        def record_latency(self, trace: object) -> None:
            router.swap(candidate)
            self.disposed_during_record = disposed["old"]

    metrics = SwapOnLatencyRecord()
    old_service.metrics = metrics
    server = create_server(router=router)

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {})

    assert result.isError is False
    assert metrics.disposed_during_record is False
    assert disposed["old"] is True
