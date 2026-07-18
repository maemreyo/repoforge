"""Coverage for deterministic repository selection on the v2 `repo_list` composite (#150,
ported onto the static 28-tool Forge v2 surface as part of epic #180 integration)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ElicitRequestParams, ElicitResult, Implementation

from repoforge.application.service import CodingService
from repoforge.config import AppConfig, ServerConfig, load_config
from repoforge.interfaces.mcp.server import create_server


def _two_repo_config(tmp_path: Path, forge_env: ForgeEnvironment) -> Path:
    config_path = tmp_path / "two-repo-config.toml"
    config_path.write_text(
        f'''[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"
path_prefixes = ["{forge_env.fake_bin}", "/usr/local/bin", "/usr/bin", "/bin"]

[repositories.demo]
path = "{forge_env.source}"
display_name = "Demo Repository"
remote = "origin"

[repositories.widgets]
path = "{forge_env.source}"
display_name = "Widgets Service"
remote = "upstream"
''',
        encoding="utf-8",
    )
    return config_path


@pytest.mark.anyio
async def test_repo_list_single_enrolled_selects_without_asking(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {})
        assert result.isError is False
        selection = result.structuredContent["selection"]
        assert selection["outcome"] == "single_enrolled"
        assert selection["repo_id"] == "demo"
        assert result.structuredContent["selection_prompt"] is None


@pytest.mark.anyio
async def test_repo_list_exact_repo_id_hint_resolves_directly(
    tmp_path: Path, forge_env: ForgeEnvironment
) -> None:
    config_path = _two_repo_config(tmp_path, forge_env)
    server = create_server(service=CodingService(load_config(config_path)))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {"requested_repo": "widgets"})
        assert result.isError is False
        selection = result.structuredContent["selection"]
        assert selection["outcome"] == "exact_match"
        assert selection["repo_id"] == "widgets"
        assert result.structuredContent["selection_prompt"] is None


@pytest.mark.anyio
async def test_repo_list_ambiguous_multi_repo_requires_input_with_deterministic_fallback(
    tmp_path: Path, forge_env: ForgeEnvironment
) -> None:
    config_path = _two_repo_config(tmp_path, forge_env)
    server = create_server(service=CodingService(load_config(config_path)))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {})
        assert result.isError is False
        selection = result.structuredContent["selection"]
        assert selection["outcome"] == "input_required"
        assert selection["repo_id"] is None
        assert sorted(c["repo_id"] for c in selection["candidates"]) == ["demo", "widgets"]

        prompt = result.structuredContent["selection_prompt"]
        assert prompt is not None
        assert prompt["status"] == "INPUT_REQUIRED"
        assert prompt["fallback_for"] == "elicitation"
        assert sorted(prompt["allowed_options"]) == ["demo", "widgets"]


@pytest.mark.anyio
async def test_repo_list_selection_prompt_is_identical_with_and_without_elicitation(
    tmp_path: Path, forge_env: ForgeEnvironment
) -> None:
    config_path = _two_repo_config(tmp_path, forge_env)
    server = create_server(service=CodingService(load_config(config_path)))

    async with create_connected_server_and_client_session(server) as session:
        without_elicitation = await session.call_tool("repo_list", {})

    async def elicitation_callback(
        _context: object,
        _params: ElicitRequestParams,
    ) -> ElicitResult:
        return ElicitResult(action="cancel")

    async with create_connected_server_and_client_session(
        server,
        elicitation_callback=elicitation_callback,
        client_info=Implementation(name="capable-client", version="9.0"),
    ) as session:
        with_elicitation = await session.call_tool("repo_list", {})

    assert without_elicitation.isError is False
    assert with_elicitation.isError is False
    assert (
        without_elicitation.structuredContent["selection_prompt"]
        == with_elicitation.structuredContent["selection_prompt"]
    )


@pytest.mark.anyio
async def test_repo_list_zero_enrolled_returns_no_match(tmp_path: Path) -> None:
    config = AppConfig(
        source_path=tmp_path / "config.toml",
        server=ServerConfig(
            workspace_root=tmp_path / "workspaces",
            state_root=tmp_path / "state",
        ),
        repositories={},
    )
    server = create_server(service=CodingService(config))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {})
        assert result.isError is False
        selection = result.structuredContent["selection"]
        assert selection["outcome"] == "no_match"
        assert selection["repo_id"] is None
        assert selection["candidates"] == []
        assert result.structuredContent["selection_prompt"] is None
