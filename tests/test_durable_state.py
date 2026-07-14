from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from repoforge.adapters.persistence.json_state_repository import JsonStateRepository
from repoforge.domain.durable_state import Revision, SchemaVersion, StateEnvelope
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.testing.fakes import InMemoryLockManager


@dataclass(frozen=True, slots=True)
class DemoRecord:
    name: str


class DemoCodec:
    schema_version = SchemaVersion(1)

    def encode(self, value: DemoRecord) -> dict[str, object]:
        return {"name": value.name}

    def decode(self, payload: dict[str, object]) -> DemoRecord:
        if set(payload) != {"name"} or not isinstance(payload["name"], str):
            raise ValueError("invalid demo record")
        return DemoRecord(payload["name"])


def _store(tmp_path: Path, *, max_record_bytes: int = 4096) -> JsonStateRepository[DemoRecord]:
    return JsonStateRepository(
        tmp_path,
        collection="demo_records",
        locks=InMemoryLockManager(),
        codec=DemoCodec(),
        id_validator=lambda value: (
            value if value.startswith("demo-") else (_ for _ in ()).throw(ValueError("bad id"))
        ),
        max_record_bytes=max_record_bytes,
    )


def test_state_envelope_types_are_positive_and_deterministic() -> None:
    envelope = StateEnvelope("demo-1", SchemaVersion(1), Revision(1), DemoRecord("alpha"))
    assert envelope.schema_version.value == 1
    assert envelope.revision.value == 1
    with pytest.raises(ValueError):
        SchemaVersion(0)
    with pytest.raises(ValueError):
        Revision(0)


def test_json_state_repository_is_private_atomic_restart_safe_and_cas(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    created = store.create("demo-1", DemoRecord("alpha"))
    assert created.revision == Revision(1)
    path = tmp_path / "demo_records" / "demo-1.json"
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert store.read("demo-1") == created
    assert _store(tmp_path).read("demo-1") == created
    assert not list(path.parent.glob("*.tmp-*"))

    saved = store.save("demo-1", DemoRecord("beta"), expected_revision=Revision(1))
    assert saved.revision == Revision(2)
    with pytest.raises(RepoForgeError) as stale:
        store.save("demo-1", DemoRecord("lost"), expected_revision=Revision(1))
    assert stale.value.code is ErrorCode.STATE_STALE
    assert store.read("demo-1") == saved


def test_json_state_repository_rejects_corruption_future_schema_identity_and_size(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.create("demo-1", DemoRecord("alpha"))
    path = tmp_path / "demo_records" / "demo-1.json"

    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(RepoForgeError) as corrupt:
        store.read("demo-1")
    assert corrupt.value.code is ErrorCode.STATE_CORRUPT

    path.write_text(
        json.dumps(
            {
                "record_id": "demo-1",
                "schema_version": 99,
                "revision": 1,
                "payload": {"name": "alpha"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RepoForgeError) as future:
        store.read("demo-1")
    assert future.value.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED

    path.write_text(
        json.dumps(
            {
                "record_id": "demo-2",
                "schema_version": 1,
                "revision": 1,
                "payload": {"name": "alpha"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RepoForgeError) as mismatch:
        store.read("demo-1")
    assert mismatch.value.code is ErrorCode.STATE_CORRUPT

    tiny = _store(tmp_path / "tiny", max_record_bytes=80)
    with pytest.raises(RepoForgeError) as too_large:
        tiny.create("demo-1", DemoRecord("x" * 200))
    assert too_large.value.code is ErrorCode.STATE_TOO_LARGE


def test_json_state_repository_rejects_unsafe_collection_and_record_ids(
    tmp_path: Path,
) -> None:
    with pytest.raises(RepoForgeError):
        JsonStateRepository(
            tmp_path,
            collection="../escape",
            locks=InMemoryLockManager(),
            codec=DemoCodec(),
            id_validator=lambda value: value,
        )
    store = _store(tmp_path)
    with pytest.raises(RepoForgeError):
        store.read("../escape")
