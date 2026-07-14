from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

from repoforge.interfaces.cli.main import build_parser

ROOT = Path(__file__).parents[1]
_COMMAND = re.compile(r"^(?:uv run )?(?:rf|repoforge)(?:\s|$)")
_SHELL_META = ("\\", "<", ">", "$", "{", "}", "|", "&&", ";")


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
