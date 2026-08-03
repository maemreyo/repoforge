#!/usr/bin/env python3
"""Validate and compile RepoForge's declarative test catalog in shadow mode."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli as tomllib

_VALID_COSTS = frozenset({"small", "medium", "large", "system"})
_VALID_PLATFORMS = frozenset({"linux", "macos", "windows"})
_SAFE_RESOURCE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


class CatalogError(ValueError):
    """The catalog cannot safely own or schedule the test suite."""


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    source_globs: tuple[str, ...]
    test_files: tuple[str, ...]
    isolation: str
    resources: tuple[str, ...]
    platforms: tuple[str, ...]
    python_versions: tuple[str, ...]
    cost: str


@dataclass(frozen=True, slots=True)
class TestCatalog:
    version: int
    capabilities: tuple[Capability, ...]
    safety_bundle: tuple[str, ...]
    source_tests: tuple[tuple[str, tuple[str, ...]], ...]
    digest: str

    @property
    def owned_tests(self) -> tuple[str, ...]:
        return tuple(
            sorted(test for capability in self.capabilities for test in capability.test_files)
        )


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    intent: str
    catalog_digest: str
    selected_files: tuple[str, ...]
    lanes: dict[str, tuple[str, ...]]
    reasons: tuple[str, ...]
    escalated: bool
    escalation_reason: str | None


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    false_negatives: tuple[str, ...]
    extras: tuple[str, ...]
    safe_to_cut_over: bool


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(glob):
        if glob[index : index + 3] == "**/":
            parts.append("(?:.*/)?")
            index += 3
        elif glob[index : index + 2] == "**":
            parts.append(".*")
            index += 2
        elif glob[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(glob[index]))
            index += 1
    return re.compile("^" + "".join(parts) + "$")


def _matches(path: str, globs: tuple[str, ...]) -> bool:
    return any(_glob_to_regex(glob).match(path) for glob in globs)


def _string_tuple(payload: dict[str, Any], name: str) -> tuple[str, ...]:
    value = payload.get(name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CatalogError(f"{name} must be a list of strings")
    return tuple(value)


def _canonical_payload(
    version: int,
    capabilities: tuple[Capability, ...],
    safety_bundle: tuple[str, ...],
    source_tests: tuple[tuple[str, tuple[str, ...]], ...],
) -> dict[str, object]:
    return {
        "version": version,
        "safety_bundle": list(safety_bundle),
        "source_tests": {source: list(tests) for source, tests in source_tests},
        "capabilities": [
            {
                "name": capability.name,
                "source_globs": list(capability.source_globs),
                "test_files": list(capability.test_files),
                "isolation": capability.isolation,
                "resources": list(capability.resources),
                "platforms": list(capability.platforms),
                "python_versions": list(capability.python_versions),
                "cost": capability.cost,
            }
            for capability in capabilities
        ],
    }


def load_catalog(path: Path) -> TestCatalog:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    version = raw.get("version")
    if version != 1:
        raise CatalogError("catalog version must be 1")
    raw_capabilities = raw.get("capabilities", {})
    if not isinstance(raw_capabilities, dict) or not raw_capabilities:
        raise CatalogError("catalog must declare at least one capability")

    capabilities: list[Capability] = []
    ownership: dict[str, str] = {}
    for name, value in sorted(raw_capabilities.items()):
        if not isinstance(name, str) or not isinstance(value, dict):
            raise CatalogError("capability entries must be named tables")
        source_globs = _string_tuple(value, "source_globs")
        test_files = _string_tuple(value, "test_files")
        isolation = value.get("isolation")
        resources = _string_tuple(value, "resources")
        platforms = _string_tuple(value, "platforms")
        python_versions = _string_tuple(value, "python_versions")
        cost = value.get("cost")
        if not source_globs or not test_files:
            raise CatalogError(f"capability {name!r} must own source globs and tests")
        if isolation not in {"pure", "sandboxed_fs", "sandboxed_process", "system"} and not (
            isinstance(isolation, str) and isolation.startswith("exclusive:")
        ):
            raise CatalogError(f"capability {name!r} has invalid isolation")
        if any(_SAFE_RESOURCE.fullmatch(item) is None for item in resources):
            raise CatalogError(f"capability {name!r} has invalid resources")
        if isinstance(isolation, str) and isolation.startswith("exclusive:"):
            resource = isolation.split(":", 1)[1]
            if _SAFE_RESOURCE.fullmatch(resource) is None or resource not in resources:
                raise CatalogError(f"capability {name!r} must declare its exclusive resource")
        if not platforms or not set(platforms) <= _VALID_PLATFORMS:
            raise CatalogError(f"capability {name!r} has invalid platforms")
        if not python_versions or any(
            re.fullmatch(r"3\.1[0-9]", item) is None for item in python_versions
        ):
            raise CatalogError(f"capability {name!r} has invalid Python versions")
        if cost not in _VALID_COSTS:
            raise CatalogError(f"capability {name!r} has invalid cost")
        for test_file in test_files:
            previous = ownership.get(test_file)
            if previous is not None:
                raise CatalogError(
                    f"test file {test_file!r} is owned by more than one capability: "
                    f"{previous!r}, {name!r}"
                )
            ownership[test_file] = name
        capabilities.append(
            Capability(
                name=name,
                source_globs=tuple(sorted(set(source_globs))),
                test_files=tuple(sorted(set(test_files))),
                isolation=isolation,
                resources=tuple(sorted(set(resources))),
                platforms=tuple(sorted(set(platforms))),
                python_versions=tuple(sorted(set(python_versions))),
                cost=cost,
            )
        )

    safety_raw = raw.get("safety_bundle", {})
    if not isinstance(safety_raw, dict):
        raise CatalogError("safety_bundle must be a table")
    safety_bundle = tuple(sorted(set(_string_tuple(safety_raw, "test_files"))))
    source_tests_raw = raw.get("source_tests", {})
    if not isinstance(source_tests_raw, dict):
        raise CatalogError("source_tests must be a table")
    source_tests: list[tuple[str, tuple[str, ...]]] = []
    for source, tests in sorted(source_tests_raw.items()):
        if (
            not isinstance(source, str)
            or not isinstance(tests, list)
            or any(not isinstance(test, str) for test in tests)
        ):
            raise CatalogError("source_tests entries must map source paths to test-file lists")
        source_tests.append((source, tuple(sorted(set(tests)))))
    capability_tuple = tuple(capabilities)
    source_test_tuple = tuple(source_tests)
    canonical = _canonical_payload(version, capability_tuple, safety_bundle, source_test_tuple)
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return TestCatalog(version, capability_tuple, safety_bundle, source_test_tuple, digest)


def validate_catalog(catalog: TestCatalog, root: Path) -> tuple[str, ...]:
    on_disk = {
        f"tests/{path.name}"
        for path in (root / "tests").glob("test_*.py")
        if path.name != "conftest.py"
    }
    owned = set(catalog.owned_tests)
    source_references = {
        test_file for _source, test_files in catalog.source_tests for test_file in test_files
    }
    violations = [
        *(f"test file {path!r} is not owned by the catalog" for path in sorted(on_disk - owned)),
        *(f"catalog references missing test file {path!r}" for path in sorted(owned - on_disk)),
        *(
            f"safety bundle references unowned test file {path!r}"
            for path in sorted(set(catalog.safety_bundle) - owned)
        ),
        *(
            f"source_tests references unowned test file {path!r}"
            for path in sorted(source_references - owned)
        ),
    ]
    return tuple(violations)


def _lane_for(capability: Capability | None) -> str:
    if capability is None or capability.isolation in {"pure", "sandboxed_fs", "sandboxed_process"}:
        return "parallel"
    return capability.isolation


def compile_plan(
    catalog: TestCatalog,
    changed_paths: list[str] | tuple[str, ...],
    *,
    intent: str,
) -> VerificationPlan:
    selected = set(catalog.safety_bundle)
    reasons: list[str] = []
    unknown: list[str] = []
    ownership = {
        test: capability for capability in catalog.capabilities for test in capability.test_files
    }
    exact_source_tests = dict(catalog.source_tests)
    if intent in {"full", "coverage", "gate"}:
        selected.update(catalog.owned_tests)
        reasons.append(f"intent {intent!r} requires every catalog test")
    else:
        for path in sorted(set(changed_paths)):
            if path.startswith("tests/") and path.endswith(".py") and path in ownership:
                selected.add(path)
                reasons.append(f"{path!r} -> itself")
                continue
            exact = exact_source_tests.get(path)
            if exact is not None:
                selected.update(exact)
                reasons.append(f"{path!r} -> {len(exact)} exact catalog test edge(s)")
                continue
            matched = [
                capability
                for capability in catalog.capabilities
                if _matches(path, capability.source_globs)
            ]
            if not matched:
                unknown.append(path)
                continue
            for capability in matched:
                selected.update(capability.test_files)
                reasons.append(f"{path!r} -> {capability.name!r}")
    escalated = bool(unknown)
    escalation_reason = None
    if unknown:
        selected.update(catalog.owned_tests)
        escalation_reason = "unowned changed paths: " + ", ".join(unknown)
    lanes: dict[str, list[str]] = {}
    for test_file in sorted(selected):
        lane = _lane_for(ownership.get(test_file))
        lanes.setdefault(lane, []).append(test_file)
    return VerificationPlan(
        intent=intent,
        catalog_digest=catalog.digest,
        selected_files=tuple(sorted(selected)),
        lanes={name: tuple(files) for name, files in sorted(lanes.items())},
        reasons=tuple(sorted(reasons)),
        escalated=escalated,
        escalation_reason=escalation_reason,
    )


def compare_shadow(
    *,
    authoritative_files: tuple[str, ...],
    shadow_files: tuple[str, ...],
) -> ShadowComparison:
    authoritative = set(authoritative_files)
    shadow = set(shadow_files)
    false_negatives = tuple(sorted(authoritative - shadow))
    extras = tuple(sorted(shadow - authoritative))
    return ShadowComparison(false_negatives, extras, not false_negatives)


def _toml_list(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(json.dumps(value) for value in values) + "]"


def render_from_manifest(manifest_path: Path) -> str:
    with manifest_path.open("rb") as handle:
        raw = tomllib.load(handle)
    lines = ["version = 1", "", "[safety_bundle]"]
    safety = raw.get("safety_bundle", {}).get("test_files", [])
    lines.append(f"test_files = {_toml_list(safety)}")
    coverage_path = manifest_path.parent / "coverage-map.json"
    coverage_map = json.loads(coverage_path.read_text(encoding="utf-8"))
    lines.extend(["", "[source_tests]"])
    for source, tests in sorted(coverage_map.items()):
        lines.append(f"{json.dumps(source)} = {_toml_list(tests)}")
    for name, payload in sorted(raw.get("groups", {}).items()):
        parallel = bool(payload.get("parallel", False))
        resource = re.sub(r"[^a-z0-9._-]+", "-", str(name).lower()).strip("-")
        isolation = "pure" if parallel else f"exclusive:{resource}"
        resources = [] if parallel else [resource]
        lines.extend(
            [
                "",
                f"[capabilities.{name}]",
                f"source_globs = {_toml_list(payload.get('source_globs', []))}",
                f"test_files = {_toml_list(payload.get('test_files', []))}",
                f"isolation = {json.dumps(isolation)}",
                f"resources = {_toml_list(resources)}",
                'platforms = ["linux", "macos"]',
                'python_versions = ["3.10", "3.11", "3.12", "3.13"]',
                f"cost = {json.dumps('large' if not parallel else 'medium')}",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--write-from-manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.write_from_manifest is not None:
        if args.output is None:
            parser.error("--write-from-manifest requires --output")
        args.output.write_text(render_from_manifest(args.write_from_manifest), encoding="utf-8")
        return 0
    if args.check is None:
        parser.error("pass --check or --write-from-manifest")
    catalog = load_catalog(args.check)
    violations = validate_catalog(catalog, args.root.resolve())
    if violations:
        for violation in violations:
            print(f"[test-catalog] {violation}")
        return 1
    print(f"[test-catalog] complete: {len(catalog.owned_tests)} tests; digest={catalog.digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
