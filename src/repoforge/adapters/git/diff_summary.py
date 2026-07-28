"""Pure parsers for bounded, hunk-free Git diff summaries."""

from __future__ import annotations

from typing import Literal

from ...domain.errors import CommandError, SecurityError
from ...domain.policy import normalize_relative_path
from ...ports.git import GitDiffSummary

DiffStatus = Literal["added", "modified", "deleted", "renamed"]


def _fields_z(raw: bytes, *, label: str) -> list[bytes]:
    if not raw:
        return []
    if not raw.endswith(b"\x00"):
        raise CommandError(f"Git returned malformed {label} output")
    return raw.split(b"\x00")[:-1]


def _path(raw: bytes, *, label: str) -> str:
    try:
        return normalize_relative_path(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, SecurityError) as exc:
        raise CommandError(f"Git returned an invalid path in {label} output") from exc


def _parse_name_status_z(raw: bytes) -> dict[str, DiffStatus]:
    fields = _fields_z(raw, label="name-status")
    statuses: dict[str, DiffStatus] = {}
    index = 0
    while index < len(fields):
        try:
            code = fields[index].decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise CommandError("Git returned a malformed name-status code") from exc
        index += 1
        kind = code[:1]
        if kind in {"R", "C"}:
            if len(code) < 2 or not code[1:].isdigit() or index + 1 >= len(fields):
                raise CommandError("Git returned a malformed rename/copy record")
            index += 1
            destination = _path(fields[index], label="name-status")
            index += 1
            status: DiffStatus = "renamed" if kind == "R" else "added"
        else:
            if code not in {"A", "D", "M", "T"} or index >= len(fields):
                raise CommandError("Git returned a malformed name-status record")
            destination = _path(fields[index], label="name-status")
            index += 1
            status_by_code: dict[str, DiffStatus] = {
                "A": "added",
                "D": "deleted",
                "M": "modified",
                "T": "modified",
            }
            status = status_by_code[code]
        if destination in statuses:
            raise CommandError("Git returned duplicate name-status paths")
        statuses[destination] = status
    return statuses


def _counts(added: bytes, deleted: bytes) -> tuple[int, int]:
    if added == deleted == b"-":
        return 0, 0
    if added == b"-" or deleted == b"-":
        raise CommandError("Git returned inconsistent binary numstat counts")
    try:
        additions = int(added)
        deletions = int(deleted)
    except ValueError as exc:
        raise CommandError("Git returned non-numeric numstat counts") from exc
    if additions < 0 or deletions < 0:
        raise CommandError("Git returned negative numstat counts")
    return additions, deletions


def _parse_numstat_z(raw: bytes) -> dict[str, tuple[int, int]]:
    fields = _fields_z(raw, label="numstat")
    counts: dict[str, tuple[int, int]] = {}
    index = 0
    while index < len(fields):
        parts = fields[index].split(b"\t", 2)
        index += 1
        if len(parts) != 3:
            raise CommandError("Git returned a malformed numstat record")
        additions, deletions = _counts(parts[0], parts[1])
        if parts[2]:
            destination = _path(parts[2], label="numstat")
        else:
            if index + 1 >= len(fields):
                raise CommandError("Git returned a malformed rename/copy numstat record")
            index += 1
            destination = _path(fields[index], label="numstat")
            index += 1
        if destination in counts:
            raise CommandError("Git returned duplicate numstat paths")
        counts[destination] = (additions, deletions)
    return counts


def parse_diff_summary(
    name_status: bytes,
    numstat: bytes,
) -> tuple[GitDiffSummary, ...]:
    """Correlate Git status and counts by normalized destination path."""

    statuses = _parse_name_status_z(name_status)
    counts = _parse_numstat_z(numstat)
    unexpected_counts = counts.keys() - statuses.keys()
    if unexpected_counts:
        raise CommandError("Git numstat output contains paths absent from name-status")
    return tuple(
        GitDiffSummary(path, status, *counts.get(path, (0, 0)))
        for path, status in sorted(statuses.items())
    )
