from __future__ import annotations

from pathlib import Path

from repoforge.audit import AuditLogger
from repoforge.config import load_config
from repoforge.runner import CommandRunner
from repoforge.service import CodingService
from repoforge.state import StateStore


def test_coding_service_uses_explicit_dependencies(tmp_path: Path) -> None:
    # Given: a valid config and concrete dependencies supplied by the composition root.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"

[repositories.demo]
path = "{repo}"
''',
        encoding="utf-8",
    )
    config = load_config(config_path)
    runner = CommandRunner(config.server)
    state = StateStore(config.server.state_root)
    audit = AuditLogger(config.server.state_root)

    # When: application service composition receives the adapters.
    service = CodingService(config, runner=runner, state=state, audit=audit)

    # Then: it uses exactly the supplied adapters without changing its public API.
    assert service.runner is runner
    assert service.state is state
    assert service.audit is audit
