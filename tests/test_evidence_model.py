from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from repoforge.adapters.persistence.json_evidence_store import JsonEvidenceStore
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceArtifactRef,
    EvidenceMeasure,
    EvidenceProvenance,
    EvidenceQuery,
    EvidenceScope,
    EvidenceSnapshot,
    EvidenceSourceKind,
    EvidenceStatus,
    evidence_status_for,
    mark_evidence_conflicts,
    new_evidence_item,
)
from repoforge.testing.fakes import InMemoryLockManager


def _snapshot(
    *,
    snapshot_id: str = "snapshot-main",
    head_sha: str = "a" * 40,
    workspace_id: str | None = "workspace-1",
    workspace_fingerprint: str | None = "b" * 64,
) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        snapshot_id=snapshot_id,
        repo_id="demo",
        workspace_id=workspace_id,
        head_sha=head_sha,
        workspace_fingerprint=workspace_fingerprint,
        config_generation=7,
        policy_hash="c" * 64,
    )


def _item(
    *,
    source_kind: EvidenceSourceKind = EvidenceSourceKind.CODE_INTELLIGENCE,
    provider_id: str = "tree-sitter",
    provider_version: str = "1.2.3",
    snapshot: EvidenceSnapshot | None = None,
    summary: str = "Symbol graph covers the changed service and its tests.",
    paths: tuple[str, ...] = ("src/service.py", "tests/test_service.py"),
    symbols: tuple[str, ...] = ("CodingService", "test_service"),
    tests: tuple[str, ...] = ("tests/test_service.py::test_service",),
    status: EvidenceStatus = EvidenceStatus.CURRENT,
    conflict_group: str | None = "symbol-graph:service",
    created_at: str = "2026-07-15T00:00:00+00:00",
    expires_at: str | None = "2026-07-16T00:00:00+00:00",
    artifact: bytes | None = None,
):
    artifact_ref = (
        EvidenceArtifactRef(
            digest=hashlib.sha256(artifact).hexdigest(),
            media_type="application/json",
            size_bytes=len(artifact),
            required=True,
        )
        if artifact is not None
        else None
    )
    return new_evidence_item(
        source_kind=source_kind,
        provider_id=provider_id,
        provider_version=provider_version,
        provenance=EvidenceProvenance(
            source_reference="provider-run:42",
            provider_run_id="run-42",
            artifact=artifact_ref,
        ),
        scope=EvidenceScope(
            paths=paths,
            symbols=symbols,
            flows=("request-to-commit",),
            tests=tests,
        ),
        snapshot=snapshot or _snapshot(),
        summary=summary,
        coverage=EvidenceMeasure(85, "Most changed symbols were indexed."),
        confidence=EvidenceMeasure(92, "Provider completed without parser errors."),
        status=status,
        conflict_group=conflict_group,
        created_at=created_at,
        expires_at=expires_at,
    )


def test_evidence_identity_is_deterministic_normalized_and_secret_safe() -> None:
    first = _item(summary="token=super-secret Symbol graph is complete")
    second = _item(
        summary="token=super-secret Symbol graph is complete",
        paths=("tests/test_service.py", "src/service.py", "src/service.py"),
        symbols=("test_service", "CodingService", "CodingService"),
        tests=(
            "tests/test_service.py::test_service",
            "tests/test_service.py::test_service",
        ),
    )

    assert first == second
    assert first.evidence_id.startswith("ev-")
    assert len(first.content_digest) == 64
    assert "super-secret" not in first.summary
    assert first.scope.paths == ("src/service.py", "tests/test_service.py")
    assert first.scope.symbols == ("CodingService", "test_service")
    assert first.coverage.value == 85
    assert first.confidence.value == 92
    assert first.schema_version == EVIDENCE_SCHEMA_VERSION == 1

    distinct = {
        _item(status=EvidenceStatus.CURRENT).status,
        _item(status=EvidenceStatus.STALE).status,
        _item(status=EvidenceStatus.PARTIAL).status,
        _item(status=EvidenceStatus.CONFLICTING).status,
        _item(status=EvidenceStatus.UNAVAILABLE).status,
    }
    assert distinct == set(EvidenceStatus)

    with pytest.raises(RepoForgeError) as invalid_measure:
        EvidenceMeasure(101, "Impossible coverage")
    assert invalid_measure.value.code is ErrorCode.EVIDENCE_INVALID


def test_staleness_and_conflicts_are_derived_from_exact_snapshot_identity() -> None:
    current = _item()
    assert (
        evidence_status_for(
            current,
            current_snapshot=_snapshot(),
            now="2026-07-15T12:00:00+00:00",
        )
        is EvidenceStatus.CURRENT
    )
    assert (
        evidence_status_for(
            current,
            current_snapshot=_snapshot(head_sha="d" * 40),
            now="2026-07-15T12:00:00+00:00",
        )
        is EvidenceStatus.STALE
    )
    assert (
        evidence_status_for(
            current,
            current_snapshot=_snapshot(),
            now="2026-07-16T00:00:00+00:00",
        )
        is EvidenceStatus.STALE
    )

    first = _item(summary="Call graph A", created_at="2026-07-15T00:00:00+00:00")
    second = _item(summary="Call graph B", created_at="2026-07-15T00:01:00+00:00")
    unrelated = _item(
        summary="Test evidence",
        conflict_group="test-coverage:service",
        created_at="2026-07-15T00:02:00+00:00",
    )
    marked = mark_evidence_conflicts((unrelated, second, first))
    by_id = {item.evidence_id: item for item in marked}
    assert by_id[first.evidence_id].status is EvidenceStatus.CONFLICTING
    assert by_id[second.evidence_id].status is EvidenceStatus.CONFLICTING
    assert by_id[unrelated.evidence_id].status is EvidenceStatus.CURRENT
    assert tuple(item.evidence_id for item in marked) == tuple(
        sorted(item.evidence_id for item in marked)
    )


def test_private_content_addressed_store_separates_artifacts_and_round_trips(
    tmp_path: Path,
) -> None:
    artifact = b'{"nodes": ["CodingService"]}'
    store = JsonEvidenceStore(
        tmp_path,
        InMemoryLockManager(),
        max_total_bytes=1_000_000,
        max_artifact_bytes=100_000,
    )
    item = _item(artifact=artifact)
    artifact_ref = item.provenance.artifact
    assert artifact_ref is not None

    assert store.create(item, artifact=artifact) == item
    item_path = tmp_path / "evidence" / "items" / f"{item.evidence_id}.json"
    artifact_path = tmp_path / "evidence" / "artifacts" / f"{artifact_ref.digest}.blob"
    assert item_path.is_file()
    assert artifact_path.is_file()
    assert os.stat(tmp_path / "evidence").st_mode & 0o777 == 0o700
    assert os.stat(item_path).st_mode & 0o777 == 0o600
    assert os.stat(artifact_path).st_mode & 0o777 == 0o600
    assert artifact not in item_path.read_bytes()
    assert store.read(item.evidence_id) == item
    assert store.read_artifact(artifact_ref.digest) == artifact
    assert JsonEvidenceStore.encode_for_test(item) == item_path.read_bytes()

    # Content-addressed creation is idempotent for identical normalized evidence.
    assert store.create(item, artifact=artifact) == item
    assert len(list((tmp_path / "evidence" / "items").glob("*.json"))) == 1
    assert len(list((tmp_path / "evidence" / "artifacts").glob("*.blob"))) == 1


def test_store_rejects_corruption_future_schema_digest_mismatch_and_missing_artifact(
    tmp_path: Path,
) -> None:
    artifact = b"provider-artifact"
    store = JsonEvidenceStore(tmp_path, InMemoryLockManager(), max_total_bytes=1_000_000)
    item = _item(artifact=artifact)
    artifact_ref = item.provenance.artifact
    assert artifact_ref is not None
    store.create(item, artifact=artifact)
    item_path = tmp_path / "evidence" / "items" / f"{item.evidence_id}.json"

    frame = json.loads(item_path.read_text(encoding="utf-8"))
    frame["payload_sha256"] = "0" * 64
    item_path.write_text(json.dumps(frame), encoding="utf-8")
    with pytest.raises(RepoForgeError) as corrupt:
        store.read(item.evidence_id)
    assert corrupt.value.code is ErrorCode.EVIDENCE_CORRUPT

    item_path.write_bytes(JsonEvidenceStore.encode_for_test(item))
    frame = json.loads(item_path.read_text(encoding="utf-8"))
    frame["evidence"]["schema_version"] = EVIDENCE_SCHEMA_VERSION + 1
    canonical = json.dumps(
        frame["evidence"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    frame["payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    item_path.write_text(json.dumps(frame), encoding="utf-8")
    with pytest.raises(RepoForgeError) as future:
        store.read(item.evidence_id)
    assert future.value.code is ErrorCode.EVIDENCE_SCHEMA_UNSUPPORTED

    with pytest.raises(RepoForgeError) as mismatch:
        store.create(_item(artifact=artifact), artifact=b"different")
    assert mismatch.value.code is ErrorCode.EVIDENCE_ARTIFACT_DIGEST_MISMATCH

    item_path.write_bytes(JsonEvidenceStore.encode_for_test(item))
    artifact_path = tmp_path / "evidence" / "artifacts" / f"{artifact_ref.digest}.blob"
    artifact_path.unlink()
    with pytest.raises(RepoForgeError) as missing:
        store.read_artifact(artifact_ref.digest)
    assert missing.value.code is ErrorCode.EVIDENCE_ARTIFACT_MISSING


def test_query_filters_orders_and_paginates_deterministically(
    tmp_path: Path,
) -> None:
    store = JsonEvidenceStore(tmp_path, InMemoryLockManager(), max_total_bytes=1_000_000)
    oldest = _item(created_at="2026-07-15T00:00:00+00:00")
    middle = _item(
        source_kind=EvidenceSourceKind.VERIFICATION,
        provider_id="pytest",
        provider_version="9.1",
        created_at="2026-07-15T00:01:00+00:00",
        conflict_group=None,
    )
    newest = _item(
        created_at="2026-07-15T00:02:00+00:00",
        symbols=("OtherSymbol",),
        tests=("tests/test_other.py::test_other",),
        conflict_group=None,
    )
    for item in (middle, newest, oldest):
        store.create(item)

    first_page = store.query(
        EvidenceQuery(path="src/service.py", limit=1),
        current_snapshot=_snapshot(),
        now="2026-07-15T12:00:00+00:00",
    )
    assert first_page.items == (newest,)
    assert first_page.next_cursor is not None
    second_page = store.query(
        EvidenceQuery(path="src/service.py", limit=1, cursor=first_page.next_cursor),
        current_snapshot=_snapshot(),
        now="2026-07-15T12:00:00+00:00",
    )
    assert second_page.items == (middle,)
    assert second_page.next_cursor is not None

    symbol_page = store.query(EvidenceQuery(symbol="CodingService", limit=10))
    assert symbol_page.items == (middle, oldest)
    test_page = store.query(
        EvidenceQuery(
            source_kinds=(EvidenceSourceKind.VERIFICATION,),
            test="tests/test_service.py::test_service",
            limit=10,
        )
    )
    assert test_page.items == (middle,)

    stale_hidden = store.query(
        EvidenceQuery(limit=10),
        current_snapshot=_snapshot(head_sha="f" * 40),
        now="2026-07-15T12:00:00+00:00",
    )
    assert stale_hidden.items == ()
    stale_visible = store.query(
        EvidenceQuery(limit=10, include_stale=True),
        current_snapshot=_snapshot(head_sha="f" * 40),
        now="2026-07-15T12:00:00+00:00",
    )
    assert len(stale_visible.items) == 3
    assert all(item.status is EvidenceStatus.STALE for item in stale_visible.items)


def test_quota_and_reference_aware_retention_fail_closed(tmp_path: Path) -> None:
    store = JsonEvidenceStore(tmp_path, InMemoryLockManager(), max_total_bytes=100_000)
    records = tuple(
        _item(
            summary=f"Evidence {index}",
            created_at=f"2026-07-{10 + index:02d}T00:00:00+00:00",
            conflict_group=None,
        )
        for index in range(1, 4)
    )
    for item in records:
        store.create(item)

    report = store.prune(
        now="2026-07-15T00:00:00+00:00",
        retention_seconds=2 * 24 * 60 * 60,
        max_items=1,
        max_total_bytes=100_000,
        protected_evidence_ids=(records[0].evidence_id,),
    )
    assert report.protected_items == 1
    assert store.read(records[0].evidence_id) == records[0]
    assert report.remaining_items == 1

    tiny = JsonEvidenceStore(
        tmp_path / "tiny",
        InMemoryLockManager(),
        max_total_bytes=100,
        max_artifact_bytes=100,
    )
    with pytest.raises(RepoForgeError) as exhausted:
        tiny.create(_item(summary="x" * 1_000, conflict_group=None))
    assert exhausted.value.code is ErrorCode.EVIDENCE_QUOTA_EXCEEDED


def test_concurrent_identical_creates_produce_one_valid_record(tmp_path: Path) -> None:
    store = JsonEvidenceStore(tmp_path, InMemoryLockManager(), max_total_bytes=1_000_000)
    item = _item(conflict_group=None)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(lambda _index: store.create(item), range(32)))

    assert all(result == item for result in results)
    assert store.read(item.evidence_id) == item
    assert len(list((tmp_path / "evidence" / "items").glob("*.json"))) == 1

    changed_identity = replace(item, evidence_id="ev-" + "0" * 24)
    with pytest.raises(RepoForgeError) as invalid:
        store.create(changed_identity)
    assert invalid.value.code is ErrorCode.EVIDENCE_INVALID
