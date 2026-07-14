from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from repoforge.interfaces.cli.onboarding_review import (
    DefaultsMode,
    configuration_diff,
    discovery_rows,
    proposal_summary,
    resolve_defaults_mode,
)


def test_defaults_mode_is_ask_for_interactive_and_none_for_noninteractive() -> None:
    assert resolve_defaults_mode(None, non_interactive=False) is DefaultsMode.ASK
    assert resolve_defaults_mode("safe", non_interactive=False) is DefaultsMode.SAFE
    assert resolve_defaults_mode(None, non_interactive=True) is DefaultsMode.NONE
    with pytest.raises(ValueError, match="interactive-only"):
        resolve_defaults_mode("ask", non_interactive=True)


def test_discovery_rows_separates_eligible_and_excluded() -> None:
    result = SimpleNamespace(
        eligible=(
            SimpleNamespace(
                repo_id="demo",
                identity=SimpleNamespace(path="/repos/demo"),
                parent_repo_id=None,
            ),
        ),
        exclusions=(
            SimpleNamespace(
                path="/repos/demo/.worktrees/one",
                reason=SimpleNamespace(value="linked_worktree"),
                detail="secondary checkout",
            ),
        ),
    )
    eligible, excluded = discovery_rows(result)
    assert eligible == (("demo", "/repos/demo", "root"),)
    assert excluded == (("/repos/demo/.worktrees/one", "linked_worktree", "secondary checkout"),)


def test_proposal_summary_is_compact_and_fail_closed() -> None:
    payload = {
        "path": "/repos/demo",
        "confidence": "high",
        "policy": {
            "mode": "standard",
            "remote": "origin",
            "default_base": "main",
            "publish_enabled": False,
            "profiles": [{"name": "quick"}, {"name": "full"}],
            "max_changed_files": 20,
            "max_diff_lines": 1000,
            "max_total_changed_bytes": 100000,
        },
        "findings": [{"code": "RISKY_COMMANDS_EXCLUDED"}],
    }
    state = SimpleNamespace(
        candidate=SimpleNamespace(repo_id="demo", identity=SimpleNamespace(path="/fallback")),
        template="standard",
        proposal_json=json.dumps(payload),
    )
    summary = proposal_summary(state)
    assert summary.repo_id == "demo"
    assert summary.profiles == "quick, full"
    assert summary.publishing == "disabled"
    assert summary.findings == "RISKY_COMMANDS_EXCLUDED"


def test_configuration_diff_is_stable_and_labels_source_config() -> None:
    rendered = configuration_diff("version = 2\n", "version = 2\nnew = true\n")
    assert rendered.startswith("--- current-config.toml\n+++ proposed-config.toml\n")
    assert "+new = true" in rendered
