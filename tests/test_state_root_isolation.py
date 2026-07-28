"""A config's state root decides where its own state lives (#318).

`AGENTS.md` requires that tests never use the developer's real state. They did: 640
directories under a real `~/.local/state/repoforge/config-locks/` were created by pytest
runs, each holding an immutable configuration generation whose own `state_root` pointed at
a temp directory that no longer existed. The CLI resolved the state root by *always*
answering with the default, ignoring `[server].state_root` entirely, so everything a run
did stayed in its temp directory except the per-config generation/lock/diagnostics tree.

The leak was invisible for the same reason it was harmful: nothing in the test's own
workspace showed it.
"""

from __future__ import annotations

from pathlib import Path

from repoforge.bootstrap import default_state_root
from repoforge.config import declared_state_root
from repoforge.interfaces.cli.main import _state_root


def _config(tmp_path: Path, *, state_root: str | None) -> Path:
    path = tmp_path / "config.toml"
    body = "[server]\n"
    if state_root is not None:
        body += f'state_root = "{state_root}"\n'
    body += 'workspace_root = "{}"\n'.format(tmp_path / "workspaces")
    path.write_text(body, encoding="utf-8")
    return path


def test_a_declared_state_root_is_honoured(tmp_path: Path) -> None:
    declared = tmp_path / "state"
    config = _config(tmp_path, state_root=str(declared))

    assert declared_state_root(config) == declared
    assert _state_root(config) == declared
    # And nothing was resolved into the developer's real state root.
    assert _state_root(config) != default_state_root()


def test_a_config_that_declares_nothing_still_uses_the_default(tmp_path: Path) -> None:
    """The live installation declares no state root, so its behaviour must not change."""
    config = _config(tmp_path, state_root=None)

    assert declared_state_root(config) is None
    assert _state_root(config) == default_state_root()


def test_an_unreadable_config_falls_back_instead_of_failing(tmp_path: Path) -> None:
    """Resolving a path must not be the thing that reports a malformed config."""
    broken = tmp_path / "config.toml"
    broken.write_text("[server\nstate_root = ", encoding="utf-8")

    assert declared_state_root(broken) is None
    assert _state_root(broken) == default_state_root()
    assert declared_state_root(tmp_path / "absent.toml") is None


def test_an_installation_created_before_this_keeps_its_existing_state(tmp_path: Path) -> None:
    """Compatibility: state written while the declaration was ignored still wins.

    Otherwise the declared root would be empty, the CLI would report "No accepted
    configuration generation", and it would silently re-import -- presenting as data loss.
    """
    import importlib

    # `from ... import main` on the package yields the FUNCTION, so reach the module.
    cli = importlib.import_module("repoforge.interfaces.cli.main")

    declared = tmp_path / "state"
    fake_default = tmp_path / "real-home-state"
    config = _config(tmp_path, state_root=str(declared))
    # Simulate the legacy layout: this config's tree exists under the DEFAULT root only.
    legacy = cli._config_lock_root(config, fake_default)
    (legacy / "generations-v3" / "1").mkdir(parents=True)

    original = cli.default_state_root
    try:
        cli.default_state_root = lambda: fake_default  # type: ignore[assignment]
        assert cli._state_root(config) == fake_default
        # Once the declared root holds this config's tree, it takes over.
        cli._config_lock_root(config, declared).mkdir(parents=True)
        assert cli._state_root(config) == declared
    finally:
        cli.default_state_root = original  # type: ignore[assignment]


def test_relative_and_home_paths_resolve_like_the_config_loader(tmp_path: Path) -> None:
    """A declared root is a config path: `~` and a relative path mean what they do
    everywhere else, resolved against the config's own directory."""
    config = _config(tmp_path, state_root="./nested/state")

    assert declared_state_root(config) == (tmp_path / "nested" / "state").resolve()
