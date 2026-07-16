"""Deterministic patch format detection and canonical unified-diff rendering."""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

from .errors import ErrorCode, RepoForgeError

ReadFile = Callable[[str], str | None]
_MAX_PATCH_CHARS = 2_000_000
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$")


@dataclass(frozen=True, slots=True)
class PatchInspection:
    input_format: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PatchNormalizationResult:
    patch: str
    input_format: str
    input_sha256: str
    normalized_sha256: str
    paths: tuple[str, ...]
    repair_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Hunk:
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _UnifiedFile:
    old_path: str | None
    new_path: str | None
    hunks: tuple[_Hunk, ...]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _error(
    message: str,
    *,
    code: ErrorCode,
    safe_next_action: str,
    details: dict[str, object] | None = None,
) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        safe_next_action=safe_next_action,
        unchanged_state=("The workspace tree, index, and HEAD were not modified.",),
        details=details,
    )


def _normalize_path(raw: str) -> str:
    value = raw.strip()
    if value == "/dev/null":
        return value
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value or value.startswith("/") or "\x00" in value:
        raise _error(
            f"Invalid patch path: {raw!r}",
            code=ErrorCode.PATCH_PARSE_FAILED,
            safe_next_action="Use repository-relative UTF-8 paths in a unified diff or OpenAI apply_patch envelope.",
            details={"path": raw},
        )
    return value


def _envelope_paths(text: str) -> tuple[str, ...]:
    paths: set[str] = set()
    for line in text.splitlines():
        for prefix in ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: "):
            if line.startswith(prefix):
                paths.add(_normalize_path(line[len(prefix) :]))
                break
    if not paths:
        raise _error(
            "OpenAI apply_patch envelope contains no file directives",
            code=ErrorCode.PATCH_PARSE_FAILED,
            safe_next_action="Add at least one *** Add File, *** Update File, or *** Delete File directive.",
        )
    return tuple(sorted(paths))


def _unified_paths(text: str) -> tuple[str, ...]:
    paths: set[str] = set()
    for line in text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) != 4:
                raise _error(
                    "Malformed diff --git header",
                    code=ErrorCode.PATCH_PARSE_FAILED,
                    safe_next_action="Regenerate the patch as a standard unified diff.",
                    details={"header": line},
                )
            for raw in parts[2:4]:
                normalized = _normalize_path(raw)
                if normalized != "/dev/null":
                    paths.add(normalized)
    if not paths:
        raise _error(
            "Unified patch contains no diff --git file headers",
            code=ErrorCode.PATCH_PARSE_FAILED,
            safe_next_action="Provide a git-style unified diff with diff --git, ---, +++, and @@ headers.",
        )
    return tuple(sorted(paths))


def inspect_patch(text: str) -> PatchInspection:
    if not isinstance(text, str) or not text.strip():
        raise _error(
            "Patch input is empty",
            code=ErrorCode.PATCH_FORMAT_UNSUPPORTED,
            safe_next_action="Use workspace_write_file for full content, workspace_edit for one exact replacement, or provide a reviewed patch.",
            details={"accepted_formats": ["unified_diff", "openai_apply_patch"]},
        )
    if len(text) > _MAX_PATCH_CHARS:
        raise _error(
            f"Patch input exceeds {_MAX_PATCH_CHARS} characters",
            code=ErrorCode.PATCH_PARSE_FAILED,
            safe_next_action="Split the change into smaller reviewed patches.",
            details={"max_patch_chars": _MAX_PATCH_CHARS, "actual_patch_chars": len(text)},
        )
    stripped = text.lstrip()
    if stripped.startswith("*** Begin Patch"):
        if not stripped.rstrip().endswith("*** End Patch"):
            raise _error(
                "OpenAI apply_patch envelope is missing *** End Patch",
                code=ErrorCode.PATCH_PARSE_FAILED,
                safe_next_action="Close the envelope with exactly one *** End Patch marker.",
            )
        return PatchInspection("openai_apply_patch", _envelope_paths(stripped))
    if any(line.startswith("diff --git ") for line in text.splitlines()):
        return PatchInspection("unified_diff", _unified_paths(text))
    raise _error(
        "Unsupported patch format",
        code=ErrorCode.PATCH_FORMAT_UNSUPPORTED,
        safe_next_action=(
            "Provide a git-style unified diff or an OpenAI *** Begin Patch envelope. "
            "For full-file content use workspace_write_file; for one exact edit use workspace_edit."
        ),
        details={"accepted_formats": ["unified_diff", "openai_apply_patch"]},
    )


def _split_text(text: str) -> list[str]:
    return text.splitlines()


def _join_text(lines: list[str], *, trailing_newline: bool = True) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if trailing_newline else "")


def _canonical_diff(old_path: str | None, new_path: str | None, old: str, new: str) -> str:
    display_old = f"a/{old_path}" if old_path is not None else "/dev/null"
    display_new = f"b/{new_path}" if new_path is not None else "/dev/null"
    identity_old = old_path or new_path
    identity_new = new_path or old_path
    assert identity_old is not None and identity_new is not None
    lines = [f"diff --git a/{identity_old} b/{identity_new}"]
    if old_path is None:
        lines.append("new file mode 100644")
    if new_path is None:
        lines.append("deleted file mode 100644")
    rendered = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=display_old,
            tofile=display_new,
            lineterm="",
            n=1,
        )
    )
    if not rendered and old_path == new_path:
        return ""
    lines.extend(rendered)
    return "\n".join(lines).rstrip() + "\n"


def _context_error(
    *,
    code: ErrorCode,
    path: str,
    ordinal: int,
    header: str,
    candidates: int,
) -> RepoForgeError:
    wording = "was not found" if code is ErrorCode.PATCH_CONTEXT_NOT_FOUND else "is ambiguous"
    return _error(
        f"Patch context {wording} for {path} hunk {ordinal}",
        code=code,
        safe_next_action=(
            "Read the exact current file and regenerate the hunk with unique surrounding context, "
            "or use workspace_edit for one exact replacement."
        ),
        details={
            "target_path": path,
            "hunk_ordinal": ordinal,
            "hunk_header": header,
            "candidate_count": candidates,
        },
    )


def _normalized_line(value: str) -> str:
    return " ".join(value.split())


def _candidate_starts(content: list[str], pattern: list[str], *, whitespace: bool) -> list[int]:
    if not pattern:
        return [0]
    candidates: list[int] = []
    comparable = [_normalized_line(item) for item in pattern] if whitespace else pattern
    for index in range(0, len(content) - len(pattern) + 1):
        window = content[index : index + len(pattern)]
        actual = [_normalized_line(item) for item in window] if whitespace else window
        if actual == comparable:
            candidates.append(index)
    return candidates


def _apply_hunks(path: str, original: str, hunks: tuple[_Hunk, ...]) -> tuple[str, set[str]]:
    content = _split_text(original)
    repairs: set[str] = set()
    displacement = 0
    for ordinal, hunk in enumerate(hunks, start=1):
        old_pattern = [line[1:] for line in hunk.lines if line[:1] in {" ", "-"}]
        exact = _candidate_starts(content, old_pattern, whitespace=False)
        whitespace = False
        if len(exact) == 1:
            start = exact[0]
        elif len(exact) > 1:
            raise _context_error(
                code=ErrorCode.PATCH_CONTEXT_AMBIGUOUS,
                path=path,
                ordinal=ordinal,
                header=hunk.header,
                candidates=len(exact),
            )
        else:
            normalized = _candidate_starts(content, old_pattern, whitespace=True)
            if len(normalized) == 1:
                start = normalized[0]
                whitespace = True
                repairs.add("whitespace_normalized_context")
            elif len(normalized) > 1:
                raise _context_error(
                    code=ErrorCode.PATCH_CONTEXT_AMBIGUOUS,
                    path=path,
                    ordinal=ordinal,
                    header=hunk.header,
                    candidates=len(normalized),
                )
            else:
                raise _context_error(
                    code=ErrorCode.PATCH_CONTEXT_NOT_FOUND,
                    path=path,
                    ordinal=ordinal,
                    header=hunk.header,
                    candidates=0,
                )
        expected = max(0, hunk.old_start - 1 + displacement)
        if start != expected:
            repairs.add("relocated_hunks")
        actual_old_count = sum(1 for line in hunk.lines if line[:1] in {" ", "-"})
        actual_new_count = sum(1 for line in hunk.lines if line[:1] in {" ", "+"})
        if actual_old_count != hunk.old_count or actual_new_count != hunk.new_count:
            repairs.add("recounted_hunks")
        matched = content[start : start + len(old_pattern)]
        replacement: list[str] = []
        cursor = 0
        for line in hunk.lines:
            prefix, value = line[:1], line[1:]
            if prefix == " ":
                replacement.append(matched[cursor] if whitespace else value)
                cursor += 1
            elif prefix == "-":
                cursor += 1
            elif prefix == "+":
                replacement.append(value)
            elif prefix == "\\":
                continue
            else:
                raise _error(
                    f"Malformed hunk line in {path}: {line!r}",
                    code=ErrorCode.PATCH_PARSE_FAILED,
                    safe_next_action="Regenerate the patch with standard unified-diff line prefixes.",
                    details={"target_path": path, "hunk_ordinal": ordinal},
                )
        content[start : start + len(old_pattern)] = replacement
        displacement += len(replacement) - len(old_pattern)
    return _join_text(content, trailing_newline=original.endswith("\n") or bool(content)), repairs


def _parse_unified(text: str) -> tuple[_UnifiedFile, ...]:
    lines = text.splitlines()
    files: list[_UnifiedFile] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("diff --git "):
            index += 1
            continue
        parts = lines[index].split()
        if len(parts) != 4:
            raise _error(
                "Malformed diff --git header",
                code=ErrorCode.PATCH_PARSE_FAILED,
                safe_next_action="Regenerate a canonical git-style unified diff.",
                details={"header": lines[index]},
            )
        index += 1
        while index < len(lines) and not lines[index].startswith("--- "):
            index += 1
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise _error(
                "Unified diff is missing ---/+++ file headers",
                code=ErrorCode.PATCH_PARSE_FAILED,
                safe_next_action="Regenerate the patch with complete file headers.",
            )
        old_raw = _normalize_path(lines[index][4:].split("\t", 1)[0])
        new_raw = _normalize_path(lines[index + 1][4:].split("\t", 1)[0])
        old_path = None if old_raw == "/dev/null" else old_raw
        new_path = None if new_raw == "/dev/null" else new_raw
        index += 2
        hunks: list[_Hunk] = []
        while index < len(lines) and not lines[index].startswith("diff --git "):
            if not lines[index].startswith("@@ "):
                index += 1
                continue
            match = _HUNK.match(lines[index])
            if match is None:
                raise _error(
                    "Malformed unified-diff hunk header",
                    code=ErrorCode.PATCH_PARSE_FAILED,
                    safe_next_action="Use @@ -OLD_START,OLD_COUNT +NEW_START,NEW_COUNT @@ syntax.",
                    details={
                        "target_path": new_path or old_path or "",
                        "hunk_header": lines[index],
                    },
                )
            header = lines[index]
            old_start, old_count, new_start, new_count = (
                int(match.group(1)),
                int(match.group(2) or "1"),
                int(match.group(3)),
                int(match.group(4) or "1"),
            )
            index += 1
            body: list[str] = []
            while (
                index < len(lines)
                and not lines[index].startswith("@@ ")
                and not lines[index].startswith("diff --git ")
            ):
                body.append(lines[index] if lines[index] else " ")
                index += 1
            hunks.append(_Hunk(header, old_start, old_count, new_start, new_count, tuple(body)))
        if not hunks:
            raise _error(
                "Unified diff contains no hunks",
                code=ErrorCode.PATCH_PARSE_FAILED,
                safe_next_action="Include at least one @@ hunk for each changed file.",
                details={"target_path": new_path or old_path or ""},
            )
        files.append(_UnifiedFile(old_path, new_path, tuple(hunks)))
    if not files:
        raise _error(
            "Unified diff contains no file sections",
            code=ErrorCode.PATCH_PARSE_FAILED,
            safe_next_action="Provide a git-style unified diff.",
        )
    return tuple(files)


def _normalize_unified(text: str, read_file: ReadFile) -> tuple[str, set[str]]:
    rendered: list[str] = []
    repairs: set[str] = set()
    for item in _parse_unified(text):
        path = item.old_path or item.new_path
        assert path is not None
        old = "" if item.old_path is None else read_file(item.old_path)
        if old is None:
            raise _error(
                f"Patch target does not exist: {item.old_path}",
                code=ErrorCode.PATCH_CONTEXT_NOT_FOUND,
                safe_next_action="Read the current workspace tree and regenerate the patch for an existing path.",
                details={"target_path": item.old_path or ""},
            )
        new, item_repairs = _apply_hunks(path, old, item.hunks)
        repairs.update(item_repairs)
        if item.new_path is None:
            new = ""
        canonical = _canonical_diff(item.old_path, item.new_path, old, new)
        if canonical:
            rendered.append(canonical)
    return "".join(rendered), repairs


def _parse_envelope(text: str, read_file: ReadFile) -> tuple[str, set[str]]:
    lines = text.strip().splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise _error(
            "Malformed OpenAI apply_patch envelope markers",
            code=ErrorCode.PATCH_PARSE_FAILED,
            safe_next_action="Use exactly one *** Begin Patch and one *** End Patch marker.",
        )
    index = 1
    rendered: list[str] = []
    repairs: set[str] = {"converted_openai_envelope"}
    while index < len(lines) - 1:
        directive = lines[index]
        index += 1
        kind: str
        if directive.startswith("*** Add File: "):
            kind = "add"
            path = _normalize_path(directive.removeprefix("*** Add File: "))
        elif directive.startswith("*** Update File: "):
            kind = "update"
            path = _normalize_path(directive.removeprefix("*** Update File: "))
        elif directive.startswith("*** Delete File: "):
            kind = "delete"
            path = _normalize_path(directive.removeprefix("*** Delete File: "))
        else:
            raise _error(
                f"Unknown apply_patch directive: {directive}",
                code=ErrorCode.PATCH_PARSE_FAILED,
                safe_next_action="Use Add File, Update File, Delete File, and optional Move to directives only.",
                details={"directive": directive},
            )
        move_to: str | None = None
        if kind == "update" and index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
            move_to = _normalize_path(lines[index].removeprefix("*** Move to: "))
            index += 1
        body: list[str] = []
        while index < len(lines) - 1 and not lines[index].startswith("*** "):
            body.append(lines[index])
            index += 1
        if kind == "add":
            if any(not line.startswith("+") for line in body):
                raise _error(
                    f"Add File lines must start with +: {path}",
                    code=ErrorCode.PATCH_PARSE_FAILED,
                    safe_next_action="Prefix every added file line with +.",
                    details={"target_path": path},
                )
            new = _join_text([line[1:] for line in body])
            rendered.append(_canonical_diff(None, path, "", new))
            continue
        old = read_file(path)
        if old is None:
            raise _error(
                f"Patch target does not exist: {path}",
                code=ErrorCode.PATCH_CONTEXT_NOT_FOUND,
                safe_next_action="Use Add File for new paths or read the current workspace tree.",
                details={"target_path": path},
            )
        if kind == "delete":
            if body:
                raise _error(
                    f"Delete File directive must not contain hunk lines: {path}",
                    code=ErrorCode.PATCH_PARSE_FAILED,
                    safe_next_action="Use only the Delete File directive for a full-file deletion.",
                    details={"target_path": path},
                )
            rendered.append(_canonical_diff(path, None, old, ""))
            continue
        hunk_lines: list[str] = []
        if not body or not any(line.startswith("@@") for line in body):
            body = ["@@", *body]
        chunk_index = 0
        synthetic: list[_Hunk] = []
        while chunk_index < len(body):
            if not body[chunk_index].startswith("@@"):
                raise _error(
                    f"Update File body must begin with @@: {path}",
                    code=ErrorCode.PATCH_PARSE_FAILED,
                    safe_next_action="Separate update chunks with @@ markers.",
                    details={"target_path": path},
                )
            header = body[chunk_index]
            chunk_index += 1
            hunk_lines = []
            while chunk_index < len(body) and not body[chunk_index].startswith("@@"):
                line = body[chunk_index]
                if line and line[0] not in {" ", "+", "-", "\\"}:
                    line = " " + line
                hunk_lines.append(line)
                chunk_index += 1
            synthetic.append(_Hunk(header, 1, 0, 1, 0, tuple(hunk_lines)))
        new, item_repairs = _apply_hunks(path, old, tuple(synthetic))
        repairs.update(item_repairs)
        if move_to is None:
            rendered.append(_canonical_diff(path, path, old, new))
        else:
            rendered.append(_canonical_diff(path, None, old, ""))
            rendered.append(_canonical_diff(None, move_to, "", new))
    return "".join(rendered), repairs


def normalize_patch(text: str, read_file: ReadFile) -> PatchNormalizationResult:
    inspection = inspect_patch(text)
    if inspection.input_format == "openai_apply_patch":
        normalized, repairs = _parse_envelope(text, read_file)
    else:
        normalized, repairs = _normalize_unified(text, read_file)
    if not normalized.strip():
        raise _error(
            "Patch produces no file changes",
            code=ErrorCode.PATCH_PARSE_FAILED,
            safe_next_action="Refresh the workspace and omit already-applied changes.",
        )
    return PatchNormalizationResult(
        patch=normalized,
        input_format=inspection.input_format,
        input_sha256=_sha256(text),
        normalized_sha256=_sha256(normalized),
        paths=inspection.paths,
        repair_actions=tuple(sorted(repairs)),
    )
