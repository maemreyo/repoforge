from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

import tomli

from repoforge.application.configuration.source import parse_source
from repoforge.interfaces.cli.main import build_parser

ROOT = Path(__file__).parents[1]
_COMMAND = re.compile(r"^(?:uv run )?(?:rf|repoforge)(?:\s|$)")
_SHELL_META = ("\\", "<", ">", "$", "{", "}", "|", "&&", ";")
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.-]+)\s*:", re.MULTILINE)


def _surface(
    parser: argparse.ArgumentParser,
) -> tuple[dict[str, argparse.Action], argparse._SubParsersAction[argparse.ArgumentParser] | None]:
    options: dict[str, argparse.Action] = {}
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser] | None = None
    for action in parser._actions:
        for option in action.option_strings:
            options[option] = action
        if isinstance(action, argparse._SubParsersAction):
            subparsers = action
    return options, subparsers


def _assert_command_surface(parser: argparse.ArgumentParser, argv: list[str]) -> None:
    """Validate documented command and flag names without requiring positional examples."""
    current = parser
    saw_command = False
    index = 0
    while index < len(argv):
        token = argv[index]
        options, subparsers = _surface(current)
        if token.startswith("-"):
            option = token.split("=", 1)[0]
            action = options.get(option)
            assert action is not None, f"unknown flag {option}"
            if "=" not in token:
                nargs = action.nargs
                if nargs in (None, 1):
                    assert index + 1 < len(argv), f"missing value for {option}"
                    index += 1
                elif nargs == "?" and index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                    index += 1
                elif isinstance(nargs, int) and nargs > 1:
                    assert index + nargs < len(argv), f"missing values for {option}"
                    index += nargs
            index += 1
            continue
        if subparsers is not None:
            assert token in subparsers.choices, f"unknown command {token}"
            current = subparsers.choices[token]
            saw_command = True
            index += 1
            continue
        index += 1
    assert saw_command


def test_scripts_and_makefile_do_not_contain_stale_or_personal_invocations() -> None:
    paths = [ROOT / "Makefile", *sorted((ROOT / "scripts").glob("*.sh"))]
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for forbidden in (
        "rf init",
        "rf smoke-test",
        "doctor --fix",
        "work-frontier",
        "trung.ngo",
    ):
        assert forbidden not in text
    assert "REPO_ID" in text
    assert "REPO_PATH" in text


def test_documented_literal_rf_commands_match_the_public_parser_surface() -> None:
    parser = build_parser()
    documents = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    commands: list[tuple[Path, list[str]]] = []
    for document in documents:
        for line in document.read_text(encoding="utf-8").splitlines():
            stripped = line.strip().removeprefix("$ ")
            if not _COMMAND.match(stripped) or any(token in stripped for token in _SHELL_META):
                continue
            argv = shlex.split(stripped)
            if argv[:2] == ["uv", "run"]:
                argv = argv[2:]
            if len(argv) > 1:
                commands.append((document, argv[1:]))
    assert commands
    for document, argv in commands:
        try:
            _assert_command_surface(parser, argv)
        except AssertionError as exc:
            raise AssertionError(
                f"Stale command in {document.relative_to(ROOT)}: {argv}: {exc}"
            ) from exc


def test_example_config_matches_current_source_schema() -> None:
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert "version = 2" in text
    assert "[[repo]]" in text
    assert 'id = "example-repository"' in text
    assert "[repositories." not in text


def test_repository_profiles_reference_existing_make_targets() -> None:
    config = tomli.loads((ROOT / "config.repoforge.toml").read_text(encoding="utf-8"))
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(_MAKE_TARGET.findall(makefile))
    profile_tables = config["repo"][0]["policy_patch"]["profiles"]

    referenced = {
        command[1]
        for profile in profile_tables.values()
        for command in profile["commands"]
        if command[:1] == ["make"] and len(command) == 2
    }

    assert referenced <= targets, f"missing Make targets: {sorted(referenced - targets)}"


def test_make_default_is_read_only_and_verification_targets_remain_available() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert ".DEFAULT_GOAL := help" in makefile
    assert re.search(r"^help:\s*(?:#.*)?$", makefile, re.MULTILINE)
    assert re.search(r"^check:\s*(?:#.*)?$", makefile, re.MULTILINE)
    assert "scripts/verify-production.sh --allow-dirty" in makefile
    assert re.search(r"^production-check:\s*(?:#.*)?$", makefile, re.MULTILINE)
    assert "scripts/verify-production.sh" in makefile
    assert re.search(r"^tickets:\s*(?:#.*)?$", makefile, re.MULTILINE)
    assert re.search(r"^inspector:\s*(?:#.*)?$", makefile, re.MULTILINE)
    assert re.search(r"^install-hooks:\s*(?:#.*)?$", makefile, re.MULTILINE)


def test_pre_push_autoformats_but_requires_generated_changes_to_be_committed() -> None:
    script = (ROOT / "scripts/pre-push.sh").read_text(encoding="utf-8")

    format_index = script.index('run_check "ruff format" uv run ruff format src tests')
    lint_index = script.index('run_check "ruff check --fix" uv run ruff check --fix src tests')
    typecheck_index = script.index('run_check "mypy --strict" uv run mypy --strict src/repoforge')

    assert "workspace_fingerprint" in script
    assert "ruff format --check" not in script
    assert format_index < lint_index < typecheck_index
    assert "Auto-format changed the working tree" in script
    assert "Review and commit those changes before pushing again" in script


def test_tree_sitter_dependencies_are_exact_and_hash_locked() -> None:
    project = tomli.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(project["project"]["dependencies"])
    pins = {
        "tree-sitter==0.25.2",
        "tree-sitter-javascript==0.25.0",
        "tree-sitter-python==0.25.0",
        "tree-sitter-typescript==0.23.2",
    }

    assert pins <= dependencies
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    for requirement in pins:
        name, version = requirement.split("==", 1)
        package = re.search(
            rf'\[\[package\]\]\nname = "{re.escape(name)}"\nversion = "{re.escape(version)}"(?P<body>.*?)(?=\n\[\[package\]\]|\Z)',
            lock,
            re.DOTALL,
        )
        assert package is not None, f"missing locked package {requirement}"
        assert re.search(r'hash = "sha256:[0-9a-f]{64}"', package.group("body"))


def test_v2_release_gate_measures_primary_and_fallback_provider_recall() -> None:
    script = (ROOT / "scripts/run_v2_release_gates.py").read_text(encoding="utf-8")

    assert "TreeSitterCodeIntelligenceProvider" in script
    assert "SyntaxCodeIntelligenceProvider" in script
    assert "measure_provider_recall" in script
    assert "provider_recall_observations=" in script


def test_release_script_requires_an_explicit_bump_and_is_cross_platform() -> None:
    script = (ROOT / "scripts/release.sh").read_text(encoding="utf-8")

    assert "${1:-}" in script
    assert "${1:-minor}" not in script
    assert "sed -i ''" not in script
    assert "python" in script


def test_release_script_rejects_untracked_files_and_ambiguous_artifacts() -> None:
    script = (ROOT / "scripts/release.sh").read_text(encoding="utf-8")

    assert "git status --porcelain" in script
    assert "rm -rf dist" in script
    assert "find dist" in script
    assert "sha256" in script.lower()
    assert "repoforge_mcp-*.whl" not in script


def test_release_script_verifies_and_builds_before_publishing() -> None:
    script = (ROOT / "scripts/release.sh").read_text(encoding="utf-8")

    verify_index = script.index("scripts/verify-production.sh")
    build_index = script.index("uv build")
    release_index = script.index("gh release create")
    push_index = script.index("git push")

    assert verify_index < build_index < push_index < release_index


def test_make_check_remains_the_stable_full_verification_contract() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "check:" in makefile
    assert "scripts/verify-production.sh --allow-dirty" in makefile


def test_repoforge_source_config_enables_reviewed_relaxed_execution() -> None:
    source = parse_source((ROOT / "config.repoforge.toml").read_text(encoding="utf-8"))
    repository = source.repositories[0]

    assert dict(repository.decisions)["risky_commands"] == "exclude"
    assert repository.policy_patch.execution_mode == "relaxed"
    assert repository.policy_patch.adhoc_runners == ("uv", "python3", "make")
    assert repository.policy_patch.adhoc_timeout_seconds == 600
    assert "ticket-graph" in repository.policy_patch.remove_profiles
