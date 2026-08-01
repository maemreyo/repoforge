"""Field-level contract-identity report for `rf doctor` (#367).

The doctor must name exactly what is inconsistent -- the active release, the identity
its manifest recorded, the packaged identity, and the in-process registry -- so an
operator can pick the safe next action instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...contracts import generated_contract_identity
from ...contracts.registry import (
    contract_artifact_paths,
    contract_identity_digest,
    render_contract_identity_artifact,
)
from ...ports.activation import ObservedRuntime, ReleaseStore

_TERMINAL_FAILED_PHASES = frozenset({"failed", "fail_closed"})
_IDENTITY_FIELDS = (
    "input_contract_digest",
    "output_contract_digest",
    "tool_schema_bundle_digest",
)


@dataclass(frozen=True, slots=True)
class ContractIdentityReport:
    ok: bool
    release_sha: str | None
    manifest_contract_identity: str | None
    packaged_contract_identity: str | None
    computed_registry_identity: str | None
    mismatched_fields: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    safe_next_action: str

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "release_sha": self.release_sha,
            "manifest_contract_identity": self.manifest_contract_identity,
            "packaged_contract_identity": self.packaged_contract_identity,
            "computed_registry_identity": self.computed_registry_identity,
            "mismatched_fields": list(self.mismatched_fields),
            "artifact_paths": list(self.artifact_paths),
            "safe_next_action": self.safe_next_action,
        }


def build_contract_identity_report(
    *,
    store: ReleaseStore,
    observed: ObservedRuntime | None,
) -> ContractIdentityReport:
    """Compare the active release's manifest proof against the packaged and computed
    identity, and advise the safe next action from the live runtime state."""

    release_sha = store.current_sha()
    manifest_identity: str | None = None
    if release_sha is not None:
        manifest = store.read_manifest(release_sha)
        manifest_identity = manifest.contract_identity if manifest is not None else None

    computed = render_contract_identity_artifact()
    packaged = dict(generated_contract_identity.CONTRACT_IDENTITY)
    computed_identity = contract_identity_digest(computed)
    packaged_identity = contract_identity_digest(packaged)

    mismatched = [field for field in _IDENTITY_FIELDS if computed.get(field) != packaged.get(field)]
    if manifest_identity and packaged_identity and manifest_identity != packaged_identity:
        mismatched.append("manifest")

    runtime_failed = bool(
        observed is not None
        and observed.phase in _TERMINAL_FAILED_PHASES
        and observed.last_error_code is not None
    )
    ok = not mismatched and not runtime_failed
    safe_next_action = _safe_next_action(
        release_sha=release_sha,
        mismatched=tuple(mismatched),
        runtime_failed=runtime_failed,
        manifest_identity=manifest_identity,
    )
    return ContractIdentityReport(
        ok=ok,
        release_sha=release_sha,
        manifest_contract_identity=manifest_identity,
        packaged_contract_identity=packaged_identity,
        computed_registry_identity=computed_identity,
        mismatched_fields=tuple(mismatched),
        artifact_paths=contract_artifact_paths(),
        safe_next_action=safe_next_action,
    )


def _safe_next_action(
    *,
    release_sha: str | None,
    mismatched: tuple[str, ...],
    runtime_failed: bool,
    manifest_identity: str | None,
) -> str:
    if release_sha is None:
        return (
            "no release is active; run `rf upgrade --activate` from a clean worktree"
            " with regenerated contract artifacts"
        )
    if mismatched or runtime_failed:
        if mismatched and not manifest_identity:
            return (
                "the active release's packaged contract identity differs from its "
                "in-process registry; rebuild and activate a release from a worktree "
                "with regenerated contract artifacts"
            )
        return (
            "the runtime is fail-closed on an inconsistent release; run "
            "`rf upgrade reconcile --repair rollback` to return to the previous "
            "release, then rebuild and activate from a clean worktree"
        )
    return "no action required"
