"""Coverage for issue #170: command-source integrity evidence on verification runs
and commits.

Non-blocking throughout: a dirty-source run still executes and still records
last_verification; this module never adds a refusal path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.adapters.audit.query import summarize_command_source_stats
from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.domain.command_source import (
    derive_command_source_paths,
    dirty_command_source_paths,
    validate_command_source_paths,
)
from repoforge.domain.errors import ConfigError

_MAKE_PROFILE = (
    "\n[repositories.demo.profiles.make_check]\n"
    'description = "Runs make check"\n'
    "verification = true\n"
    'commands = [["make", "check"]]\n'
)
_SCRIPT_PROFILE = (
    "\n[repositories.demo.profiles.run_script]\n"
    'description = "Runs a repo-relative script verbatim"\n'
    "verification = false\n"
    'commands = [["python3", "scripts/check.py"]]\n'
)
_EXPLICIT_PATHS_PROFILE = (
    "\n[repositories.demo.profiles.explicit_source]\n"
    'description = "Declares its own command_source_paths, overriding derivation"\n'
    "verification = true\n"
    'commands = [["make", "check"]]\n'
    'command_source_paths = ["custom.mk"]\n'
)
_MAKEFILE_CONTENT = "check:\n\t@echo ok\n"


def _append(env: ForgeEnvironment, text: str) -> None:
    current = env.config_path.read_text(encoding="utf-8")
    env.config_path.write_text(current + text, encoding="utf-8")


def _reload_service(env: ForgeEnvironment) -> CodingService:
    return CodingService(load_config(env.config_path))


# ---------------------------------------------------------------------------
# Pure domain-level unit tests
# ---------------------------------------------------------------------------


def test_derive_includes_default_makefile_spellings_for_a_make_step() -> None:
    derived = derive_command_source_paths((("make", "check"),))
    assert set(derived) == {"Makefile", "makefile", "GNUmakefile"}


def test_derive_includes_verbatim_repo_relative_script_paths() -> None:
    derived = derive_command_source_paths((("python3", "scripts/check_release_contracts.py"),))
    assert derived == ("scripts/check_release_contracts.py",)


def test_derive_ignores_flags_and_non_script_arguments() -> None:
    derived = derive_command_source_paths((("pytest", "-q", "--maxfail=1"),))
    assert derived == ()


def test_derive_is_empty_for_commands_with_no_make_or_script_paths() -> None:
    derived = derive_command_source_paths((("python3", "-c", "print(1)"),))
    assert derived == ()


@pytest.mark.parametrize(
    "paths",
    [
        ("/absolute.mk",),
        ("../escape.mk",),
        ("",),
        ("x" * 300,),
    ],
)
def test_validate_rejects_unsafe_command_source_paths(paths: tuple[str, ...]) -> None:
    with pytest.raises(ConfigError):
        validate_command_source_paths(paths, "test.command_source_paths")


def test_validate_rejects_too_many_or_duplicate_paths() -> None:
    with pytest.raises(ConfigError, match="20 entries"):
        validate_command_source_paths(tuple(f"f{i}.mk" for i in range(21)), "ctx")
    with pytest.raises(ConfigError, match="duplicates"):
        validate_command_source_paths(("a.mk", "a.mk"), "ctx")


def test_dirty_command_source_paths_matches_declared_patterns_only() -> None:
    changed = frozenset({"Makefile", "src/app.py", "README.md"})
    dirty = dirty_command_source_paths(changed, ("Makefile", "makefile", "GNUmakefile"))
    assert dirty == ("Makefile",)


def test_dirty_command_source_paths_is_empty_with_no_declared_paths() -> None:
    assert dirty_command_source_paths(frozenset({"Makefile"}), ()) == ()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_legacy_profiles_with_no_make_or_scripts_derive_empty_paths(
    forge_env: ForgeEnvironment,
) -> None:
    config = load_config(forge_env.config_path)
    quick = config.repositories["demo"].profiles["quick"]
    full = config.repositories["demo"].profiles["full"]
    assert quick.command_source_paths == ()
    assert full.command_source_paths == ()


def test_make_profile_derives_default_makefile_paths_at_load_time(
    forge_env: ForgeEnvironment,
) -> None:
    _append(forge_env, _MAKE_PROFILE)
    config = load_config(forge_env.config_path)
    profile = config.repositories["demo"].profiles["make_check"]
    assert set(profile.command_source_paths) == {"Makefile", "makefile", "GNUmakefile"}


def test_script_profile_derives_verbatim_script_path(forge_env: ForgeEnvironment) -> None:
    _append(forge_env, _SCRIPT_PROFILE)
    config = load_config(forge_env.config_path)
    profile = config.repositories["demo"].profiles["run_script"]
    assert profile.command_source_paths == ("scripts/check.py",)


def test_explicit_command_source_paths_override_derivation(forge_env: ForgeEnvironment) -> None:
    _append(forge_env, _EXPLICIT_PATHS_PROFILE)
    config = load_config(forge_env.config_path)
    profile = config.repositories["demo"].profiles["explicit_source"]
    assert profile.command_source_paths == ("custom.mk",)


# ---------------------------------------------------------------------------
# Real-workspace integration tests
# ---------------------------------------------------------------------------


def test_profile_with_no_command_source_paths_always_stamps_clean(
    forge_env: ForgeEnvironment,
) -> None:
    created = forge_env.service.workspace_create("demo", "clean stamp")
    workspace_id = created["workspace_id"]
    result = forge_env.service.workspace_run_profile(workspace_id, "quick")
    assert result["command_source_dirty"] is False
    assert result["command_source_dirty_paths"] == []
    assert result["command_source_warning"] is None


def test_edited_makefile_stamps_dirty_executes_normally_and_reverting_cleans_it(
    tmp_path: Path,
) -> None:
    from conftest import create_forge_environment, git

    env = create_forge_environment(tmp_path)
    # The Makefile must already exist in the committed base so "reverting" the
    # worktree hack means restoring the exact base version, not deleting the file.
    (env.source / "Makefile").write_text(_MAKEFILE_CONTENT, encoding="utf-8")
    git("add", "Makefile", cwd=env.source)
    git("commit", "-m", "add Makefile", cwd=env.source)
    git("push", cwd=env.source)
    _append(env, _MAKE_PROFILE)
    service = _reload_service(env)
    created = service.workspace_create("demo", "makefile hack pattern")
    workspace_id = created["workspace_id"]
    workspace_path = Path(created["path"])

    hacked_content = _MAKEFILE_CONTENT + "\nhack:\n\t@echo hacked\n"
    (workspace_path / "Makefile").write_text(hacked_content, encoding="utf-8")
    dirty_result = service.workspace_run_profile(workspace_id, "make_check")
    assert dirty_result["command_source_dirty"] is True
    assert "Makefile" in dirty_result["command_source_dirty_paths"]
    assert dirty_result["command_source_warning"] is not None
    assert "workspace_run_diagnostic" in dirty_result["command_source_warning"]
    # Non-blocking: the run still executed and still recorded a verification receipt.
    status = service.workspace_status(workspace_id)
    assert status["last_verification"] is not None

    (workspace_path / "Makefile").write_text(_MAKEFILE_CONTENT, encoding="utf-8")
    clean_result = service.workspace_run_profile(workspace_id, "make_check")
    assert clean_result["command_source_dirty"] is False
    assert clean_result["command_source_dirty_paths"] == []
    assert clean_result["command_source_warning"] is None


# ---------------------------------------------------------------------------
# Commit callout
# ---------------------------------------------------------------------------


def test_commit_callout_present_when_command_source_path_is_committed(
    tmp_path: Path,
) -> None:
    from conftest import create_forge_environment

    env = create_forge_environment(tmp_path, require_verification=False)
    _append(env, _MAKE_PROFILE)
    service = _reload_service(env)
    created = service.workspace_create("demo", "commit callout present")
    workspace_id = created["workspace_id"]
    workspace_path = Path(created["path"])

    (workspace_path / "Makefile").write_text(_MAKEFILE_CONTENT, encoding="utf-8")
    committed = service.workspace_commit(workspace_id, "add Makefile")
    assert committed["command_source_paths_committed"] == ["Makefile"]


def test_commit_callout_absent_when_no_command_source_path_changes(
    tmp_path: Path,
) -> None:
    from conftest import create_forge_environment

    env = create_forge_environment(tmp_path, require_verification=False)
    _append(env, _MAKE_PROFILE)
    service = _reload_service(env)
    created = service.workspace_create("demo", "commit callout absent")
    workspace_id = created["workspace_id"]
    workspace_path = Path(created["path"])

    (workspace_path / "scratch.txt").write_text("hello\n", encoding="utf-8")
    committed = service.workspace_commit(workspace_id, "unrelated change")
    assert committed["command_source_paths_committed"] == []


# ---------------------------------------------------------------------------
# Audit stats aggregation
# ---------------------------------------------------------------------------


def test_audit_stats_reports_dirty_and_clean_counts_per_profile(tmp_path: Path) -> None:
    from conftest import create_forge_environment

    env = create_forge_environment(tmp_path)
    _append(env, _MAKE_PROFILE)
    service = _reload_service(env)
    created = service.workspace_create("demo", "audit stats dirty and clean")
    workspace_id = created["workspace_id"]
    workspace_path = Path(created["path"])

    (workspace_path / "Makefile").write_text(_MAKEFILE_CONTENT, encoding="utf-8")
    service.workspace_run_profile(workspace_id, "make_check")  # dirty
    service.workspace_run_profile(workspace_id, "make_check")  # still dirty
    (workspace_path / "Makefile").unlink()
    service.workspace_run_profile(workspace_id, "quick")  # clean, different profile

    rows = summarize_command_source_stats(env.root / "state" / "audit.jsonl")
    by_profile = {row["profile"]: row for row in rows}
    assert by_profile["make_check"] == {"profile": "make_check", "dirty": 2, "clean": 0}
    assert by_profile["quick"] == {"profile": "quick", "dirty": 0, "clean": 1}


def test_audit_stats_command_source_is_empty_for_missing_log(tmp_path: Path) -> None:
    assert summarize_command_source_stats(tmp_path / "does-not-exist.jsonl") == []
