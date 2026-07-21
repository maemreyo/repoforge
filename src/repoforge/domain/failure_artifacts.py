"""Provider-neutral extraction of bounded actionable verification failures."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

_MAX_SELECTORS = 100
_MAX_LOCATIONS = 100
_PYTEST_LEADING = re.compile(
    r"^(?:FAILED|ERROR)\s+(?P<selector>[^\s]+(?:\:\:[^\s]+)*)",
    re.MULTILINE,
)
_PYTEST_TRAILING = re.compile(
    r"^(?P<selector>[^\s]+\:\:[^\s]+)\s+(?:FAILED|ERROR)\b",
    re.MULTILINE,
)
_RUFF_LOCATION = re.compile(
    r"^(?P<path>[^\s:][^:\n]*):(?P<line>\d+):(?P<column>\d+):\s*"
    r"(?P<code>[A-Z][A-Z0-9]{1,9})\b",
    re.MULTILINE,
)
_MYPY_LOCATION = re.compile(
    r"^(?P<path>[^\s:][^:\n]*):(?P<line>\d+)(?::(?P<column>\d+))?:\s*"
    r"(?:error|note):.*?(?:\[(?P<code>[a-z0-9-]+)\])?\s*$",
    re.MULTILINE,
)
_UNITTEST_FAILURE = re.compile(
    r"^(?:FAIL|ERROR):\s+(?P<test>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"\((?P<scope>[A-Za-z_][A-Za-z0-9_.]*)\)",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class FailureLocation:
    path: str
    line: int | None = None
    column: int | None = None
    code: str | None = None


@dataclass(frozen=True, slots=True)
class FailureExtraction:
    provider: str | None
    selector_coverage: str
    selectors_unavailable_reason: str | None
    selectors: tuple[str, ...]
    locations: tuple[FailureLocation, ...]


def _normalized_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return PurePosixPath(normalized).as_posix()


def _dedupe(values: Sequence[str], *, limit: int = _MAX_SELECTORS) -> tuple[tuple[str, ...], bool]:
    result: list[str] = []
    seen: set[str] = set()
    truncated = False
    for value in values:
        normalized = value.strip().replace("\\", "/").rstrip(":")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if len(result) >= limit:
            truncated = True
            break
        result.append(normalized)
    return tuple(result), truncated


def _locations(pattern: re.Pattern[str], output: str) -> tuple[tuple[FailureLocation, ...], bool]:
    result: list[FailureLocation] = []
    seen: set[tuple[str, int | None, int | None, str | None]] = set()
    truncated = False
    for match in pattern.finditer(output):
        path = _normalized_path(match.group("path"))
        line_text = match.groupdict().get("line")
        column_text = match.groupdict().get("column")
        code = match.groupdict().get("code")
        identity = (
            path,
            int(line_text) if line_text else None,
            int(column_text) if column_text else None,
            code,
        )
        if identity in seen:
            continue
        seen.add(identity)
        if len(result) >= _MAX_LOCATIONS:
            truncated = True
            break
        result.append(FailureLocation(*identity))
    return tuple(result), truncated


def _provider_from_argv(argv: Sequence[str]) -> str | None:
    tokens = tuple(PurePosixPath(value.replace("\\", "/")).name.lower() for value in argv)
    if any(token in {"pytest", "py.test"} for token in tokens):
        return "pytest"
    if "unittest" in tokens:
        return "unittest"
    if "ruff" in tokens:
        return "ruff"
    if "mypy" in tokens:
        return "mypy"
    if any(token in {"build", "package", "wheel", "sdist"} for token in tokens):
        return "build"
    if any("schema" in token or "contract" in token for token in tokens):
        return "schema"
    return None


def extract_failure(
    argv: Sequence[str],
    output: str,
    *,
    returncode: int,
) -> FailureExtraction:
    """Extract exact rerun selectors and source locations before output truncation."""

    if returncode == 0:
        return FailureExtraction(None, "not_applicable", None, (), ())

    pytest_selectors, pytest_truncated = _dedupe(
        [
            *(match.group("selector") for match in _PYTEST_LEADING.finditer(output)),
            *(match.group("selector") for match in _PYTEST_TRAILING.finditer(output)),
        ]
    )
    if pytest_selectors:
        return FailureExtraction(
            "pytest",
            "partial" if pytest_truncated else "complete",
            "selectors_truncated" if pytest_truncated else None,
            pytest_selectors,
            (),
        )

    ruff_locations, ruff_locations_truncated = _locations(_RUFF_LOCATION, output)
    if ruff_locations:
        ruff_selectors, ruff_selectors_truncated = _dedupe([item.path for item in ruff_locations])
        ruff_truncated = ruff_locations_truncated or ruff_selectors_truncated
        return FailureExtraction(
            "ruff",
            "partial" if ruff_truncated else "complete",
            "selectors_truncated" if ruff_truncated else None,
            ruff_selectors,
            ruff_locations,
        )

    mypy_locations, mypy_locations_truncated = _locations(_MYPY_LOCATION, output)
    if mypy_locations:
        mypy_selectors, mypy_selectors_truncated = _dedupe([item.path for item in mypy_locations])
        mypy_truncated = mypy_locations_truncated or mypy_selectors_truncated
        return FailureExtraction(
            "mypy",
            "partial" if mypy_truncated else "complete",
            "selectors_truncated" if mypy_truncated else None,
            mypy_selectors,
            mypy_locations,
        )

    unittest_selectors, unittest_truncated = _dedupe(
        [
            f"{match.group('scope')}.{match.group('test')}"
            for match in _UNITTEST_FAILURE.finditer(output)
        ]
    )
    if unittest_selectors:
        return FailureExtraction(
            "unittest",
            "partial" if unittest_truncated else "complete",
            "selectors_truncated" if unittest_truncated else None,
            unittest_selectors,
            (),
        )

    return FailureExtraction(
        _provider_from_argv(argv) or "custom",
        "unavailable",
        "output_unrecognized",
        (),
        (),
    )
