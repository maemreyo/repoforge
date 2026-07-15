from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.application.configuration.document import parse_resolved, render_resolved
from repoforge.application.service import CodingService
from repoforge.config import DEFAULT_RESOURCE_BUDGET, load_config
from repoforge.domain.config_generation import CapabilityDeltaKind, classify_capability_delta
from repoforge.domain.errors import ConfigError
from repoforge.domain.resource_budget import RESOURCE_BUDGET_FIELDS


def _config_text(repo: Path, server_budget: str = "", repository_budget: str = "") -> str:
    return f'''[server]
workspace_root = "{repo.parent / "workspaces"}"
state_root = "{repo.parent / "state"}"
{server_budget}

[repositories.demo]
path = "{repo}"
{repository_budget}
'''


def test_resource_budgets_use_conservative_typed_defaults(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(repo), encoding="utf-8")

    loaded = load_config(config_path)

    assert loaded.server.resource_budget.max_concurrent_operations == 4
    assert loaded.server.resource_budget.max_subprocesses == 8
    assert loaded.repositories["demo"].resource_budget == loaded.server.resource_budget


def test_repository_resource_budget_inherits_unspecified_limits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _config_text(
            repo,
            server_budget="""[server.resource_budget]
max_output_bytes = 65536
max_provider_requests = 200
""",
            repository_budget="""[repositories.demo.resource_budget]
max_output_bytes = 4096
""",
        ),
        encoding="utf-8",
    )

    loaded = load_config(config_path)

    budget = loaded.repositories["demo"].resource_budget
    assert budget.max_output_bytes == 4096
    assert budget.max_provider_requests == 200


def test_repository_resource_budget_cannot_expand_server_policy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _config_text(
            repo,
            server_budget="""[server.resource_budget]
max_output_bytes = 4096
""",
            repository_budget="""[repositories.demo.resource_budget]
max_output_bytes = 4097
""",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="cannot exceed"):
        load_config(config_path)


def test_invalid_resource_budget_value_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _config_text(
            repo,
            server_budget="""[server.resource_budget]
max_memory_bytes = 0
""",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="max_memory_bytes"):
        load_config(config_path)


def test_repository_overview_renders_resolved_resource_budget(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(repo), encoding="utf-8")

    repository = CodingService(load_config(config_path)).repo_list()["repositories"][0]

    assert repository["resource_budget"]["max_concurrent_operations"] == 4
    assert repository["resource_budget"]["max_provider_requests"] == 100


def test_resolved_config_renders_resource_budget_tables_deterministically() -> None:
    document = parse_resolved(None)
    document["server"]["resource_budget"] = {"max_output_bytes": 4096}
    document["repositories"]["demo"] = {
        "path": "/tmp/demo",
        "resource_budget": {"max_output_bytes": 1024},
    }

    rendered = render_resolved(
        document,
        generation=1,
        source_path="/tmp/config.toml",
        source_sha256="source",
        created_at="2026-07-15T00:00:00+00:00",
        reason="test",
        proposal_id=None,
        repository_fingerprints=(("demo", "fingerprint"),),
    )

    assert "[server.resource_budget]" in rendered
    assert "[repositories.demo.resource_budget]" in rendered
    assert rendered.index("[server.resource_budget]") < rendered.index("[repositories.demo]")


def test_resource_budget_delta_classifies_tightening_as_restriction() -> None:
    current = """[server]
[server.resource_budget]
max_output_bytes = 65536
[repositories.demo]
path = "/tmp/demo"
"""
    candidate = current.replace("max_output_bytes = 65536", "max_output_bytes = 4096")

    delta = classify_capability_delta(current, candidate)

    assert delta.kind is CapabilityDeltaKind.RESTRICTION
    assert {change.path for change in delta.changes} == {
        "server.resource_budget.max_output_bytes",
        "repositories.demo.resource_budget.max_output_bytes",
    }


def test_resource_budget_delta_classifies_relaxing_as_expansion() -> None:
    current = """[server]
[server.resource_budget]
max_output_bytes = 4096
[repositories.demo]
path = "/tmp/demo"
[repositories.demo.resource_budget]
max_output_bytes = 1024
"""
    candidate = current.replace("max_output_bytes = 1024", "max_output_bytes = 2048")

    delta = classify_capability_delta(current, candidate)

    assert delta.kind is CapabilityDeltaKind.EXPANSION
    assert delta.changes[0].path == "repositories.demo.resource_budget.max_output_bytes"


def test_omitted_budget_is_equivalent_to_explicit_conservative_defaults() -> None:
    current = """[server]
[repositories.demo]
path = "/tmp/demo"
"""
    budget_lines = "\n".join(
        f"{field} = {getattr(DEFAULT_RESOURCE_BUDGET, field)}" for field in RESOURCE_BUDGET_FIELDS
    )
    candidate = current.replace(
        "[repositories.demo]", f"[server.resource_budget]\n{budget_lines}\n[repositories.demo]"
    )

    assert classify_capability_delta(current, candidate).kind is CapabilityDeltaKind.EQUIVALENT
