from __future__ import annotations

import pytest

from repoforge.domain.activation import (
    ActivationOutcome,
    ActivationReceipt,
    ActivationStage,
    ReleaseManifest,
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
