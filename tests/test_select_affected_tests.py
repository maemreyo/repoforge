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
    coverage_map: dict[str, tuple[str, ...]] | None = None,
) -> Any:
    return selector.Manifest(
        groups=groups,
        safety_bundle=safety_bundle,
        conftest_consumers=conftest_consumers,
        coverage_map=coverage_map or {},
    )


def test_the_shipped_manifest_is_complete_against_the_real_tests_directory() -> None:
    root = Path(__file__).parents[1]
    manifest = selector.load_manifest(root / "tests/test-groups.toml")

    violations = selector.check_completeness(manifest, root / "tests")

    assert violations == []


@pytest.mark.parametrize(
    ("changed_path", "required_tests"),
    [
        (
            "src/repoforge/domain/github_capability_preflight.py",
            {"tests/test_github_capability_preflight_domain.py"},
        ),
        (
            "src/repoforge/ports/github_capability_preflight.py",
            {"tests/test_github_capability_preflight_adapter.py"},
        ),
        (
            "src/repoforge/adapters/github/capability_preflight.py",
            {"tests/test_github_capability_preflight_adapter.py"},
        ),
        (
            "src/repoforge/adapters/github/api_identity.py",
            {
                "tests/test_github_api_identity.py",
                "tests/test_github_capability_preflight_adapter.py",
            },
        ),
        (
            "src/repoforge/domain/repository_identity.py",
            {
                "tests/test_github_api_identity.py",
                "tests/test_operation_identity_leases.py",
                "tests/test_repository_identity_contracts.py",
            },
        ),
        (
            "src/repoforge/adapters/publication.py",
            {
                "tests/test_github_api_identity.py",
                "tests/test_publication_adapter.py",
            },
        ),
        (
            "src/repoforge/application/publication.py",
            {
                "tests/test_github_api_identity.py",
                "tests/test_publication_guards.py",
            },
        ),
        (
            "src/repoforge/application/context.py",
            {
                "tests/test_github_api_identity.py",
                "tests/test_integration.py",
            },
        ),
        (
            "src/repoforge/bootstrap.py",
            {
                "tests/test_github_api_identity.py",
                "tests/test_integration.py",
            },
        ),
    ],
)
def test_shipped_manifest_selects_github_capability_preflight_consumers(
    changed_path: str,
    required_tests: set[str],
) -> None:
    root = Path(__file__).parents[1]
    manifest = selector.load_manifest(root / "tests/test-groups.toml")

    selection = selector.select_affected_tests(manifest, [changed_path])

    assert selection.escalated_to_wide is False
    assert required_tests <= set(selection.selected_files)


def test_shipped_manifest_fails_closed_for_an_unmapped_package_module() -> None:
    root = Path(__file__).parents[1]
    manifest = selector.load_manifest(root / "tests/test-groups.toml")
    changed_path = "src/repoforge/task7_unmapped_sentinel.py"

    selection = selector.select_affected_tests(manifest, [changed_path])

    assert selection.escalated_to_wide is True
    assert changed_path in (selection.escalation_reason or "")


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


def _coverage_manifest() -> Any:
    return _manifest(
        groups=(
            _group(
                "core",
                source_globs=("src/repoforge/**", "docs/**"),
                test_files=("tests/test_a.py", "tests/test_b.py", "tests/test_c.py"),
            ),
        ),
        safety_bundle=("tests/test_smoke.py",),
        conftest_consumers=("tests/test_a.py", "tests/test_b.py"),
        coverage_map={
            "src/repoforge/application/workspace/commit.py": ("tests/test_a.py", "tests/test_b.py"),
            "src/repoforge/adapters/git/cli.py": ("tests/test_a.py", "tests/test_c.py"),
        },
    )


def test_coverage_map_selects_only_covering_tests_plus_safety_bundle() -> None:
    manifest = _coverage_manifest()

    selection = selector.select_affected_tests(
        manifest, ["src/repoforge/application/workspace/commit.py"]
    )

    assert selection.escalated_to_wide is False
    # Exactly the two covering tests + safety bundle -- not the whole group.
    assert selection.selected_files == ("tests/test_a.py", "tests/test_b.py", "tests/test_smoke.py")


def test_coverage_map_unions_covering_tests_across_changed_files() -> None:
    manifest = _coverage_manifest()

    selection = selector.select_affected_tests(
        manifest,
        [
            "src/repoforge/application/workspace/commit.py",
            "src/repoforge/adapters/git/cli.py",
        ],
    )

    assert selection.escalated_to_wide is False
    assert selection.selected_files == (
        "tests/test_a.py",
        "tests/test_b.py",
        "tests/test_c.py",
        "tests/test_smoke.py",
    )


def test_coverage_map_unmapped_package_module_fails_closed() -> None:
    manifest = _coverage_manifest()

    selection = selector.select_affected_tests(
        manifest, ["src/repoforge/application/workspace/brand_new.py"]
    )

    assert selection.escalated_to_wide is True
    assert "fail-closed" in (selection.escalation_reason or "")


def test_coverage_map_changed_test_file_runs_itself() -> None:
    manifest = _coverage_manifest()

    selection = selector.select_affected_tests(manifest, ["tests/test_c.py"])

    assert selection.escalated_to_wide is False
    assert selection.selected_files == ("tests/test_c.py", "tests/test_smoke.py")


def test_coverage_map_non_package_path_falls_back_to_group_globs() -> None:
    manifest = _coverage_manifest()

    selection = selector.select_affected_tests(manifest, ["docs/guide.md"])

    assert selection.escalated_to_wide is False
    # docs/** matches the 'core' group -> its whole test_files list + safety bundle.
    assert selection.selected_files == (
        "tests/test_a.py",
        "tests/test_b.py",
        "tests/test_c.py",
        "tests/test_smoke.py",
    )


def test_coverage_map_always_wide_path_still_escalates() -> None:
    manifest = _coverage_manifest()

    selection = selector.select_affected_tests(manifest, ["pyproject.toml"])

    assert selection.escalated_to_wide is True


def test_coverage_map_conftest_change_adds_consumers() -> None:
    manifest = _coverage_manifest()

    selection = selector.select_affected_tests(
        manifest,
        ["tests/conftest.py", "src/repoforge/adapters/git/cli.py"],
    )

    assert selection.escalated_to_wide is False
    # cli.py -> test_a, test_c ; conftest -> consumers (test_a, test_b) ; + safety
    assert selection.selected_files == (
        "tests/test_a.py",
        "tests/test_b.py",
        "tests/test_c.py",
        "tests/test_smoke.py",
    )


# ------------------------- pull-request-time coverage-map freshness


def _tree(tmp_path: Path, **modules: str) -> Path:
    """Materialise package modules so the check can read what it is judging."""
    for relative, source in modules.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return tmp_path


_REAL_BODY = "def run() -> int:\n    return 1\n"


def test_map_freshness_passes_when_changed_modules_are_mapped(tmp_path: Path) -> None:
    root = _tree(tmp_path, **{"src/repoforge/adapters/git/cli.py": _REAL_BODY})

    assert (
        selector.check_map_freshness(
            _coverage_manifest(), ["src/repoforge/adapters/git/cli.py"], root
        )
        == 0
    )


def test_map_freshness_fails_on_a_package_module_missing_from_the_map(tmp_path: Path) -> None:
    """An unmapped module makes the selector fail closed, so every later change to it
    runs the whole suite. That is the rot this catches while it is still a pull request."""
    root = _tree(tmp_path, **{"src/repoforge/brand_new.py": _REAL_BODY})

    assert (
        selector.check_map_freshness(_coverage_manifest(), ["src/repoforge/brand_new.py"], root)
        == 1
    )


def test_map_freshness_ignores_paths_the_map_never_covers(tmp_path: Path) -> None:
    """The map is source-to-test; docs, tests and data are selected by other rules."""
    root = _tree(tmp_path, **{"docs/guide.md": "x", "tests/test_new.py": "x"})

    assert (
        selector.check_map_freshness(
            _coverage_manifest(),
            ["docs/guide.md", "tests/test_new.py", "pyproject.toml", "src/repoforge/notes.txt"],
            root,
        )
        == 0
    )


def test_map_freshness_does_not_demand_the_impossible(tmp_path: Path) -> None:
    """A Protocol can never be mapped, so rejecting it would send the author to run
    `make test-map`, which cannot fix it. This is the case the first version got wrong."""
    root = _tree(
        tmp_path,
        **{"src/repoforge/ports/store.py": "class S:\n    def read(self) -> str: ...\n"},
    )

    assert (
        selector.check_map_freshness(_coverage_manifest(), ["src/repoforge/ports/store.py"], root)
        == 0
    )


def test_a_protocol_module_is_not_treated_as_a_stale_map_entry() -> None:
    """Only executed function-body lines carry a per-test context, so a Protocol whose
    methods are all `...` can never appear in the map. Demanding it made the gate tell
    authors to run `make test-map`, which cannot fix those files -- 15 of the 16 it
    rejected on the identity branch were exactly this."""
    protocol = (
        "from typing import Protocol\n\n\nclass Store(Protocol):\n"
        "    def read(self) -> str: ...\n"
        "    def write(self, value: str) -> None: ...\n"
    )

    assert selector.is_mappable_module(protocol, "src/repoforge/ports/store.py") is False


def test_a_constants_module_is_not_treated_as_a_stale_map_entry() -> None:
    constants = 'IDENTITY = {"contract_version": 2}\n'

    assert selector.is_mappable_module(constants, "src/repoforge/contracts/generated.py") is False


def test_a_package_init_is_not_treated_as_a_stale_map_entry() -> None:
    """Zero of the mapped files are `__init__.py`: where one carries code it is a lazy
    `__getattr__` production never reaches, because modules import submodules directly."""
    lazy_init = (
        "from importlib import import_module\n"
        "from typing import Any\n\n\n"
        "def __getattr__(name: str) -> Any:\n"
        "    return getattr(import_module('.errors', __name__), name)\n"
    )

    assert selector.is_mappable_module(lazy_init, "src/repoforge/domain/__init__.py") is False


def test_a_module_with_real_function_bodies_is_mappable() -> None:
    module = "def add(a: int, b: int) -> int:\n    return a + b\n"

    assert selector.is_mappable_module(module, "src/repoforge/domain/math.py") is True
