"""Field-level contract-identity report for `rf doctor` (#367).

The doctor must name exactly what is inconsistent -- the active release, the identity
its manifest recorded, the packaged identity, and the in-process registry -- so an
operator can pick the safe next action instead of guessing.
"""

from __future__ import annotations

from pathlib import Path

from repoforge.adapters.activation.release_store import RuntimeReleaseStore
from repoforge.application.activation.contract_identity import build_contract_identity_report
from repoforge.contracts.registry import contract_identity_digest, render_contract_identity_artifact
from repoforge.domain.activation import ReleaseManifest
from repoforge.ports.activation import ObservedRuntime

_SHA = "0123456789abcdef0123456789abcdef01234567"
_SURFACE = "a" * 64
_FINGERPRINT = "b" * 64
_CLEAN_IDENTITY = contract_identity_digest(render_contract_identity_artifact())


def _manifest(*, contract_identity: str = "") -> ReleaseManifest:
    return ReleaseManifest(
        commit_sha=_SHA,
        package_version="2.2.0",
        build_fingerprint=_FINGERPRINT,
        tool_surface_hash=_SURFACE,
        contract_identity=contract_identity,
        source_worktree="/home/dev/repoforge",
        built_at="2026-07-25T10:00:00+00:00",
    )


def _store(tmp_path: Path, *, manifest: ReleaseManifest | None) -> RuntimeReleaseStore:
    store = RuntimeReleaseStore(tmp_path / "release-root")
    if manifest is not None:
        store.release_path(_SHA).mkdir(parents=True, exist_ok=True)
        store.write_manifest(manifest)
        store.swap_current(_SHA)
    return store


def test_report_is_clean_when_everything_agrees(tmp_path: Path) -> None:
    report = build_contract_identity_report(
        store=_store(tmp_path, manifest=_manifest(contract_identity=_CLEAN_IDENTITY)),
        observed=None,
    )

    assert report.ok is True
    assert report.release_sha == _SHA
    assert report.mismatched_fields == ()
    assert report.manifest_contract_identity == _CLEAN_IDENTITY
    assert report.packaged_contract_identity == report.computed_registry_identity == _CLEAN_IDENTITY
    assert "no action" in report.safe_next_action


def test_report_names_the_offending_artifact_paths(tmp_path: Path) -> None:
    report = build_contract_identity_report(
        store=_store(tmp_path, manifest=_manifest(contract_identity=_CLEAN_IDENTITY)),
        observed=None,
    )

    assert report.artifact_paths
    assert all(path.endswith("generated_contract_identity.py") for path in report.artifact_paths)


def test_report_flags_a_manifest_that_disagrees_with_the_packaged_identity(
    tmp_path: Path,
) -> None:
    report = build_contract_identity_report(
        store=_store(tmp_path, manifest=_manifest(contract_identity="d" * 64)),
        observed=None,
    )

    assert report.ok is False
    assert report.manifest_contract_identity == "d" * 64
    assert report.packaged_contract_identity == report.computed_registry_identity
    assert "manifest" in report.mismatched_fields
    assert "repair rollback" in report.safe_next_action


def test_report_advises_repair_for_a_fail_closed_runtime(tmp_path: Path) -> None:
    report = build_contract_identity_report(
        store=_store(tmp_path, manifest=_manifest(contract_identity=_CLEAN_IDENTITY)),
        observed=ObservedRuntime(
            running_release_sha=_SHA,
            phase="fail_closed",
            pid=99,
            last_error_code="CONTRACT_ARTIFACT_MISMATCH",
        ),
    )

    assert report.ok is False
    assert "repair rollback" in report.safe_next_action
