"""Deterministic reference executor for the frozen Forge v2 release corpora."""

from __future__ import annotations

import time
from collections.abc import Mapping

from ..application.read_batch import FileReadRequest, LoadedTextFile, execute_batch_read
from ..domain.errors import SecurityError
from ..domain.patches import inspect_patch, normalize_patch
from ..ports.code_intelligence import CodeIntelligenceProvider
from .code_intelligence import measure_provider_recall
from .harness import CaseObservation, CorpusCase

_BASE_FILES: dict[str, str] = {
    "README.md": "# Existing\n",
    "src/example.py": "def enabled():\n    return False\n",
    "obsolete.txt": "old\n",
    "old.txt": "old\n",
    "src/a.py": "A = 1\n\n\n\nB = 1\n",
    "repeated.txt": "same\nsame\n",
    "old_name.py": "def run():\n    return True\n",
}


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError("expected an array of strings")
    return tuple(value)


def _integer(value: object, context: str, *, default: int | None = None) -> int:
    selected = default if value is None else value
    if not isinstance(selected, int) or isinstance(selected, bool):
        raise ValueError(f"{context} must be an integer")
    return selected


def _expected_paths(case: CorpusCase) -> tuple[str, ...]:
    raw = case.expected.get("changed_paths", ())
    return tuple(sorted(_strings(raw)))


def _observation(
    case: CorpusCase,
    *,
    started: float,
    success: bool,
    wrong_target: bool = False,
    regression_caught: bool = False,
    fell_back_full: bool = False,
    truncated: bool = False,
    resume_metadata: bool = True,
    details: Mapping[str, object] | None = None,
) -> CaseObservation:
    return CaseObservation(
        corpus=case.corpus,
        case_id=case.case_id,
        success=success,
        wrong_target=wrong_target,
        regression_caught=regression_caught,
        fell_back_full=fell_back_full,
        truncated=truncated,
        resume_metadata=resume_metadata,
        duration_ms=(time.perf_counter() - started) * 1_000.0,
        details=details or {},
    )


def _generated_changes(case: CorpusCase, started: float) -> CaseObservation:
    files = dict(_BASE_FILES)
    original = dict(files)
    operations = case.input.get("operations")
    if not isinstance(operations, list):
        raise ValueError("generated change operations must be an array")
    dry_run = case.input.get("dry_run") is True
    failed = False
    diagnostics = 0
    try:
        for raw in operations:
            if not isinstance(raw, dict) or not isinstance(raw.get("op"), str):
                raise ValueError("each generated operation must be an object with op")
            operation = str(raw["op"])
            path = raw.get("path")
            if isinstance(path, str) and path.startswith(".github/workflows/"):
                raise SecurityError("denied workflow path")
            diagnostics += 1
            if operation == "replace_text":
                assert isinstance(path, str)
                old = str(raw["old_text"])
                expected = _integer(
                    raw.get("expected_occurrences"),
                    "expected_occurrences",
                    default=1,
                )
                current = files.get(path)
                if current is None or current.count(old) != expected:
                    raise ValueError("stale or ambiguous replacement")
                files[path] = current.replace(old, str(raw.get("new_text", "")))
            elif operation == "write":
                assert isinstance(path, str)
                files[path] = str(raw.get("content", ""))
            elif operation == "create":
                assert isinstance(path, str)
                if path in files:
                    raise ValueError("create target already exists")
                files[path] = str(raw.get("content", ""))
            elif operation == "delete":
                assert isinstance(path, str)
                if path not in files:
                    raise ValueError("delete target is missing")
                del files[path]
            elif operation == "move":
                source = str(raw["source"])
                destination = str(raw["destination"])
                if source not in files or destination in files:
                    raise ValueError("invalid move")
                files[destination] = files.pop(source)
            else:
                raise ValueError(f"unsupported generated operation: {operation}")
    except (SecurityError, ValueError):
        failed = True
        files = original

    if dry_run:
        files = original
    changed_paths = tuple(
        sorted(path for path in set(original) | set(files) if original.get(path) != files.get(path))
    )
    expected_failed = case.expected.get("status") == "failed"
    expected_changed = case.expected.get("changed")
    success = failed == expected_failed and changed_paths == _expected_paths(case)
    if expected_changed is not None:
        success = success and bool(changed_paths) is bool(expected_changed)
    if "diagnostic_count" in case.expected:
        success = success and diagnostics == _integer(
            case.expected["diagnostic_count"], "diagnostic_count"
        )
    return _observation(
        case,
        started=started,
        success=success,
        wrong_target=bool(set(changed_paths) - set(_expected_paths(case))),
        details={"changed_paths": changed_paths, "failed": failed, "dry_run": dry_run},
    )


def _patches(case: CorpusCase, started: float) -> CaseObservation:
    patch = case.input.get("patch")
    if not isinstance(patch, str):
        raise ValueError("patch corpus input requires text")

    failed = False
    paths: tuple[str, ...] = ()
    try:
        inspection = inspect_patch(patch)
        paths = tuple(sorted(inspection.paths))
        if any(".." in path.split("/") for path in paths):
            raise SecurityError("path traversal")
        # Always normalize, matching production's unconditional
        # normalize_workspace_patch() call for every input format -- a
        # reference executor that only exercises normalize_patch for
        # openai_apply_patch input hides bugs unified-diff-format patches
        # would hit in production (#225 review).
        patch_files = {**_BASE_FILES, "README.md": "# Demo\n"}
        normalized = normalize_patch(patch, lambda path: patch_files.get(path))
        paths = tuple(sorted(normalized.paths))
    except (SecurityError, ValueError, RuntimeError):
        failed = True
        paths = ()

    expected_failed = case.expected.get("status") == "failed"
    expected_paths = _expected_paths(case)
    success = failed == expected_failed and (failed or paths == expected_paths)
    return _observation(
        case,
        started=started,
        success=success,
        wrong_target=bool(set(paths) - set(expected_paths)),
        details={"paths": paths, "failed": failed},
    )


def _seeded_bug(
    case: CorpusCase,
    started: float,
    provider: CodeIntelligenceProvider | None,
) -> CaseObservation:
    if case.metadata.get("provider_recall") is True and provider is not None:
        recall = measure_provider_recall(provider, (case,))
        assert len(recall) == 1
        observation = recall[0]
        expected = set(observation.expected_tests)
        routed = set(observation.routed_tests)
        caught = bool(expected) and expected <= routed
        return _observation(
            case,
            started=started,
            success=caught,
            regression_caught=caught,
            wrong_target=bool(routed - expected),
            details={"expected_tests": sorted(expected), "routed_tests": sorted(routed)},
        )
    return _observation(
        case,
        started=started,
        success=True,
        fell_back_full=True,
        details={"reason": "no complete provider fixture; full verification required"},
    )


def _read_loader(path: str) -> LoadedTextFile:
    if path == ".env":
        raise SecurityError("denied path")
    if path == "binary.dat":
        return LoadedTextFile(path, b"\x00\xff")
    contents = {
        "small.txt": "one\ntwo\nthree\n",
        "a.txt": "\n".join(f"a{index}" for index in range(1, 21)) + "\n",
        "b.txt": "\n".join(f"b{index}" for index in range(1, 21)) + "\n",
        "large.txt": "\n".join(f"line-{index}-" + "x" * 40 for index in range(1, 201)) + "\n",
    }
    if path not in contents:
        raise ValueError(f"unknown read fixture: {path}")
    return LoadedTextFile(path, contents[path].encode("utf-8"))


def _requests(raw: object) -> tuple[FileReadRequest, ...]:
    if not isinstance(raw, list):
        raise ValueError("read files must be an array")
    return tuple(
        FileReadRequest(
            path=str(item["path"]),
            start_line=_integer(item.get("start_line"), "start_line", default=1),
            end_line=_integer(item.get("end_line"), "end_line", default=500),
        )
        for item in raw
        if isinstance(item, dict)
    )


def _read_golden(case: CorpusCase, started: float) -> CaseObservation:
    if case.case_id == "resume-cursor-no-duplication":
        request = (FileReadRequest("large.txt", 1, 2_001),)
        first = execute_batch_read(
            kind="release-gate-read",
            scope="golden",
            requests=request,
            loader=_read_loader,
            byte_budget=200,
            cursor=None,
        )
        chunks = [item.content for item in first.files]
        cursor = first.next_cursor
        seen: set[str] = set()
        while cursor is not None:
            if cursor in seen:
                return _observation(case, started=started, success=False, resume_metadata=False)
            seen.add(cursor)
            page = execute_batch_read(
                kind="release-gate-read",
                scope="golden",
                requests=request,
                loader=_read_loader,
                byte_budget=200,
                cursor=cursor,
            )
            chunks.extend(item.content for item in page.files)
            cursor = page.next_cursor
        expected = _read_loader("large.txt").data.decode("utf-8")
        rendered = "".join(chunks)
        expected_rendered = "\n".join(
            f"{index}: {line}" for index, line in enumerate(expected.splitlines(), 1)
        )
        return _observation(
            case,
            started=started,
            success=rendered == expected_rendered,
            details={"pages": len(seen) + 1},
        )

    result = execute_batch_read(
        kind="release-gate-read",
        scope=case.case_id,
        requests=_requests(case.input.get("files")),
        loader=_read_loader,
        byte_budget=_integer(case.input.get("byte_budget"), "byte_budget", default=60_000),
        cursor=None,
    )
    expected_failed = case.expected.get("status") == "failed"
    failed = bool(result.errors)
    success = failed == expected_failed
    if "truncated" in case.expected:
        success = success and result.truncated is bool(case.expected["truncated"])
    if case.expected.get("next_cursor") is True:
        success = success and result.next_cursor is not None
    if case.expected.get("per_file_ranges") is True:
        success = success and len(result.files) == 2 and result.files[0].start_line == 2
    return _observation(
        case,
        started=started,
        success=success,
        truncated=result.truncated,
        resume_metadata=not result.truncated or result.next_cursor is not None,
        details={"errors": len(result.errors), "files": len(result.files)},
    )


class ReferenceExecutor:
    """Provider-neutral deterministic executor for the frozen release corpora."""

    def __init__(self, provider: CodeIntelligenceProvider | None = None) -> None:
        self._provider = provider

    def __call__(self, case: CorpusCase) -> CaseObservation:
        started = time.perf_counter()
        if case.corpus == "generated_changes":
            return _generated_changes(case, started)
        if case.corpus == "patches":
            return _patches(case, started)
        if case.corpus == "seeded_bugs":
            return _seeded_bug(case, started, self._provider)
        if case.corpus == "read_golden":
            return _read_golden(case, started)
        raise ValueError(f"unsupported corpus: {case.corpus}")


__all__ = ["ReferenceExecutor"]
