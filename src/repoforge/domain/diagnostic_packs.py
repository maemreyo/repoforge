"""Ready-to-paste ecosystem diagnostic template packs.

These are proposal-ready config snippets surfaced through read-only repository
context output (see ``repo_context`` / ``repo_task_context``) whenever a repo's
detected ecosystem has no enrolled diagnostics. Nothing here is ever
auto-enrolled -- enrolling a diagnostic remains a human/config action.
"""

from __future__ import annotations

from dataclasses import dataclass

_ECOSYSTEM_MARKERS: dict[str, tuple[str, ...]] = {
    "python": ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"),
    "node": ("package.json",),
    "go": ("go.mod",),
    "rust": ("Cargo.toml",),
    "make": ("Makefile", "makefile", "GNUmakefile"),
}


@dataclass(frozen=True, slots=True)
class DiagnosticPackSuggestion:
    ecosystem: str
    diagnostic_id: str
    summary: str
    config_snippet: str


def detect_ecosystems(root_files: tuple[str, ...]) -> frozenset[str]:
    """Detect ecosystems from top-level tracked file names (a bounded, cheap heuristic)."""
    present = set(root_files)
    return frozenset(
        ecosystem
        for ecosystem, markers in _ECOSYSTEM_MARKERS.items()
        if present.intersection(markers)
    )


def _pack(
    ecosystem: str, diagnostic_id: str, summary: str, snippet: str
) -> DiagnosticPackSuggestion:
    return DiagnosticPackSuggestion(ecosystem, diagnostic_id, summary, snippet.strip() + "\n")


_PACKS: dict[str, tuple[DiagnosticPackSuggestion, ...]] = {
    "python": (
        _pack(
            "python",
            "pytest-files",
            "Run up to 8 tracked pytest files in one call",
            """
[repositories.<repo_id>.diagnostics.pytest-files]
summary = "Run tracked pytest files"
argv = ["uv", "run", "pytest", "{selector}", "-q"]
selector_kind = "tracked_path"
selector_max_values = 8
timeout_seconds = 120
network_policy = "local_only"
mutability = "read_only"
parser = "pytest"
output_limit = 12000
""",
        ),
        _pack(
            "python",
            "pytest-keyword",
            "Run pytest filtered by a validated keyword expression",
            """
[repositories.<repo_id>.diagnostics.pytest-keyword]
summary = "Run pytest filtered by keyword"
argv = ["uv", "run", "pytest", "-k", "{selector}", "-q"]
selector_kind = "token"
selector_char_classes = ["alnum", "underscore", "space"]
selector_max_length = 128
timeout_seconds = 120
network_policy = "local_only"
mutability = "read_only"
parser = "pytest"
output_limit = 12000
""",
        ),
        _pack(
            "python",
            "ruff-path",
            "Lint one tracked path with ruff",
            """
[repositories.<repo_id>.diagnostics.ruff-path]
summary = "Lint one tracked path"
argv = ["uv", "run", "ruff", "check", "{selector}"]
selector_kind = "tracked_path"
timeout_seconds = 60
network_policy = "local_only"
mutability = "read_only"
parser = "text"
output_limit = 12000
""",
        ),
        _pack(
            "python",
            "mypy-package",
            "Typecheck one package name with mypy",
            """
[repositories.<repo_id>.diagnostics.mypy-package]
summary = "Typecheck one package"
argv = ["uv", "run", "mypy", "{selector}"]
selector_kind = "package_name"
timeout_seconds = 120
network_policy = "local_only"
mutability = "read_only"
parser = "text"
output_limit = 12000
""",
        ),
    ),
    "node": (
        _pack(
            "node",
            "vitest-files",
            "Run up to 8 tracked vitest files in one call",
            """
[repositories.<repo_id>.diagnostics.vitest-files]
summary = "Run tracked vitest files"
argv = ["npx", "vitest", "run", "{selector}"]
selector_kind = "tracked_path"
selector_max_values = 8
timeout_seconds = 120
network_policy = "local_only"
mutability = "read_only"
parser = "text"
output_limit = 12000
""",
        ),
        _pack(
            "node",
            "eslint-path",
            "Lint one tracked path with eslint",
            """
[repositories.<repo_id>.diagnostics.eslint-path]
summary = "Lint one tracked path"
argv = ["npx", "eslint", "{selector}"]
selector_kind = "tracked_path"
timeout_seconds = 60
network_policy = "local_only"
mutability = "read_only"
parser = "text"
output_limit = 12000
""",
        ),
        _pack(
            "node",
            "tsc-project",
            "Typecheck one tsconfig project path",
            """
[repositories.<repo_id>.diagnostics.tsc-project]
summary = "Typecheck one tsconfig project"
argv = ["npx", "tsc", "-p", "{selector}", "--noEmit"]
selector_kind = "tracked_path"
timeout_seconds = 120
network_policy = "local_only"
mutability = "read_only"
parser = "text"
output_limit = 12000
""",
        ),
    ),
    "go": (
        _pack(
            "go",
            "go-test-pkg",
            "Run go test for one package filtered by a validated test-name token",
            """
[repositories.<repo_id>.diagnostics.go-test-pkg]
summary = "Run go test for one package, filtered by name"
argv = ["go", "test", "{selector}", "-run", "{selector:name}"]
selector_kind = "token"
selector_char_classes = ["alnum", "path"]
selector_max_length = 128

[repositories.<repo_id>.diagnostics.go-test-pkg.selectors.name]
kind = "token"
char_classes = ["alnum"]
max_length = 128
""",
        ),
    ),
    "rust": (
        _pack(
            "rust",
            "cargo-test-name",
            "Run cargo test filtered by a validated test-name token",
            """
[repositories.<repo_id>.diagnostics.cargo-test-name]
summary = "Run cargo test filtered by name"
argv = ["cargo", "test", "{selector}"]
selector_kind = "token"
selector_char_classes = ["alnum", "path"]
selector_max_length = 128
timeout_seconds = 300
network_policy = "local_only"
mutability = "read_only"
parser = "text"
output_limit = 12000
""",
        ),
    ),
    "make": (
        _pack(
            "make",
            "make-target",
            "Run one reviewed, enumerated make target (replace values with the repo's real targets)",
            """
[repositories.<repo_id>.diagnostics.make-target]
summary = "Run one reviewed make target"
argv = ["make", "{selector}"]
selector_kind = "enum"
selector_values = ["lint", "typecheck"]
timeout_seconds = 300
network_policy = "local_only"
mutability = "read_only"
parser = "text"
output_limit = 12000
""",
        ),
    ),
}


def ecosystem_diagnostic_packs(ecosystems: frozenset[str]) -> tuple[DiagnosticPackSuggestion, ...]:
    """Return every proposal-ready pack for the given detected ecosystems, sorted deterministically."""
    return tuple(pack for ecosystem in sorted(ecosystems) for pack in _PACKS.get(ecosystem, ()))


__all__ = [
    "DiagnosticPackSuggestion",
    "detect_ecosystems",
    "ecosystem_diagnostic_packs",
]
