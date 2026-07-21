from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


def _load_selector_module() -> Any:
    script = Path(__file__).parents[1] / "scripts/select_affected_tests.py"
    spec = importlib.util.spec_from_file_location("repoforge_select_affected_tests", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


selector = _load_selector_module()


def _group(
    name: str,
    *,
    source_globs: tuple[str, ...],
    test_files: tuple[str, ...],
    parallel: bool = True,
) -> Any:
    return selector.Group(
        name=name,
        description=name,
        parallel=parallel,
        source_globs=source_globs,
        test_files=test_files,
    )


def _manifest(
    *,
    groups: tuple[Any, ...],
    safety_bundle: tuple[str, ...] = (),
    conftest_consumers: tuple[str, ...] = (),
) -> Any:
    return selector.Manifest(
        groups=groups, safety_bundle=safety_bundle, conftest_consumers=conftest_consumers
    )


def test_the_shipped_manifest_is_complete_against_the_real_tests_directory() -> None:
    root = Path(__file__).parents[1]
    manifest = selector.load_manifest(root / "tests/test-groups.toml")

    violations = selector.check_completeness(manifest, root / "tests")

    assert violations == []


def test_glob_matching_supports_recursive_and_single_segment_wildcards() -> None:
    assert selector._matches_any(
        "src/repoforge/adapters/git/foo.py", ("src/repoforge/adapters/git/**",)
    )
    assert not selector._matches_any(
        "src/repoforge/adapters/hygiene/foo.py", ("src/repoforge/adapters/git/**",)
    )
    assert selector._matches_any(
        "src/repoforge/interfaces/cli/runtime_commands.py",
        ("src/repoforge/interfaces/cli/runtime*.py",),
    )
    assert not selector._matches_any(
        "src/repoforge/interfaces/cli/main.py", ("src/repoforge/interfaces/cli/runtime*.py",)
    )
    assert selector._matches_any(".github/workflows/ci.yml", (".github/workflows/**",))


def test_selection_is_deterministic_for_a_representative_change_set() -> None:
    manifest = _manifest(
        groups=(
            _group("alpha", source_globs=("src/alpha/**",), test_files=("tests/test_alpha.py",)),
            _group("beta", source_globs=("src/beta/**",), test_files=("tests/test_beta.py",)),
            _group("gamma", source_globs=("src/gamma/**",), test_files=("tests/test_gamma.py",)),
        ),
        safety_bundle=("tests/test_alpha.py",),
    )

    first = selector.select_affected_tests(manifest, ["src/alpha/thing.py", "src/beta/other.py"])
    second = selector.select_affected_tests(manifest, ["src/beta/other.py", "src/alpha/thing.py"])

    assert first == second
    assert first.selected_groups == ("alpha", "beta")
    assert first.omitted_groups == ("gamma",)
    assert first.selected_files == ("tests/test_alpha.py", "tests/test_beta.py")
    assert first.escalated_to_wide is False


def test_unmapped_source_path_escalates_to_wide_fail_closed() -> None:
    manifest = _manifest(
        groups=(
            _group("alpha", source_globs=("src/alpha/**",), test_files=("tests/test_alpha.py",)),
            _group("beta", source_globs=("src/beta/**",), test_files=("tests/test_beta.py",)),
        ),
        safety_bundle=(),
    )

    selection = selector.select_affected_tests(manifest, ["src/unmapped/thing.py"])

    assert selection.escalated_to_wide is True
    assert "src/unmapped/thing.py" in (selection.escalation_reason or "")
    assert set(selection.selected_files) == {"tests/test_alpha.py", "tests/test_beta.py"}


def test_always_wide_path_escalates_even_when_it_would_otherwise_map_narrowly() -> None:
    manifest = _manifest(
        groups=(
            _group("alpha", source_globs=("src/alpha/**",), test_files=("tests/test_alpha.py",)),
        ),
        safety_bundle=(),
    )

    selection = selector.select_affected_tests(manifest, ["pyproject.toml"])

    assert selection.escalated_to_wide is True
    assert selection.selected_files == ("tests/test_alpha.py",)


@pytest.mark.parametrize(
    "changed_paths",
    [
        ["src/alpha/thing.py"],
        ["src/unmapped/thing.py"],
        ["pyproject.toml"],
        [],
    ],
)
def test_safety_bundle_always_runs_regardless_of_selection(changed_paths: list[str]) -> None:
    manifest = _manifest(
        groups=(
            _group("alpha", source_globs=("src/alpha/**",), test_files=("tests/test_alpha.py",)),
        ),
        safety_bundle=("tests/test_safety_smoke.py",),
    )

    selection = selector.select_affected_tests(manifest, changed_paths)

    assert "tests/test_safety_smoke.py" in selection.selected_files


def test_check_completeness_reports_unmapped_and_stale_and_duplicate_entries(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_owned.py").write_text("", encoding="utf-8")
    (tests_dir / "test_unowned.py").write_text("", encoding="utf-8")

    manifest = _manifest(
        groups=(
            _group(
                "alpha",
                source_globs=(),
                test_files=("tests/test_owned.py", "tests/test_missing.py"),
            ),
            _group("beta", source_globs=(), test_files=("tests/test_owned.py",)),
        ),
        safety_bundle=("tests/test_owned.py",),
    )

    violations = selector.check_completeness(manifest, tests_dir)

    joined = "\n".join(violations)
    assert "test_owned.py" in joined and "more than one group" in joined
    assert "test_missing.py" in joined and "does not exist on disk" in joined
    assert "test_unowned.py" in joined and "is not mapped to any group" in joined


def test_no_changed_paths_runs_only_the_safety_bundle() -> None:
    manifest = _manifest(
        groups=(
            _group("alpha", source_globs=("src/alpha/**",), test_files=("tests/test_alpha.py",)),
        ),
        safety_bundle=("tests/test_safety_smoke.py",),
    )

    selection = selector.select_affected_tests(manifest, [])

    assert selection.selected_files == ("tests/test_safety_smoke.py",)
    assert selection.escalated_to_wide is False
