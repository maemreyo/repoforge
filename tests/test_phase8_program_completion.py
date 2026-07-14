from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from repoforge.application.configuration.source import parse_source, render_source
from repoforge.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_release_contract_matches_frozen_golden() -> None:
    from repoforge.interfaces.cli.contract import build_cli_release_contract
    from repoforge.interfaces.mcp.contract import build_release_contract

    expected = json.loads(
        (ROOT / "docs/contracts/release-contract-v1.json").read_text(encoding="utf-8")
    )
    actual = asyncio.run(build_release_contract())
    actual["cli"] = build_cli_release_contract()
    assert actual == expected
    assert "onboard" in expected["cli"]["commands"]
    assert expected["cli"]["commands"]["repo discover"]["read_only"] is True


def test_minimal_and_legacy_config_fixtures_remain_compatible() -> None:
    minimal_path = ROOT / "tests/fixtures/config/minimal-v2.toml"
    minimal = parse_source(minimal_path.read_text(encoding="utf-8"))
    assert minimal.tunnel_id == "fixture-tunnel"
    assert tuple(item.repo_id for item in minimal.repositories) == ("demo",)
    assert parse_source(render_source(minimal)) == minimal

    legacy_path = ROOT / "tests/fixtures/config/legacy-v1.toml"
    legacy = load_config(legacy_path)
    assert tuple(legacy.repositories) == ("demo",)
    assert legacy.repositories["demo"].default_verification_profile == "quick"
    assert legacy.repositories["demo"].profiles["quick"].commands == (("python", "-m", "pytest"),)


def test_release_contract_checker_passes_from_repository_root() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/check_release_contracts.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "release contracts match" in completed.stdout


def test_compatibility_tunnel_script_delegates_to_foreground_start() -> None:
    script = ROOT / "scripts/run-tunnel.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!")
    assert "REPOFORGE_BIN" in text
    assert 'start "$@"' in text


def test_production_ci_covers_supported_python_and_required_gates() -> None:
    workflow = (ROOT / ".github/workflows/production-gate.yml").read_text(encoding="utf-8")
    for version in ("3.10", "3.11", "3.12", "3.13"):
        assert version in workflow
    for command in (
        "ruff format --check src tests",
        "ruff check src tests",
        "mypy --strict src/repoforge",
        "pytest --timeout=60 --cov=repoforge --cov-branch",
        "test_onboarding_real_git.py",
        "scripts/check_release_contracts.py",
        "uv build",
        "scripts/verify-wheel-install.sh",
    ):
        assert command in workflow
    assert "macos-latest" in workflow
    wheel_verifier = (ROOT / "scripts/verify-wheel-install.sh").read_text(encoding="utf-8")
    assert "scripts/verify-wheel-e2e.py" in wheel_verifier
    assert "${REPOFORGE_SMOKE_PYTHON:-python3}" in wheel_verifier
    assert (ROOT / "scripts/verify-wheel-e2e.py").is_file()


def test_production_verifier_reports_head_and_refuses_dirty_tracked_tree() -> None:
    script = (ROOT / "scripts/verify-production.sh").read_text(encoding="utf-8")
    shard_runner = (ROOT / "scripts/run_test_shards.py").read_text(encoding="utf-8")
    assert "git rev-parse HEAD" in script
    assert "check_release_contracts.py" in script
    assert "verify-wheel-install.sh" in script
    assert "git status --porcelain --untracked-files=normal" in script
    assert "PYTHONDONTWRITEBYTECODE=1" in script
    assert "run_test_shards.py" in script
    assert "COVERAGE_FILE" in shard_runner
    assert '"-p"' in shard_runner and '"no:cacheprovider"' in shard_runner
    assert '"--timeout=60"' in shard_runner
    assert '"combine"' in shard_runner and '"--fail-under=80"' in shard_runner
    assert script.count("git status --porcelain --untracked-files=normal") >= 2


def test_contribution_guidance_requires_scoped_conventional_commits() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "Conventional Commits" in text
    assert "type(scope): summary" in text
    assert "one independently deployable concern" in text


def test_plan_records_phase8_and_no_stale_immediate_action() -> None:
    text = (ROOT / "docs/plans/repoforge-production-architecture-tunnel-plan.md").read_text(
        encoding="utf-8"
    )
    assert "Phase 8 — Program completion and release gates" in text
