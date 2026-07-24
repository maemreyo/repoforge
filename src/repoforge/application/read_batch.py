"""Shared bounded, resumable batch-reading engine for repository and workspace reads."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from ..domain.errors import ErrorCode, RepoForgeError, WorkspaceError

_MAX_FILES = 20
_MAX_LINES_PER_REQUEST = 2001
_MAX_CURSOR_CHARS = 4096


@dataclass(frozen=True, slots=True)
class FileReadRequest:
    path: str
    start_line: int = 1
    end_line: int = 500


@dataclass(frozen=True, slots=True)
class LoadedTextFile:
    path: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ReadFileResult:
    path: str
    sha256: str
    size_bytes: int
    total_lines: int
    start_line: int
    end_line: int
    content: str
    truncated: bool
    omitted_line_range: tuple[int, int] | None
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ReadFileError:
    path: str
    error_code: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class BatchReadResult:
    files: tuple[ReadFileResult, ...]
    errors: tuple[ReadFileError, ...]
    requested: int
    succeeded: int
    truncated: bool
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class _CursorState:
    file_index: int
    char_offset: int


Loader = Callable[[str], LoadedTextFile]


def validate_requests(requests: tuple[FileReadRequest, ...]) -> None:
    if not requests or len(requests) > _MAX_FILES:
        raise WorkspaceError(f"files must contain between 1 and {_MAX_FILES} entries")
    seen: set[str] = set()
    for index, request in enumerate(requests):
        if not request.path:
            raise WorkspaceError(f"files[{index}].path must be non-empty")
        if request.path in seen:
            raise WorkspaceError(f"duplicate path in files: {request.path}")
        seen.add(request.path)
        if request.start_line < 1:
            raise WorkspaceError(f"files[{index}].start_line must be at least 1")
        if request.end_line < request.start_line:
            raise WorkspaceError(f"files[{index}].end_line must be >= start_line")
        if request.end_line - request.start_line + 1 > _MAX_LINES_PER_REQUEST:
            raise WorkspaceError(
                f"files[{index}] requests more than {_MAX_LINES_PER_REQUEST} lines"
            )


def _binding(kind: str, scope: str, requests: tuple[FileReadRequest, ...]) -> str:
    payload = {
        "kind": kind,
        "scope": scope,
        "files": [
            {"path": item.path, "start_line": item.start_line, "end_line": item.end_line}
            for item in requests
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _encode_cursor(binding: str, state: _CursorState) -> str:
    payload = {
        "v": 1,
        "binding": binding,
        "file_index": state.file_index,
        "char_offset": state.char_offset,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    envelope = {
        "payload": base64.urlsafe_b64encode(raw).decode().rstrip("="),
        "checksum": hashlib.sha256(raw).hexdigest()[:24],
    }
    token = (
        base64.urlsafe_b64encode(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    if len(token) > _MAX_CURSOR_CHARS:
        raise WorkspaceError("Read cursor exceeds its reviewed bound")
    return token


def _decode_cursor(token: str | None, binding: str, file_count: int) -> _CursorState:
    if token is None:
        return _CursorState(0, 0)
    if not token or len(token) > _MAX_CURSOR_CHARS:
        raise WorkspaceError("Read cursor is malformed or outside its reviewed bound")
    try:
        padded = token + "=" * (-len(token) % 4)
        envelope = json.loads(base64.urlsafe_b64decode(padded).decode())
        payload_token = envelope["payload"]
        payload_padded = payload_token + "=" * (-len(payload_token) % 4)
        raw = base64.urlsafe_b64decode(payload_padded)
        if envelope["checksum"] != hashlib.sha256(raw).hexdigest()[:24]:
            raise ValueError("checksum")
        payload = json.loads(raw)
        if payload["v"] != 1 or payload["binding"] != binding:
            raise WorkspaceError("Read cursor does not match the exact request or snapshot")
        state = _CursorState(int(payload["file_index"]), int(payload["char_offset"]))
    except WorkspaceError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WorkspaceError("Read cursor is malformed") from exc
    if not 0 <= state.file_index <= file_count or state.char_offset < 0:
        raise WorkspaceError("Read cursor contains an invalid position")
    return state


def _render(data: bytes, request: FileReadRequest) -> tuple[str, int, list[tuple[int, int, int]]]:
    if b"\x00" in data:
        raise RepoForgeError(
            "Binary files are not supported by this tool",
            code=ErrorCode.SECURITY_POLICY_VIOLATION,
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepoForgeError(
            "File is not valid UTF-8",
            code=ErrorCode.SECURITY_POLICY_VIOLATION,
        ) from exc
    lines = text.splitlines()
    actual_end = min(request.end_line, len(lines))
    selected = lines[request.start_line - 1 : actual_end]
    segments: list[str] = []
    spans: list[tuple[int, int, int]] = []
    cursor = 0
    for offset, line in enumerate(selected):
        line_number = request.start_line + offset
        segment = f"{line_number}: {line}"
        if offset + 1 < len(selected):
            segment += "\n"
        segments.append(segment)
        spans.append((cursor, cursor + len(segment), line_number))
        cursor += len(segment)
    return "".join(segments), len(lines), spans


def _serialized_content_bytes(text: str) -> int:
    """Measure the JSON-escaped bytes emitted for a string value, excluding its quotes."""

    encoded = json.dumps(text, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return max(0, len(encoded) - 2)


def _prefix_within_transport_budget(text: str, budget: int) -> str:
    if not text or budget <= 0:
        return ""
    if _serialized_content_bytes(text) <= budget:
        return text
    low = 1
    high = len(text)
    best = ""
    while low <= high:
        middle = (low + high) // 2
        candidate = text[:middle]
        if _serialized_content_bytes(candidate) <= budget:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _line_bounds(
    spans: list[tuple[int, int, int]],
    start_offset: int,
    end_offset: int,
    request: FileReadRequest,
    truncated: bool,
) -> tuple[int, int, tuple[int, int] | None]:
    touched = [number for start, end, number in spans if end > start_offset and start < end_offset]
    if touched:
        start_line = touched[0]
        end_line = touched[-1]
    else:
        start_line = request.start_line
        end_line = request.start_line - 1
    omitted: tuple[int, int] | None = None
    if truncated:
        next_lines = [number for start, end, number in spans if end > end_offset]
        if next_lines:
            omitted = (next_lines[0], spans[-1][2])
    return start_line, end_line, omitted


def execute_batch_read(
    *,
    kind: str,
    scope: str,
    requests: tuple[FileReadRequest, ...],
    loader: Loader,
    byte_budget: int,
    cursor: str | None,
) -> BatchReadResult:
    """Read sequentially under one global UTF-8 byte budget with exact resume cursors."""

    validate_requests(requests)
    if not 1 <= byte_budget <= 120_000:
        raise WorkspaceError("byte_budget must be between 1 and 120000")
    binding = _binding(kind, scope, requests)
    position = _decode_cursor(cursor, binding, len(requests))
    remaining = byte_budget
    files: list[ReadFileResult] = []
    errors: list[ReadFileError] = []
    index = position.file_index
    char_offset = position.char_offset
    next_cursor: str | None = None

    while index < len(requests):
        request = requests[index]
        try:
            loaded = loader(request.path)
            rendered, total_lines, spans = _render(loaded.data, request)
        except (RepoForgeError, ValueError) as exc:
            code = (
                exc.code.value
                if isinstance(exc, RepoForgeError)
                else ErrorCode.INPUT_REQUIRED.value
            )
            errors.append(ReadFileError(request.path, code, type(exc).__name__, str(exc)))
            index += 1
            char_offset = 0
            continue
        if char_offset > len(rendered):
            raise WorkspaceError("Read cursor points beyond the requested content")
        pending = rendered[char_offset:]
        chunk = _prefix_within_transport_budget(pending, remaining)
        if pending and not chunk:
            required_bytes = _serialized_content_bytes(pending[:1])
            raise RepoForgeError(
                "RESULT_TRANSPORT_BUDGET_EXCEEDED: one source character cannot fit the "
                "remaining serialized page budget",
                code=ErrorCode.RESULT_TRANSPORT_BUDGET_EXCEEDED,
                retryable=False,
                details={
                    "byte_budget": byte_budget,
                    "remaining_bytes": remaining,
                    "required_bytes": required_bytes,
                    "path": request.path,
                },
                safe_next_action=(
                    "Retry the same exact read with a larger byte_budget; no result page was emitted."
                ),
                unchanged_state=("Repository and workspace content were not modified.",),
            )
        end_offset = char_offset + len(chunk)
        truncated_file = end_offset < len(rendered)
        if truncated_file:
            next_cursor = _encode_cursor(binding, _CursorState(index, end_offset))
        elif index + 1 < len(requests):
            next_cursor = _encode_cursor(binding, _CursorState(index + 1, 0))
        else:
            next_cursor = None
        start_line, end_line, omitted = _line_bounds(
            spans,
            char_offset,
            end_offset,
            request,
            truncated_file,
        )
        files.append(
            ReadFileResult(
                path=loaded.path,
                sha256=hashlib.sha256(loaded.data).hexdigest(),
                size_bytes=len(loaded.data),
                total_lines=total_lines,
                start_line=start_line,
                end_line=end_line,
                content=chunk,
                truncated=truncated_file,
                omitted_line_range=omitted,
                next_cursor=next_cursor if truncated_file else None,
            )
        )
        remaining -= _serialized_content_bytes(chunk)
        if truncated_file or remaining <= 0:
            break
        index += 1
        char_offset = 0

    return BatchReadResult(
        files=tuple(files),
        errors=tuple(errors),
        requested=len(requests),
        succeeded=len(files),
        truncated=next_cursor is not None,
        next_cursor=next_cursor,
    )
