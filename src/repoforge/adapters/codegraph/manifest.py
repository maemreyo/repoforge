"""Snapshot-bound manifest for one managed CodeGraph source projection."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ...domain.code_intelligence import CodeIntelligenceSnapshot
from ...domain.policy import normalize_relative_path

_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_LIMITATIONS = 32


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def _safe_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} has an invalid format")
    return value


def _json_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"projection manifest {field_name} must be an integer")
    return value


def _json_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"projection manifest {field_name} must be a boolean")
    return value


def _json_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"projection manifest {field_name} must be text")
    return value


@dataclass(frozen=True, slots=True, order=True)
class ProjectionEntry:
    path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_relative_path(self.path))
        object.__setattr__(self, "sha256", _sha256(self.sha256, "entry sha256"))
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool):
            raise ValueError("entry size_bytes must be a non-negative integer")
        if self.size_bytes < 0:
            raise ValueError("entry size_bytes must be a non-negative integer")

    def as_data(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size_bytes": self.size_bytes}


@dataclass(frozen=True, slots=True)
class ProjectionManifest:
    snapshot_id: str
    repo_id: str
    workspace_id: str
    head_sha: str
    workspace_fingerprint: str
    options_digest: str
    selection_digest: str
    entries: tuple[ProjectionEntry, ...]
    total_files: int
    total_bytes: int
    limitations: tuple[str, ...] = ()
    truncated: bool = False
    schema_version: int = _SCHEMA_VERSION
    manifest_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError(f"unsupported projection manifest schema: {self.schema_version}")
        object.__setattr__(self, "snapshot_id", _safe_id(self.snapshot_id, "snapshot_id"))
        object.__setattr__(self, "repo_id", _safe_id(self.repo_id, "repo_id"))
        object.__setattr__(self, "workspace_id", _safe_id(self.workspace_id, "workspace_id"))
        if not isinstance(self.head_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", self.head_sha):
            raise ValueError("head_sha must be a lowercase 40-character Git SHA")
        object.__setattr__(
            self,
            "workspace_fingerprint",
            _sha256(self.workspace_fingerprint, "workspace_fingerprint"),
        )
        object.__setattr__(self, "options_digest", _sha256(self.options_digest, "options_digest"))
        object.__setattr__(
            self,
            "selection_digest",
            _sha256(self.selection_digest, "selection_digest"),
        )
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, ProjectionEntry) for entry in self.entries
        ):
            raise ValueError("entries must be an immutable ProjectionEntry tuple")
        normalized_entries = tuple(sorted(set(self.entries)))
        object.__setattr__(self, "entries", normalized_entries)
        expected_files = len(normalized_entries)
        expected_bytes = sum(entry.size_bytes for entry in normalized_entries)
        if not isinstance(self.total_files, int) or isinstance(self.total_files, bool):
            raise ValueError("projection total_files must be a non-negative integer")
        if not isinstance(self.total_bytes, int) or isinstance(self.total_bytes, bool):
            raise ValueError("projection total_bytes must be a non-negative integer")
        if self.total_files < 0 or self.total_bytes < 0:
            raise ValueError("projection totals must be non-negative integers")
        if self.total_files != expected_files or self.total_bytes != expected_bytes:
            raise ValueError("projection totals must match manifest entries")
        if not isinstance(self.limitations, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.limitations
        ):
            raise ValueError("limitations must be an immutable non-empty text tuple")
        normalized_limitations = tuple(sorted({item.strip() for item in self.limitations}))
        if len(normalized_limitations) > _MAX_LIMITATIONS:
            raise ValueError(f"projection limitations exceed {_MAX_LIMITATIONS} items")
        object.__setattr__(self, "limitations", normalized_limitations)
        if not isinstance(self.truncated, bool):
            raise ValueError("truncated must be a boolean")
        object.__setattr__(
            self, "manifest_digest", hashlib.sha256(_canonical(self._data())).hexdigest()
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: CodeIntelligenceSnapshot,
        *,
        options_digest: str,
        selection_digest: str,
        entries: tuple[ProjectionEntry, ...],
        limitations: tuple[str, ...] = (),
        truncated: bool = False,
    ) -> ProjectionManifest:
        return cls(
            snapshot_id=snapshot.snapshot_id,
            repo_id=snapshot.repo_id,
            workspace_id=snapshot.workspace_id,
            head_sha=snapshot.head_sha,
            workspace_fingerprint=snapshot.workspace_fingerprint,
            options_digest=options_digest,
            selection_digest=selection_digest,
            entries=entries,
            total_files=len(entries),
            total_bytes=sum(entry.size_bytes for entry in entries),
            limitations=limitations,
            truncated=truncated,
        )

    def _data(self) -> dict[str, object]:
        return {
            "entries": [entry.as_data() for entry in self.entries],
            "head_sha": self.head_sha,
            "limitations": list(self.limitations),
            "options_digest": self.options_digest,
            "repo_id": self.repo_id,
            "schema_version": self.schema_version,
            "selection_digest": self.selection_digest,
            "snapshot_id": self.snapshot_id,
            "total_bytes": self.total_bytes,
            "total_files": self.total_files,
            "truncated": self.truncated,
            "workspace_fingerprint": self.workspace_fingerprint,
            "workspace_id": self.workspace_id,
        }

    def as_data(self) -> dict[str, object]:
        return {**self._data(), "manifest_digest": self.manifest_digest}

    def to_json(self) -> str:
        return json.dumps(self.as_data(), sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> ProjectionManifest:
        def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate projection manifest key: {key}")
                result[key] = value
            return result

        try:
            raw = json.loads(text, object_pairs_hook=no_duplicates)
        except json.JSONDecodeError as exc:
            raise ValueError("projection manifest is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError("projection manifest must be a JSON object")
        digest = raw.pop("manifest_digest", None)
        calculated_digest = hashlib.sha256(_canonical(raw)).hexdigest()
        if not isinstance(digest, str) or digest != calculated_digest:
            raise ValueError("projection manifest digest does not match its payload")
        expected_fields = {
            "entries",
            "head_sha",
            "limitations",
            "options_digest",
            "repo_id",
            "schema_version",
            "selection_digest",
            "snapshot_id",
            "total_bytes",
            "total_files",
            "truncated",
            "workspace_fingerprint",
            "workspace_id",
        }
        unsupported = sorted(set(raw) - expected_fields)
        if unsupported:
            raise ValueError(f"projection manifest contains unsupported fields: {unsupported}")
        missing = sorted(expected_fields - set(raw))
        if missing:
            raise ValueError(f"projection manifest is missing required fields: {missing}")
        entries_raw = raw.get("entries")
        if not isinstance(entries_raw, list):
            raise ValueError("projection manifest entries must be a list")
        entries: list[ProjectionEntry] = []
        for item in entries_raw:
            if not isinstance(item, dict):
                raise ValueError("projection manifest entry must be an object")
            unsupported_entry = sorted(set(item) - {"path", "sha256", "size_bytes"})
            if unsupported_entry:
                raise ValueError(
                    f"projection manifest entry contains unsupported fields: {unsupported_entry}"
                )
            entries.append(
                ProjectionEntry(
                    path=_json_text(item.get("path"), "entry path"),
                    sha256=_json_text(item.get("sha256"), "entry sha256"),
                    size_bytes=_json_int(item.get("size_bytes"), "entry size_bytes"),
                )
            )
        limitations_raw = raw.get("limitations")
        if not isinstance(limitations_raw, list) or not all(
            isinstance(item, str) for item in limitations_raw
        ):
            raise ValueError("projection manifest limitations must be a text list")
        manifest = cls(
            snapshot_id=_json_text(raw.get("snapshot_id"), "snapshot_id"),
            repo_id=_json_text(raw.get("repo_id"), "repo_id"),
            workspace_id=_json_text(raw.get("workspace_id"), "workspace_id"),
            head_sha=_json_text(raw.get("head_sha"), "head_sha"),
            workspace_fingerprint=_json_text(
                raw.get("workspace_fingerprint"), "workspace_fingerprint"
            ),
            options_digest=_json_text(raw.get("options_digest"), "options_digest"),
            selection_digest=_json_text(raw.get("selection_digest"), "selection_digest"),
            entries=tuple(entries),
            total_files=_json_int(raw.get("total_files"), "total_files"),
            total_bytes=_json_int(raw.get("total_bytes"), "total_bytes"),
            limitations=tuple(item for item in limitations_raw if isinstance(item, str)),
            truncated=_json_bool(raw.get("truncated"), "truncated"),
            schema_version=_json_int(raw.get("schema_version"), "schema_version"),
        )
        return manifest


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    source_root: Path
    manifest: ProjectionManifest


__all__ = ["ProjectionEntry", "ProjectionManifest", "ProjectionResult"]
