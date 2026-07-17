"""Agent-facing configuration administration with capability-delta gating.

The service lets a connected model inspect the reviewed configuration, read bounded
operational logs, and propose repository policy changes. Every change flows through the
same immutable-generation pipeline as the CLI: template proposal, durable policy patch,
rendered candidate, ``load_config`` validation, and capability-delta classification.

Gating contract (enforced twice — here for UX, and independently by
``ConfigurationStore.accept`` which fails closed):

- ``equivalent`` changes create no generation;
- ``metadata_only`` and ``restriction`` changes are accepted immediately and hot
  reloaded when the runtime supports it;
- ``expansion`` and ``incompatible`` changes are never applied on the model's
  authority. They are persisted as a pending change the operator approves out of band
  with ``rf config approve <change_id>`` — the approval token never transits the model
  conversation.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ...config import load_config
from ...domain.config_generation import (
    CapabilityDeltaKind,
    ConfigMutation,
    classify_capability_delta,
    sha256_text,
)
from ...domain.errors import ConfigError
from ...domain.policy_patch import (
    PolicyPatchError,
    ProfilePatch,
    RepositoryPolicyPatch,
)
from ...domain.repository_proposal import EnrollmentMode
from ...ports.clock import Clock
from ...ports.configuration import ConfigurationStore
from ...ports.ids import IdGenerator
from ..approvals import PendingPolicyChangeStore
from ..configuration.document import (
    apply_policy_patch,
    apply_proposal,
    apply_ticket_graph,
    parse_resolved,
    render_resolved,
)
from ..configuration.source import (
    SourceConfiguration,
    SourceRepository,
    parse_source,
    render_source,
)
from ..repository_admin.proposals import RepositoryProposalService

_AUTO_APPLY_DELTAS = frozenset({CapabilityDeltaKind.METADATA_ONLY, CapabilityDeltaKind.RESTRICTION})
_MAX_LOG_LIMIT = 200


@dataclass(frozen=True, slots=True)
class ProfileDefinition:
    """One complete profile definition supplied by the connected model."""

    name: str
    commands: tuple[tuple[str, ...], ...]
    description: str = ""
    verification: bool = False
    timeout_seconds: int | None = None
    working_directory: str | None = None


class ConfigAdminService:
    """Bounded configuration inspection, log reads, and gated policy mutation."""

    def __init__(
        self,
        *,
        store: ConfigurationStore,
        proposals: RepositoryProposalService,
        clock: Clock,
        ids: IdGenerator,
        pending: PendingPolicyChangeStore,
        audit_log_path: Path,
        runtime_log_path: Path,
        read_audit: Callable[..., list[dict[str, Any]]],
        read_log: Callable[[Path, int], list[str]],
        reload_runtime: Callable[[int], dict[str, Any]] | None = None,
        read_runtime_status: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self._store = store
        self._proposals = proposals
        self._clock = clock
        self._ids = ids
        self.pending = pending
        self._audit_log_path = audit_log_path
        self._runtime_log_path = runtime_log_path
        self._read_audit = read_audit
        self._read_log = read_log
        self._reload_runtime = reload_runtime
        self._read_runtime_status = read_runtime_status

    # -- reads ---------------------------------------------------------------

    def config_inspect(self, repo_id: str | None = None) -> dict[str, Any]:
        current = self._store.current()
        if current is None:
            raise ConfigError("No accepted configuration generation")
        active = self._store.active()
        source = self._source()
        document = parse_resolved(self._store.read_resolved_text(current.generation))
        repositories_raw = document.get("repositories", {})
        if not isinstance(repositories_raw, dict):
            raise ConfigError("Resolved configuration repositories must be a table")
        if repo_id is not None and repo_id not in repositories_raw:
            raise ConfigError(f"Unknown repository id: {repo_id}")
        source_by_id = {item.repo_id: item for item in source.repositories}
        repositories: dict[str, Any] = {}
        for name in sorted(repositories_raw):
            if repo_id is not None and name != repo_id:
                continue
            entry = repositories_raw[name]
            if not isinstance(entry, dict):
                continue
            source_item = source_by_id.get(name)
            source_graph = (
                source_item.ticket_graph.as_table()
                if source_item is not None and source_item.ticket_graph is not None
                else None
            )
            accepted_raw = entry.get("ticket_graph")
            accepted_graph = dict(accepted_raw) if isinstance(accepted_raw, dict) else None
            graph_drift = (
                "none"
                if source_graph == accepted_graph
                else "source_only"
                if source_graph is not None and accepted_graph is None
                else "accepted_only"
                if source_graph is None and accepted_graph is not None
                else "mismatch"
            )
            repositories[name] = {
                "path": entry.get("path"),
                "read_only": entry.get("read_only", False),
                "publish_enabled": entry.get("publish_enabled", False),
                "default_base": entry.get("default_base"),
                "allowed_paths": entry.get("allowed_paths", []),
                "denied_paths": entry.get("denied_paths", []),
                "max_changed_files": entry.get("max_changed_files"),
                "max_diff_lines": entry.get("max_diff_lines"),
                "max_total_changed_bytes": entry.get("max_total_changed_bytes"),
                "default_verification_profile": entry.get("default_verification_profile"),
                "require_verification_before_commit": entry.get(
                    "require_verification_before_commit", False
                ),
                "execution_mode": entry.get("execution_mode", "strict"),
                "adhoc_runners": entry.get("adhoc_runners", []),
                "adhoc_timeout_seconds": entry.get("adhoc_timeout_seconds", 300),
                "profiles": entry.get("profiles", {}),
                "diagnostics": sorted((entry.get("diagnostics") or {}).keys()),
                "formatters": sorted((entry.get("formatters") or {}).keys()),
                "policy_template": source_item.policy_template if source_item else None,
                "decisions": dict(source_item.decisions) if source_item else {},
                "policy_overrides": dict(source_item.policy_overrides) if source_item else {},
                "policy_patch": source_item.policy_patch.as_table() if source_item else {},
                "ticket_graph": {
                    "source": source_graph,
                    "accepted": accepted_graph,
                    "drift": graph_drift,
                },
            }
        return {
            "status": "ok",
            "source_path": str(self._store.source_path),
            "accepted_generation": current.generation,
            "active_generation": active.generation if active else None,
            "restart_required": active is None or active.generation != current.generation,
            "capability_delta_of_accepted": current.delta.value,
            "repositories": repositories,
            "runtime_health": self._read_runtime_status() if self._read_runtime_status else None,
            "pending_changes": self.pending.summaries(),
        }

    def runtime_logs_read(
        self,
        source: str = "audit",
        limit: int = 50,
        action: str | None = None,
        only_failed: bool = False,
        min_duration_ms: float | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _MAX_LOG_LIMIT
        ):
            raise ConfigError(f"runtime_logs_read limit must be an integer in 1..{_MAX_LOG_LIMIT}")
        if source == "audit":
            events = self._read_audit(
                self._audit_log_path,
                limit=limit,
                action=action,
                only_failed=only_failed,
                min_duration_ms=min_duration_ms,
            )
            return {
                "status": "ok",
                "source": "audit",
                "path": str(self._audit_log_path),
                "events": events,
                "count": len(events),
            }
        if source == "runtime":
            lines = self._read_log(self._runtime_log_path, limit)
            return {
                "status": "ok",
                "source": "runtime",
                "path": str(self._runtime_log_path),
                "lines": lines,
                "count": len(lines),
            }
        raise ConfigError("runtime_logs_read source must be 'audit' or 'runtime'")

    # -- writes --------------------------------------------------------------

    def repo_policy_apply(
        self,
        repo_id: str,
        *,
        set_profiles: list[dict[str, Any]] | None = None,
        remove_profiles: list[str] | None = None,
        set_diagnostics: dict[str, Any] | None = None,
        remove_diagnostics: list[str] | None = None,
        set_formatters: dict[str, Any] | None = None,
        remove_formatters: list[str] | None = None,
        execution_mode: str | None = None,
        adhoc_runners: list[str] | None = None,
        adhoc_timeout_seconds: int | None = None,
        policy_overrides: dict[str, str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        current = self._store.current()
        if current is None:
            raise ConfigError("No accepted configuration generation")
        source = self._source()
        source_item = next((item for item in source.repositories if item.repo_id == repo_id), None)
        if source_item is None:
            raise ConfigError(f"Unknown repository id: {repo_id}")
        delta_patch = self._build_patch(
            set_profiles=set_profiles or [],
            remove_profiles=remove_profiles or [],
            set_diagnostics=set_diagnostics or {},
            remove_diagnostics=remove_diagnostics or [],
            set_formatters=set_formatters or {},
            remove_formatters=remove_formatters or [],
            execution_mode=execution_mode,
            adhoc_runners=adhoc_runners,
            adhoc_timeout_seconds=adhoc_timeout_seconds,
        )
        if delta_patch.is_empty() and not policy_overrides:
            raise ConfigError("repo_policy_apply requires at least one change")
        merged_patch = source_item.policy_patch.merge(delta_patch)
        merged_overrides = dict(source_item.policy_overrides)
        merged_overrides.update(policy_overrides or {})
        try:
            proposal = self._proposals.propose(
                Path(source_item.path),
                repo_id=repo_id,
                decisions=dict(source_item.decisions),
                template=EnrollmentMode(source_item.policy_template),
                overrides=merged_overrides,
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        if proposal.required_decisions:
            return {
                "status": "input_required",
                "required_decisions": [asdict(item) for item in proposal.required_decisions],
                "unchanged_state": ["configuration", "runtime"],
                "safe_next_action": (
                    "Ask the operator to resolve these decisions with "
                    "`rf repo refresh --decision ...` before changing policy."
                ),
            }
        updated_source = SourceConfiguration(
            source.tunnel_id,
            source.profile,
            tuple(
                SourceRepository(
                    item.repo_id,
                    item.path,
                    proposal.proposal_id,
                    item.policy_template,
                    item.decisions,
                    tuple(sorted(merged_overrides.items())),
                    merged_patch,
                    item.ticket_graph,
                )
                if item.repo_id == repo_id
                else item
                for item in source.repositories
            ),
        )
        source_text = render_source(updated_source)
        document = parse_resolved(self._store.read_resolved_text(current.generation))
        document = apply_proposal(document, proposal)
        document = apply_ticket_graph(document, repo_id, source_item.ticket_graph)
        try:
            document = apply_policy_patch(document, repo_id, merged_patch)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        fingerprint_map = current.repository_fingerprint_map()
        fingerprint_map[repo_id] = proposal.facts_fingerprint
        fingerprints = tuple(sorted(fingerprint_map.items()))
        reason = f"agent policy change for {repo_id}"
        candidate = render_resolved(
            document,
            generation=current.generation + 1,
            source_path=str(self._store.source_path),
            source_sha256=sha256_text(source_text),
            created_at=self._clock.now_iso(),
            reason=reason,
            proposal_id=None,
            repository_fingerprints=fingerprints,
        )
        self._validate_candidate(candidate)
        delta = classify_capability_delta(
            self._store.read_resolved_text(current.generation), candidate
        )
        changes = [asdict(change) for change in delta.changes]
        for change in changes:
            change["direction"] = str(change["direction"].value)
        if dry_run:
            return {
                "status": "preview",
                "capability_delta": delta.kind.value,
                "changes": changes,
                "requires_operator_approval": delta.kind
                not in _AUTO_APPLY_DELTAS | {CapabilityDeltaKind.EQUIVALENT},
                "unchanged_state": ["configuration", "runtime"],
                "safe_next_action": "Re-run with dry_run=false to request the change.",
            }
        if delta.kind is CapabilityDeltaKind.EQUIVALENT:
            return {
                "status": "unchanged",
                "capability_delta": delta.kind.value,
                "generation": current.generation,
                "unchanged_state": ["configuration", "runtime"],
                "safe_next_action": "No semantic change was detected; nothing to apply.",
            }
        expected_source_sha = sha256_text(self._store.read_source_text())
        if delta.kind in _AUTO_APPLY_DELTAS:
            change_id = self._change_id(candidate)
            generation = self._store.accept(
                ConfigMutation(
                    source_text,
                    candidate,
                    fingerprints,
                    reason,
                    self._clock.now_iso(),
                    current.generation,
                    expected_source_sha,
                    proposal_id=change_id,
                    approval=None,
                    correlation_id=self._ids.new_hex(24),
                )
            )
            reload_result = self._reload(generation.generation)
            return {
                "status": "applied",
                "capability_delta": delta.kind.value,
                "changes": changes,
                "generation": generation.generation,
                "runtime": reload_result,
                "safe_next_action": (
                    "The change is active."
                    if reload_result.get("status") == "hot_reloaded"
                    else "Ask the operator to run `rf runtime reload` to activate it."
                ),
            }
        change_id = self._change_id(candidate)
        record = {
            "change_id": change_id,
            "repo_id": repo_id,
            "reason": reason,
            "created_at": self._clock.now_iso(),
            "capability_delta": delta.kind.value,
            "changes": changes,
            "source_text": source_text,
            "resolved_text": candidate,
            "repository_fingerprints": [list(item) for item in fingerprints],
            "expected_generation": current.generation,
            "expected_source_sha256": expected_source_sha,
            "proposal_id": change_id,
        }
        self.pending.save(record)
        return {
            "status": "pending_approval",
            "capability_delta": delta.kind.value,
            "changes": changes,
            "change_id": change_id,
            "unchanged_state": ["configuration", "runtime"],
            "safe_next_action": (
                "This change expands allowed capability, so it requires operator review. "
                f"Ask the operator to run `rf config approve {change_id}` "
                f"(or `rf config reject {change_id}`) in a terminal."
            ),
        }

    # -- internals -----------------------------------------------------------

    def _source(self) -> SourceConfiguration:
        try:
            return parse_source(self._store.read_source_text())
        except ValueError as exc:
            raise ConfigError(
                "Legacy resolved configuration does not support agent policy changes. "
                "Migrate with `rf setup --force ...` first."
            ) from exc

    @staticmethod
    def _build_patch(
        *,
        set_profiles: list[dict[str, Any]],
        remove_profiles: list[str],
        set_diagnostics: dict[str, Any],
        remove_diagnostics: list[str],
        set_formatters: dict[str, Any],
        remove_formatters: list[str],
        execution_mode: str | None,
        adhoc_runners: list[str] | None,
        adhoc_timeout_seconds: int | None,
    ) -> RepositoryPolicyPatch:
        try:
            profiles = []
            for raw in set_profiles:
                if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                    raise PolicyPatchError("each profile requires a string name")
                table = {key: value for key, value in raw.items() if key != "name"}
                profiles.append(ProfilePatch.from_table(raw["name"], table))
            return RepositoryPolicyPatch(
                profiles=tuple(profiles),
                diagnostics=tuple(sorted(set_diagnostics.items())),
                formatters=tuple(sorted(set_formatters.items())),
                execution_mode=execution_mode,
                adhoc_runners=tuple(adhoc_runners) if adhoc_runners is not None else None,
                adhoc_timeout_seconds=adhoc_timeout_seconds,
                remove_profiles=tuple(remove_profiles),
                remove_diagnostics=tuple(remove_diagnostics),
                remove_formatters=tuple(remove_formatters),
            )
        except PolicyPatchError as exc:
            raise ConfigError(f"Invalid policy patch: {exc}") from exc

    @staticmethod
    def _validate_candidate(candidate: str) -> None:
        with tempfile.TemporaryDirectory(prefix="repoforge-policy-candidate-") as directory:
            path = Path(directory) / "resolved.toml"
            path.write_text(candidate, encoding="utf-8")
            load_config(path)

    @staticmethod
    def _change_id(candidate: str) -> str:
        return f"chg-{sha256_text(candidate)[:20]}"

    def _reload(self, generation: int) -> dict[str, Any]:
        if self._reload_runtime is None:
            return {
                "status": "restart_required",
                "detail": "No in-process runtime reload is available for this transport.",
            }
        try:
            return self._reload_runtime(generation)
        except Exception as exc:
            return {
                "status": "reload_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }
