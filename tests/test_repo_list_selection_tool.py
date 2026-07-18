"""MCP contract coverage for repo_list's additive repository-selection guidance (#150)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ElicitRequestParams, ElicitResult, Implementation

from repoforge.application.service import CodingService
from repoforge.config import AppConfig, ServerConfig
from repoforge.interfaces.mcp.server import create_server


def _two_repo_config(tmp_path: Path, forge_env: ForgeEnvironment) -> Path:
    """A second repository sharing the same fixture checkout is enough for pure repo_list
    selection behavior: it never touches the filesystem or requires a distinct Git tree."""

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
    server = create_server(forge_env.config_path)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {})
        assert result.isError is False
        selection = result.structuredContent["selection"]
        assert selection["outcome"] == "single_enrolled"
        assert selection["repo_id"] == "demo"
        assert "selection_prompt" not in result.structuredContent


@pytest.mark.anyio
async def test_repo_list_exact_repo_id_hint_resolves_directly(
    tmp_path: Path, forge_env: ForgeEnvironment
) -> None:
    config_path = _two_repo_config(tmp_path, forge_env)
    server = create_server(config_path)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {"requested_repo": "widgets"})
        assert result.isError is False
        selection = result.structuredContent["selection"]
        assert selection["outcome"] == "exact_match"
        assert selection["repo_id"] == "widgets"
        assert "selection_prompt" not in result.structuredContent


@pytest.mark.anyio
async def test_repo_list_ambiguous_multi_repo_requires_input_with_deterministic_fallback(
    tmp_path: Path, forge_env: ForgeEnvironment
) -> None:
    config_path = _two_repo_config(tmp_path, forge_env)
    server = create_server(config_path)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("repo_list", {})
        assert result.isError is False
        selection = result.structuredContent["selection"]
        assert selection["outcome"] == "input_required"
        assert selection["repo_id"] is None
        assert sorted(c["repo_id"] for c in selection["candidates"]) == ["demo", "widgets"]

        # Deterministic fallback text must be present regardless of Elicitation support.
        prompt = result.structuredContent["selection_prompt"]
        assert prompt["status"] == "INPUT_REQUIRED"
        assert prompt["fallback_for"] == "elicitation"
        assert sorted(prompt["allowed_options"]) == ["demo", "widgets"]


@pytest.mark.anyio
async def test_repo_list_selection_prompt_is_identical_with_and_without_elicitation(
    tmp_path: Path, forge_env: ForgeEnvironment
) -> None:
    config_path = _two_repo_config(tmp_path, forge_env)
    server = create_server(config_path)

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
    # The presentation channel may differ for a richer client in the future, but the
    # deterministic fallback text itself must never depend on negotiated capability.
    assert (
        without_elicitation.structuredContent["selection_prompt"]
        == with_elicitation.structuredContent["selection_prompt"]
    )


@pytest.mark.anyio
async def test_repo_list_zero_enrolled_returns_no_match(tmp_path: Path) -> None:
    # `load_config` itself requires at least one configured repository (a separate, unrelated
    # invariant), so an empty registry is built in-memory here to exercise repo_list's NO_MATCH
    # branch directly against the same CodingService/MCP code path as every other case above.
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
        assert "selection_prompt" not in result.structuredContent
