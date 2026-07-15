from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from repoforge.application.configuration.document import parse_resolved, render_resolved
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.errors import ConfigError
from repoforge.domain.provider_manifest import ProviderExecutableIdentity, ProviderKind
from repoforge.testing.fakes import ScriptedCommandExecutor


def _resolved_config(tmp_path: Path, provider_block: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = tmp_path / "resolved.toml"
    config.write_text(
        f'''[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"

{provider_block}

[repositories.demo]
path = "{repo}"
''',
        encoding="utf-8",
    )
    return config


def test_load_config_parses_reviewed_provider_manifest(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"provider").hexdigest()
    config = _resolved_config(
        tmp_path,
        f'''[[providers]]
provider_id = "python-analyzer"
kind = "analyzer"
version = "1.2.0"
executable = "python3"
executable_digest = "{digest}"
supported_languages = ["python"]
supported_capabilities = ["lint"]
health_probe_arguments = ["--version"]
coverage_model = "statement"
confidence_model = "static"
network_policy = "none"

[providers.filesystem]
capability = "read"
allowed_paths = []

[providers.output_bounds]
max_stdout_chars = 1000
max_stderr_chars = 500
max_artifact_bytes = 10000
''',
    )

    loaded = load_config(config)
    manifest = loaded.providers[0]

    assert manifest.kind is ProviderKind.ANALYZER
    assert manifest.runtime == ProviderExecutableIdentity("python3", digest)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("kind", '"unknown"', r"providers\[0\].kind"),
        ("max_stdout_chars", "0", "max_stdout_chars"),
        ("supported_languages", '"python"', "array of strings"),
    ],
)
def test_load_config_rejects_invalid_provider_values(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    digest = "a" * 64
    base = {
        "kind": '"analyzer"',
        "max_stdout_chars": "1000",
        "supported_languages": '["python"]',
    }
    base[field] = value
    config = _resolved_config(
        tmp_path,
        f'''[[providers]]
provider_id = "python-analyzer"
kind = {base["kind"]}
version = "1.2.0"
executable = "python3"
executable_digest = "{digest}"
supported_languages = {base["supported_languages"]}

[providers.output_bounds]
max_stdout_chars = {base["max_stdout_chars"]}
max_stderr_chars = 500
max_artifact_bytes = 10000
''',
    )

    with pytest.raises(ConfigError, match=message):
        load_config(config)


def test_resolved_document_preserves_provider_entries() -> None:
    document = {
        "providers": [
            {
                "provider_id": "python-analyzer",
                "kind": "analyzer",
                "version": "1.0.0",
                "executable": "python3",
                "executable_digest": "a" * 64,
            }
        ],
        "repositories": {"demo": {"path": "/repos/demo"}},
    }

    rendered = render_resolved(
        document,
        generation=2,
        source_path="config.toml",
        source_sha256="b" * 64,
        created_at="2026-07-15T00:00:00+00:00",
        reason="provider config refresh",
        proposal_id=None,
        repository_fingerprints=(("demo", "c" * 64),),
    )

    assert parse_resolved(rendered)["providers"] == document["providers"]


def test_repository_refresh_document_preserves_reviewed_provider_entries() -> None:
    reviewed = """[[providers]]
provider_id = "python-analyzer"
kind = "analyzer"
version = "1.0.0"
executable = "python3"
executable_digest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

[repositories.demo]
path = "/repos/demo"
"""

    document = parse_resolved(reviewed)
    document["repositories"]["demo"]["display_name"] = "refreshed"
    rendered = render_resolved(
        document,
        generation=2,
        source_path="config.toml",
        source_sha256="b" * 64,
        created_at="2026-07-15T00:00:00+00:00",
        reason="repository refresh",
        proposal_id="refresh-proposal",
        repository_fingerprints=(("demo", "c" * 64),),
    )

    providers = parse_resolved(rendered)["providers"]
    assert isinstance(providers, list)
    provider = providers[0]
    assert isinstance(provider, dict)
    assert provider["provider_id"] == "python-analyzer"
    assert provider["executable_digest"] == "a" * 64


def test_build_application_exposes_registry_from_loaded_config(tmp_path: Path) -> None:
    digest = "a" * 64
    config_path = _resolved_config(
        tmp_path,
        f'''[[providers]]
provider_id = "python-analyzer"
kind = "analyzer"
version = "1.0.0"
executable = "missing-provider"
executable_digest = "{digest}"
''',
    )
    config = load_config(config_path)

    application = build_application(
        config,
        overrides=AdapterOverrides(command=ScriptedCommandExecutor()),
    )

    assert application.context.provider_registry is not None
    assert application.context.provider_registry.get_provider("python-analyzer") is not None
