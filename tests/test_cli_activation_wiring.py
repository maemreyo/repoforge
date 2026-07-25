"""Round-7 findings 4 and 7: the CLI's composition decisions are themselves regressions.

Both bugs guarded here were invisible to every existing test because they live in the wiring
rather than in an adapter: which release root is allowed to rewrite the user's PATH launcher,
and which launchd label the registrar and kickstarter are actually handed.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from repoforge.adapters.activation.release_store import RuntimeReleaseStore
from repoforge.domain.activation import SUPERVISOR_AGENT_LABEL
from repoforge.interfaces.cli.main import _launch_agent_arguments, _manages_path_launcher


def _args(**overrides: object) -> argparse.Namespace:
    namespace = argparse.Namespace(release_root=None, no_path_shim=False)
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


def test_the_environment_release_root_override_disables_the_path_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REPOFORGE_RELEASE_ROOT redirects the root without any CLI flag being passed.

    A flag-only check reported "not managed" while still rewriting the operator's real
    ``~/.local/bin/rf`` to exec a temporary release root -- found by a live run, and this is
    the direct unit gate against it returning.
    """
    monkeypatch.setenv("REPOFORGE_RELEASE_ROOT", str(tmp_path / "sandbox-root"))

    assert _manages_path_launcher(_args()) is False


def test_the_default_release_root_still_manages_the_path_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REPOFORGE_RELEASE_ROOT", raising=False)

    assert _manages_path_launcher(_args()) is True
    # An explicit --release-root pointing AT the default root is still the default root.
    default = str(RuntimeReleaseStore.default_release_root())
    assert _manages_path_launcher(_args(release_root=default)) is True
    # ... and --no-path-shim opts out even there.
    assert _manages_path_launcher(_args(no_path_shim=True)) is False


def test_an_explicit_release_root_flag_disables_the_path_launcher(tmp_path: Path) -> None:
    assert _manages_path_launcher(_args(release_root=str(tmp_path / "other-root"))) is False


def test_the_launch_agent_arguments_carry_the_namespaced_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registrar and kickstarter must receive the PER-ROOT label, not the base one.

    A correct `supervisor_agent_label()` is worthless if the composition root passes a
    different label to launchctl: a sandbox would then `kickstart -k` the operator's real
    supervisor, which is exactly the live failure that motivated namespacing.
    """
    sandbox_root = tmp_path / "sandbox-root"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    arguments = _launch_agent_arguments(
        _args(release_root=str(sandbox_root), config=str(tmp_path / "config.toml"))
    )

    expected = RuntimeReleaseStore(sandbox_root).supervisor_agent_label()
    assert arguments["label"] == expected
    assert expected != SUPERVISOR_AGENT_LABEL
    assert expected.startswith(f"{SUPERVISOR_AGENT_LABEL}.")
    # The launcher must be the sandbox's supervisor shim, never a production path.
    assert arguments["launcher_path"] == sandbox_root / "bin" / "rf-supervisor"


def test_the_default_root_keeps_the_base_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REPOFORGE_RELEASE_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    arguments = _launch_agent_arguments(_args(config=str(tmp_path / "config.toml")))

    assert arguments["label"] == SUPERVISOR_AGENT_LABEL
