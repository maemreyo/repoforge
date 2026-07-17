"""Shared structured search, tree, diff, and cursor pagination primitives."""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import re
import shlex
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, TypeVar, cast

from ..domain.errors import SecurityError
from ..ports.git import GitSearchLocation

T = TypeVar("T")
_MAX_CURSOR_CHARS = 4096
_UNSAFE_REGEX = (
    re.compile(r"\\[1-9]"),
    re.compile(r"\(\?<"),
    re.compile(r"\([^)]*[+*][^)]*\)[+*{]"),
    re.compile(r"\.\*.*\.\*"),
)
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@"
)


class SearchMode(str, Enum):
    LITERAL = "literal"
    REGEX = "regex"
    FILE_NAME = "file_name"


@dataclass(frozen=True, slots=True)
class StructuredSearchMatch:
    path: str
    line: int | None
    column: int | None
    match: str
    context_before: tuple[str, ...]
    context_after: tuple[str, ...]
    score: float
    provider: str


@dataclass(frozen=True, slots=True)
class StructuredTreeEntry:
    path: str
    kind: str
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class StructuredDiffLine:
    kind: str
    old_line: int | None
    new_line: int | None
    text: str


@dataclass(frozen=True, slots=True)
class StructuredDiffHunk:
    header: str
    lines: tuple[StructuredDiffLine, ...]


@dataclass(frozen=True, slots=True)
class StructuredDiffFile:
    path: str
    status: str
    additions: int
    deletions: int
    hunks: tuple[StructuredDiffHunk, ...]


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[object, ...]
    next_cursor: str | None
    omitted_count: int
    truncated: bool


def _request_binding(kind: str, scope: str, request: object) -> str:
    encoded = json.dumps(
        {"kind": kind, "scope": scope, "request": request},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _encode_cursor(binding: str, index: int) -> str:
    raw = json.dumps(
        {"v": 1, "binding": binding, "index": index},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    envelope = json.dumps(
        {
            "payload": base64.urlsafe_b64encode(raw).decode().rstrip("="),
            "checksum": hashlib.sha256(raw).hexdigest()[:24],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(envelope).decode().rstrip("=")


def _decode_cursor(cursor: str | None, binding: str, total: int) -> int:
    if cursor is None:
        return 0
    if not cursor or len(cursor) > _MAX_CURSOR_CHARS:
        raise ValueError("cursor is malformed")
    try:
        outer = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        envelope = json.loads(outer)
        payload_token = envelope["payload"]
        raw = base64.urlsafe_b64decode(payload_token + "=" * (-len(payload_token) % 4))
        if envelope["checksum"] != hashlib.sha256(raw).hexdigest()[:24]:
            raise ValueError("checksum")
        payload = json.loads(raw)
        if payload["v"] != 1 or payload["binding"] != binding:
            raise ValueError("cursor does not match the exact request or scope")
        index = int(payload["index"])
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("cursor is malformed") from exc
    if not 0 <= index <= total:
        raise ValueError("cursor contains an invalid position")
    return index


def paginate(
    items: Sequence[T],
    *,
    kind: str,
    scope: str,
    request: object,
    max_items: int,
    byte_budget: int,
    cursor: str | None,
) -> Page:
    if not 1 <= max_items <= 2000:
        raise ValueError("max_items must be between 1 and 2000")
    if not 1 <= byte_budget <= 120_000:
        raise ValueError("byte_budget must be between 1 and 120000")
    binding = _request_binding(kind, scope, request)
    start = _decode_cursor(cursor, binding, len(items))
    selected: list[T] = []
    used = 0
    index = start
    while index < len(items) and len(selected) < max_items:
        item = items[index]
        payload = asdict(cast(Any, item)) if hasattr(item, "__dataclass_fields__") else item
        item_bytes = len(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        if selected and used + item_bytes > byte_budget:
            break
        selected.append(item)
        used += item_bytes
        index += 1
        if used >= byte_budget:
            break
    if not selected and start < len(items):
        selected.append(items[start])
        index = start + 1
    next_cursor = _encode_cursor(binding, index) if index < len(items) else None
    return Page(
        items=tuple(selected),
        next_cursor=next_cursor,
        omitted_count=max(0, len(items) - index),
        truncated=next_cursor is not None,
    )


def validate_path_glob(path_glob: str | None) -> None:
    if path_glob is None:
        return
    path = PurePosixPath(path_glob)
    if (
        path_glob.startswith(("/", "-", ":"))
        or ".." in path.parts
        or any(ord(character) < 32 for character in path_glob)
    ):
        raise SecurityError("Unsafe path_glob")


def validate_regex(query: str) -> None:
    if len(query) > 500 or any(pattern.search(query) for pattern in _UNSAFE_REGEX):
        raise SecurityError("unsafe regex pattern rejected by the bounded search guard")
    try:
        re.compile(query)
    except re.error as exc:
        raise ValueError(f"Invalid regex: {exc}") from exc


def search_files(
    paths: Iterable[str],
    *,
    load_text: Callable[[str], str | None],
    query: str,
    mode: SearchMode,
    path_glob: str | None,
    context_lines: int,
    deadline_ms: float = 500.0,
) -> tuple[StructuredSearchMatch, ...]:
    if not query or "\x00" in query:
        raise ValueError("query must be non-empty and cannot contain NUL")
    if not 0 <= context_lines <= 5:
        raise ValueError("context_lines must be between 0 and 5")
    validate_path_glob(path_glob)
    if mode is SearchMode.REGEX:
        validate_regex(query)
    matcher = re.compile(query) if mode is SearchMode.REGEX else None
    deadline = time.monotonic() + deadline_ms / 1000
    matches: list[StructuredSearchMatch] = []
    for path in sorted(paths):
        if path_glob is not None and not PurePosixPath(path).match(path_glob):
            continue
        if time.monotonic() > deadline:
            raise SecurityError("regex/search timeout exceeded the reviewed deadline")
        if mode is SearchMode.FILE_NAME:
            if query.casefold() in path.casefold():
                matches.append(
                    StructuredSearchMatch(
                        path,
                        None,
                        None,
                        path,
                        (),
                        (),
                        1.0,
                        "builtin_file_name",
                    )
                )
            continue
        text = load_text(path)
        if text is None:
            continue
        lines = text.splitlines()
        for line_index, line in enumerate(lines):
            if matcher is None:
                starts: list[tuple[int, str]] = []
                offset = 0
                while True:
                    found = line.find(query, offset)
                    if found < 0:
                        break
                    starts.append((found, query))
                    offset = found + max(1, len(query))
            else:
                starts = [(match.start(), match.group(0)) for match in matcher.finditer(line)]
            for column, matched in starts:
                if not matched:
                    continue
                matches.append(
                    StructuredSearchMatch(
                        path=path,
                        line=line_index + 1,
                        column=column + 1,
                        match=matched[:4000],
                        context_before=tuple(
                            lines[max(0, line_index - context_lines) : line_index]
                        ),
                        context_after=tuple(
                            lines[line_index + 1 : min(len(lines), line_index + 1 + context_lines)]
                        ),
                        score=1.0 if matcher is None else 0.9,
                        provider="builtin_literal" if matcher is None else "builtin_regex",
                    )
                )
    return tuple(matches)


def structured_regex_matches(
    locations: Iterable[GitSearchLocation],
    *,
    load_text: Callable[[str], str | None],
    context_lines: int,
) -> tuple[StructuredSearchMatch, ...]:
    if not 0 <= context_lines <= 5:
        raise ValueError("context_lines must be between 0 and 5")
    cache: dict[str, list[str] | None] = {}
    matches: list[StructuredSearchMatch] = []
    for location in locations:
        if location.path not in cache:
            text = load_text(location.path)
            cache[location.path] = None if text is None else text.splitlines()
        lines = cache[location.path]
        before: tuple[str, ...] = ()
        after: tuple[str, ...] = ()
        if lines is not None and 1 <= location.line <= len(lines):
            index = location.line - 1
            before = tuple(line[:4000] for line in lines[max(0, index - context_lines) : index])
            after = tuple(
                line[:4000]
                for line in lines[index + 1 : min(len(lines), index + 1 + context_lines)]
            )
        matches.append(
            StructuredSearchMatch(
                path=location.path,
                line=location.line,
                column=location.column,
                match=location.match[:4000],
                context_before=before,
                context_after=after,
                score=0.9,
                provider="git_grep_regex",
            )
        )
    return tuple(matches)


def tree_entries(
    paths: Iterable[str],
    *,
    subtree: str | None,
    size_of: Callable[[str], int | None],
) -> tuple[StructuredTreeEntry, ...]:
    normalized_subtree = None
    if subtree is not None:
        normalized_subtree = subtree.replace("\\", "/").strip("/")
        if not normalized_subtree or ".." in PurePosixPath(normalized_subtree).parts:
            raise SecurityError("Unsafe subtree")
    visible_files = []
    directories: set[str] = set()
    for path in sorted(set(paths)):
        if normalized_subtree is not None and not path.startswith(f"{normalized_subtree}/"):
            continue
        visible_files.append(path)
        parts = PurePosixPath(path).parts
        for index in range(1, len(parts)):
            directory = PurePosixPath(*parts[:index]).as_posix()
            if normalized_subtree is not None and directory == normalized_subtree:
                continue
            if normalized_subtree is None or directory.startswith(f"{normalized_subtree}/"):
                directories.add(directory)
    entries = [StructuredTreeEntry(path, "directory", None) for path in directories]
    entries.extend(StructuredTreeEntry(path, "file", size_of(path)) for path in visible_files)
    return tuple(sorted(entries, key=lambda item: (item.path, item.kind)))


def build_diff_file(
    path: str,
    before: bytes | None,
    after: bytes | None,
) -> StructuredDiffFile | None:
    if before == after:
        return None
    if before is None:
        status = "added"
    elif after is None:
        status = "deleted"
    else:
        status = "modified"
    if (before is not None and b"\x00" in before) or (after is not None and b"\x00" in after):
        return StructuredDiffFile(path, status, 0, 0, ())
    try:
        before_text = "" if before is None else before.decode("utf-8")
        after_text = "" if after is None else after.decode("utf-8")
    except UnicodeDecodeError:
        return StructuredDiffFile(path, status, 0, 0, ())
    unified = list(
        difflib.unified_diff(
            before_text.splitlines(),
            after_text.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    hunks: list[StructuredDiffHunk] = []
    current_header: str | None = None
    current_lines: list[StructuredDiffLine] = []
    old_line = 0
    new_line = 0
    additions = 0
    deletions = 0
    for line in unified[2:]:
        header = _HUNK_HEADER.match(line)
        if header is not None:
            if current_header is not None:
                hunks.append(StructuredDiffHunk(current_header, tuple(current_lines)))
            current_header = line
            current_lines = []
            old_line = int(header.group("old"))
            new_line = int(header.group("new"))
            continue
        if current_header is None or not line:
            continue
        marker = line[0]
        text = line[1:]
        if marker == "+":
            additions += 1
            current_lines.append(StructuredDiffLine("add", None, new_line, text[:10_000]))
            new_line += 1
        elif marker == "-":
            deletions += 1
            current_lines.append(StructuredDiffLine("delete", old_line, None, text[:10_000]))
            old_line += 1
        elif marker == " ":
            current_lines.append(StructuredDiffLine("context", old_line, new_line, text[:10_000]))
            old_line += 1
            new_line += 1
    if current_header is not None:
        hunks.append(StructuredDiffHunk(current_header, tuple(current_lines)))
    return StructuredDiffFile(path, status, additions, deletions, tuple(hunks))


def parse_unified_diff(text: str) -> tuple[StructuredDiffFile, ...]:
    """Parse bounded Git unified diff output without reconstructing or renumbering hunks."""

    def clean_path(raw: str) -> str:
        if raw in {"/dev/null", "dev/null"}:
            return raw
        return raw[2:] if raw.startswith(("a/", "b/")) else raw

    files: list[StructuredDiffFile] = []
    chunks = re.split(r"(?=^diff --git )", text, flags=re.MULTILINE)
    for chunk in chunks:
        lines = chunk.splitlines()
        if not lines or not lines[0].startswith("diff --git "):
            continue
        try:
            header_parts = shlex.split(lines[0])
        except ValueError as exc:
            raise ValueError(f"Invalid unified diff file header: {exc}") from exc
        if len(header_parts) != 4:
            raise ValueError("Invalid unified diff file header")
        path = clean_path(header_parts[3])
        status = "modified"
        for line in lines[1:]:
            if line.startswith("new file mode ") or line == "--- /dev/null":
                status = "added"
            elif line.startswith("deleted file mode ") or line == "+++ /dev/null":
                status = "deleted"
            elif line.startswith("rename to "):
                status = "renamed"
                try:
                    renamed = shlex.split(line.removeprefix("rename to "))
                except ValueError as exc:
                    raise ValueError(f"Invalid unified diff rename path: {exc}") from exc
                if len(renamed) != 1:
                    raise ValueError("Invalid unified diff rename path")
                path = clean_path(renamed[0])
            elif line.startswith("+++ ") and line != "+++ /dev/null":
                candidate = line.removeprefix("+++ ")
                try:
                    parsed = shlex.split(candidate)
                except ValueError:
                    parsed = []
                if len(parsed) == 1:
                    path = clean_path(parsed[0])

        hunks: list[StructuredDiffHunk] = []
        current_header: str | None = None
        current_lines: list[StructuredDiffLine] = []
        old_line = 0
        new_line = 0
        additions = 0
        deletions = 0
        for line in lines[1:]:
            hunk_header = _HUNK_HEADER.match(line)
            if hunk_header is not None:
                if current_header is not None:
                    hunks.append(StructuredDiffHunk(current_header, tuple(current_lines)))
                current_header = line[:500]
                current_lines = []
                old_line = int(hunk_header.group("old"))
                new_line = int(hunk_header.group("new"))
                continue
            if current_header is None or not line or line.startswith("\\ No newline"):
                continue
            marker = line[0]
            content = line[1:][:10_000]
            if marker == "+":
                additions += 1
                current_lines.append(StructuredDiffLine("add", None, new_line, content))
                new_line += 1
            elif marker == "-":
                deletions += 1
                current_lines.append(StructuredDiffLine("delete", old_line, None, content))
                old_line += 1
            elif marker == " ":
                current_lines.append(StructuredDiffLine("context", old_line, new_line, content))
                old_line += 1
                new_line += 1
        if current_header is not None:
            hunks.append(StructuredDiffHunk(current_header, tuple(current_lines)))
        files.append(
            StructuredDiffFile(
                path=path,
                status=status,
                additions=additions,
                deletions=deletions,
                hunks=tuple(hunks),
            )
        )
    return tuple(files)
