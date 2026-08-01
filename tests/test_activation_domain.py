from __future__ import annotations

import json

import pytest

from repoforge.domain.activation import (
    ActivationOutcome,
    ActivationReceipt,
    ActivationStage,
    ReleaseManifest,
    worker_reclamation_summary,
)

_SHA256 = "a" * 64
_OTHER_SHA256 = "b" * 64
_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _manifest(**overrides: str) -> ReleaseManifest:
    base = {
        "commit_sha": _COMMIT,
        "package_version": "2.2.0",
        "build_fingerprint": _SHA256,
        "tool_surface_hash": _OTHER_SHA256,
        "source_worktree": "/home/dev/repoforge",
        "built_at": "2026-07-25T10:00:00+00:00",
    }
    base.update(overrides)
    return ReleaseManifest(**base)


def test_release_manifest_round_trips_through_a_dict() -> None:
    manifest = _manifest()
    assert ReleaseManifest.from_dict(manifest.to_dict()) == manifest


def test_release_manifest_carries_human_provenance_without_changing_identity() -> None:
    """`branch`/`subject` are labels, so they must not alter what a release IS."""
    plain = _manifest()
    labelled = _manifest(branch="feat/activation", subject="ship the gate")

    assert labelled.branch == "feat/activation"
    assert labelled.label == "feat/activation"
    assert ReleaseManifest.from_dict(labelled.to_dict()) == labelled
    # Identity is the commit and the build fingerprint; the label is not part of it.
    assert labelled.commit_sha == plain.commit_sha
    assert labelled.build_fingerprint == plain.build_fingerprint


def test_a_manifest_written_before_provenance_existed_is_still_readable() -> None:
    """Releases installed by an earlier version must not become unreadable."""
    legacy = _manifest().to_dict()
    del legacy["branch"]
    del legacy["subject"]

    manifest = ReleaseManifest.from_dict(legacy)

    assert manifest.branch == ""
    assert manifest.subject == ""
    # Still displayable: a short sha is the fallback label.
    assert manifest.label == _COMMIT[:12]


def test_a_manifest_with_a_non_string_branch_is_corruption_not_age() -> None:
    raw = _manifest().to_dict()
    broken: dict[str, object] = dict(raw)
    broken["branch"] = 17
    with pytest.raises(ValueError, match="branch"):
        ReleaseManifest.from_dict(broken)


def test_release_manifest_rejects_a_non_hex_commit() -> None:
    with pytest.raises(ValueError, match="commit_sha"):
        _manifest(commit_sha="not-a-sha")


def test_release_manifest_rejects_a_non_sha256_fingerprint() -> None:
    with pytest.raises(ValueError, match="build_fingerprint"):
        _manifest(build_fingerprint="short")


def test_manifest_round_trips_the_contract_identity_proof() -> None:
    manifest = _manifest(contract_identity="c" * 64)
    assert ReleaseManifest.from_dict(manifest.to_dict()) == manifest


def test_a_manifest_written_before_contract_proof_existed_is_still_readable() -> None:
    """Releases installed before #367 carry no contract proof; they stay readable."""
    legacy = _manifest().to_dict()
    del legacy["contract_identity"]

    manifest = ReleaseManifest.from_dict(legacy)

    assert manifest.contract_identity == ""


def test_manifest_rejects_a_non_sha256_contract_identity() -> None:
    with pytest.raises(ValueError, match="contract_identity"):
        _manifest(contract_identity="not-a-digest")


def test_activation_receipt_round_trips_and_preserves_outcome() -> None:
    receipt = ActivationReceipt(
        receipt_id="act-20260725-001",
        from_sha=_COMMIT,
        to_sha="fedcba98",
        to_fingerprint=_SHA256,
        tool_surface_hash=_OTHER_SHA256,
        rediscovery_required=True,
        outcome=ActivationOutcome.ACTIVATED,
        activated_at="2026-07-25T10:05:00+00:00",
        from_fingerprint=_OTHER_SHA256,
        detail="activated",
        stage=ActivationStage.HEALTH_VERIFIED,
        observed_sha="fedcba98",
        converged=True,
        cause_receipt_id="act-20260725-000",
    )
    restored = ActivationReceipt.from_dict(receipt.to_dict())
    assert restored == receipt
    assert restored.outcome is ActivationOutcome.ACTIVATED


def test_activation_receipt_allows_a_null_origin_for_the_first_activation() -> None:
    receipt = ActivationReceipt(
        receipt_id="act-20260725-001",
        from_sha=None,
        to_sha=_COMMIT,
        to_fingerprint=_SHA256,
        tool_surface_hash=_OTHER_SHA256,
        rediscovery_required=False,
        outcome=ActivationOutcome.ACTIVATED,
        activated_at="2026-07-25T10:05:00+00:00",
        stage=ActivationStage.HEALTH_VERIFIED,
        converged=True,
    )
    assert receipt.from_sha is None
    assert receipt.from_fingerprint is None


def test_activation_receipt_rejects_a_malformed_id() -> None:
    with pytest.raises(ValueError, match="receipt_id"):
        ActivationReceipt(
            receipt_id="not-a-receipt",
            from_sha=None,
            to_sha=_COMMIT,
            to_fingerprint=_SHA256,
            tool_surface_hash=_OTHER_SHA256,
            rediscovery_required=False,
            outcome=ActivationOutcome.ACTIVATED,
            activated_at="2026-07-25T10:05:00+00:00",
        )


def test_activated_receipt_requires_convergence_and_a_verified_stage() -> None:
    """A symlink switch is not an activation: ACTIVATED must be earned."""
    with pytest.raises(ValueError, match="without convergence"):
        ActivationReceipt(
            receipt_id="act-20260725-001",
            from_sha=None,
            to_sha=_COMMIT,
            to_fingerprint=_SHA256,
            tool_surface_hash=_OTHER_SHA256,
            rediscovery_required=False,
            outcome=ActivationOutcome.ACTIVATED,
            activated_at="2026-07-25T10:05:00+00:00",
            stage=ActivationStage.SYMLINK_SWITCHED,
            converged=False,
        )


def test_failed_receipt_may_record_the_stage_it_reached() -> None:
    receipt = ActivationReceipt(
        receipt_id="act-20260725-002",
        from_sha=_COMMIT,
        to_sha="fedcba98",
        to_fingerprint=_SHA256,
        tool_surface_hash=_OTHER_SHA256,
        rediscovery_required=False,
        outcome=ActivationOutcome.FAILED,
        activated_at="2026-07-25T10:05:00+00:00",
        stage=ActivationStage.RUNTIME_RESTARTED,
        observed_sha=_COMMIT,
        converged=False,
    )
    restored = ActivationReceipt.from_dict(receipt.to_dict())
    assert restored == receipt
    assert restored.stage is ActivationStage.RUNTIME_RESTARTED


# ------------------------------------------- bounded reclamation evidence (#424)


def _full_reclamation(n: int, *, unreadable: int = 0) -> dict[str, object]:
    return {
        "inspected": n,
        "reclaimed": n,
        "already_gone": 0,
        "refused_unproven": 0,
        "survived_kill": 0,
        "possibly_alive_unproven": 0,
        "scan_complete": True,
        "unreadable_record_ids": tuple(f"worker-bad-{i}" for i in range(unreadable)),
        "evidence_complete": unreadable == 0,
        "worker_ids": tuple(f"worker-{i:012x}" for i in range(n)),
        "pids": tuple(range(1000, 1000 + n)),
        "release_shas": ("1111aaa",),
        "detail": f"reconciled {n} live execution worker binding(s)",
    }


@pytest.mark.parametrize("count", [0, 1, 92, 2_000])
def test_worker_reclamation_summary_is_bounded_at_any_scale(count: int) -> None:
    """The receipt summary stays small at incident scale and beyond (#424)."""
    summary = worker_reclamation_summary(
        _full_reclamation(count), artifact_id="recl-abcd", digest="d" * 64
    )
    assert summary["inspected"] == count
    assert summary["worker_sample"] == [f"worker-{i:012x}" for i in range(min(count, 8))]
    assert summary["evidence_reference"] == "worker-reclamation:recl-abcd"
    assert summary["evidence_digest"] == "d" * 64
    encoded = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    assert len(encoded) < 4_096


def test_worker_reclamation_summary_ignores_many_unreadable_ids() -> None:
    """2,000 unreadable ids must not bloat the receipt summary (#424)."""
    summary = worker_reclamation_summary(
        _full_reclamation(2_000, unreadable=2_000),
        artifact_id="recl-abcd",
        digest="d" * 64,
    )
    assert summary["evidence_complete"] is False
    assert "unreadable_record_ids" not in summary
    assert len(json.dumps(summary, sort_keys=True, separators=(",", ":"))) < 4_096


def test_activation_receipt_accepts_a_bounded_worker_reclamation_summary() -> None:
    summary = worker_reclamation_summary(
        _full_reclamation(92), artifact_id="recl-abcd", digest="d" * 64
    )
    receipt = ActivationReceipt(
        receipt_id="act-20260725-001",
        from_sha=None,
        to_sha=_COMMIT,
        to_fingerprint=_SHA256,
        tool_surface_hash=_OTHER_SHA256,
        rediscovery_required=False,
        outcome=ActivationOutcome.ACTIVATED,
        activated_at="2026-07-25T10:05:00+00:00",
        stage=ActivationStage.HEALTH_VERIFIED,
        converged=True,
        worker_reclamation=summary,
    )
    assert receipt.worker_reclamation == summary


def test_activation_receipt_still_rejects_an_oversized_raw_reclamation() -> None:
    """The 4 KiB cap stays enforced; the summary is what makes it satisfiable."""
    raw = _full_reclamation(2_000)
    with pytest.raises(ValueError, match="worker_reclamation evidence is too large"):
        ActivationReceipt(
            receipt_id="act-20260725-001",
            from_sha=None,
            to_sha=_COMMIT,
            to_fingerprint=_SHA256,
            tool_surface_hash=_OTHER_SHA256,
            rediscovery_required=False,
            outcome=ActivationOutcome.ACTIVATED,
            activated_at="2026-07-25T10:05:00+00:00",
            stage=ActivationStage.HEALTH_VERIFIED,
            converged=True,
            worker_reclamation=raw,
        )
