"""Round-7 findings 4 and 7: the CLI's composition decisions are themselves regressions.

Both bugs guarded here were invisible to every existing test because they live in the wiring
rather than in an adapter: which release root is allowed to rewrite the user's PATH launcher,
and which launchd label the registrar and kickstarter are actually handed.
"""

from __future__ import annotations

import argparse
import importlib
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


def test_runtime_ls_reports_an_empty_release_root_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`rf runtime ls` is what someone runs when lost, so it must survive a bare install.

    No config, no runtime record, no launchd job: it still has to answer with what IS
    known instead of raising.
    """
    import json

    from repoforge.interfaces.cli.main import main

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REPOFORGE_CONFIG", str(tmp_path / "absent-config.toml"))

    assert main(["runtime", "ls", "--release-root", str(tmp_path / "release-root")]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["releases"] == []
    assert payload["counts"]["releases"] == 0
    # "stopped" (record absent) or "unknown" (no observation) -- never a running claim.
    assert payload["production"]["phase"] in {"stopped", "unknown"}
    assert payload["production"]["converged"] is False
    assert payload["production"]["os_resident"] is False
    assert "rf upgrade --from-worktree" in payload["safe_next_action"]
    # Execution-worker evidence rides along even on an empty install (#368).
    assert payload["execution_workers"]["stale_execution_worker_count"] == 0
    assert payload["execution_workers"]["reclamation_safe"] is True


def test_version_switch_fails_closed_and_leaves_current_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A switch that cannot proceed must exit non-zero and never move `current`.

    Resolution itself is covered at the service level; what this pins down is that the CLI
    wires the subcommand and fails closed rather than exiting 0 having done nothing.
    """
    from repoforge.interfaces.cli.main import main

    release_root = tmp_path / "release-root"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REPOFORGE_CONFIG", str(tmp_path / "absent-config.toml"))

    exit_code = main(["version", "switch", "no-such-branch", "--release-root", str(release_root)])

    assert exit_code != 0
    assert not (release_root / "current").exists()


def test_upgrade_reconcile_is_wired_to_the_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#304: an activation that reached its target needs a command that terminalizes it.

    The forward path only exists if the CLI actually offers it, so this pins the
    subcommand and its dispatch rather than the service behaviour (covered in
    tests/test_upgrade_service.py).
    """
    import json

    from repoforge.application.activation.upgrade import UpgradeResult

    # `from ... import main` on the package yields the FUNCTION, so reach the module.
    cli_module = importlib.import_module("repoforge.interfaces.cli.main")

    calls: list[str] = []
    repairs: list[str | None] = []

    class _Service:
        def reconcile(self, *, repair: str | None = None) -> UpgradeResult:
            calls.append("reconcile")
            repairs.append(repair)
            return UpgradeResult(
                status="reconciled",
                candidate_sha="aaa1111",
                build_fingerprint="a" * 64,
                tool_surface_hash="b" * 64,
                active_sha="aaa1111",
                activation_receipt="act-20260728-006",
                converged=True,
                detail="Reconciled an unterminalized activation",
            )

    monkeypatch.setattr(cli_module, "_build_upgrade_service", lambda args: _Service())

    assert cli_module.main(["upgrade", "reconcile"]) == 0
    assert calls == ["reconcile"]
    # Repair is opt-in and reaches the service exactly as passed (#367).
    assert repairs == [None]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "reconciled"
    assert payload["activation_receipt"] == "act-20260728-006"

    capsys.readouterr()
    assert cli_module.main(["upgrade", "reconcile", "--repair", "rollback"]) == 0
    assert repairs == [None, "rollback"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "reconciled"


def test_an_upgrade_that_ended_in_a_rollback_does_not_exit_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--watch` can end in an auto-rollback: exiting 0 told automation it succeeded."""
    from repoforge.application.activation.upgrade import UpgradeResult

    # `from ... import main` on the package yields the FUNCTION, so reach the module.
    cli_module = importlib.import_module("repoforge.interfaces.cli.main")

    class _Service:
        def upgrade(self, worktree, **kwargs) -> UpgradeResult:
            return UpgradeResult(
                status="rolled_back",
                candidate_sha="bbb2222",
                build_fingerprint="a" * 64,
                tool_surface_hash="b" * 64,
                active_sha="aaa1111",
                detail="Auto-rollback: candidate unhealthy within the window",
            )

    monkeypatch.setattr(cli_module, "_build_upgrade_service", lambda args: _Service())

    assert cli_module.main(["upgrade", "--activate", "--watch"]) == 1
    capsys.readouterr()
