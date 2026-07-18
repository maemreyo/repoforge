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

import hashlib
import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ...config import load_config
from ...domain.config_generation import (
    CapabilityDeltaKind,
    ConfigGeneration,
    ConfigMutation,
    classify_capability_delta,
    sha256_text,
)
from ...domain.errors import ConfigError, RepoForgeError
from ...domain.generated_paths import parse_generated_paths
from ...domain.issue_writes import IssueWritePolicy, IssueWritePolicyError
from ...domain.policy_patch import (
    PolicyPatchError,
    ProfilePatch,
    RepositoryPolicyPatch,
)
from ...domain.redaction import redact_text
from ...domain.repository_proposal import EnrollmentMode
from ...ports.clock import Clock
from ...ports.configuration import ConfigurationStore
from ...ports.ids import IdGenerator
from ..approvals import PendingPolicyChangeStore
from ..configuration.document import (
    apply_generated_paths,
    apply_issue_write_policy,
    apply_policy_patch,
    apply_proposal,
    apply_risk_policy,
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
_HOST_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9:/])/(?:[^/\s]+/)*[^/\s]+"),
    re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:\\(?:[^\\\s]+\\)*[^\\\s]+"),
)


def _redact_host_paths(value: str) -> str:
    redacted = redact_text(value, limit=4_000)
    for pattern in _HOST_PATH_PATTERNS:
        redacted = pattern.sub("<redacted:host_path>", redacted)
    return redacted


class AuditEventPageView(Protocol):
    @property
    def events(self) -> list[dict[str, Any]]: ...

    @property
    def next_cursor(self) -> str | None: ...

    @property
    def truncated(self) -> bool: ...


class AuditPageReader(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        limit: int,
        action: str | None = None,
        only_failed: bool = False,
        min_duration_ms: float | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        cursor: str | None = None,
    ) -> AuditEventPageView: ...


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
        read_audit_page: AuditPageReader | None = None,
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
        self._read_audit_page = read_audit_page
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
                "generated_paths": (
                    [rule.as_table() for rule in source_item.generated_paths] if source_item else []
                ),
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

    @staticmethod
    def _changed_sections(changes: object) -> list[str]:
        if not isinstance(changes, list):
            return []
        sections: set[str] = set()
        for item in changes:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if isinstance(path, str) and path:
                sections.add(path.split(".", 1)[0])
        return sorted(sections)[:100]

    def _generation_summary(self, generation: ConfigGeneration, state: str) -> dict[str, Any]:
        changed_sections: list[str]
        previous = getattr(generation, "previous_generation", None)
        if isinstance(previous, int) and previous > 0:
            delta = classify_capability_delta(
                self._store.read_resolved_text(previous),
                self._store.read_resolved_text(generation.generation),
            )
            changed_sections = sorted({item.path.split(".", 1)[0] for item in delta.changes})[:100]
        else:
            document = parse_resolved(self._store.read_resolved_text(generation.generation))
            changed_sections = sorted(
                key for key in document if key not in {"repoforge_lock", "schema_version"}
            )[:100]
        return {
            "generation": generation.generation,
            "state": state,
            "digest": generation.resolved_sha256,
            "changed_sections": changed_sections,
        }

    def config_inspect_v2(
        self, repo_id: str | None = None, include_pending: bool = True
    ) -> dict[str, Any]:
        current = self._store.current()
        if current is None:
            raise ConfigError("No accepted configuration generation")
        active = self._store.active()
        document = parse_resolved(self._store.read_resolved_text(current.generation))
        repositories = document.get("repositories", {})
        if not isinstance(repositories, dict):
            raise ConfigError("Resolved configuration repositories must be a table")
        if repo_id is not None and repo_id not in repositories:
            raise ConfigError(f"Unknown repository id: {repo_id}")
        selected = [repo_id] if repo_id is not None else sorted(str(key) for key in repositories)
        facts: list[dict[str, str]] = []
        for name in selected:
            entry = repositories.get(name)
            if not isinstance(entry, dict):
                continue
            prefix = "" if repo_id is not None or len(selected) == 1 else f"{name}."
            profiles = entry.get("profiles")
            diagnostics = entry.get("diagnostics")
            values = {
                "repo_id": name,
                "read_only": str(bool(entry.get("read_only", False))).lower(),
                "publish_enabled": str(bool(entry.get("publish_enabled", False))).lower(),
                "default_base": str(entry.get("default_base", "")),
                "profile_count": str(len(profiles) if isinstance(profiles, dict) else 0),
                "diagnostic_count": str(len(diagnostics) if isinstance(diagnostics, dict) else 0),
                "execution_mode": str(entry.get("execution_mode", "strict")),
            }
            facts.extend(
                {"key": prefix + key, "value": value[:10_000]}
                for key, value in sorted(values.items())
            )
        pending: list[dict[str, Any]] = []
        if include_pending:
            for item in self.pending.summaries()[:100]:
                expected = item.get("expected_generation")
                if not isinstance(expected, int) or expected <= 0:
                    continue
                encoded = json.dumps(item, sort_keys=True, default=str, separators=(",", ":"))
                pending.append(
                    {
                        "generation": expected,
                        "state": "pending",
                        "digest": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                        "changed_sections": self._changed_sections(item.get("changes")),
                    }
                )
        restart_required = active is None or active.generation != current.generation
        return {
            "status": "ok",
            "summary": f"Inspected accepted configuration generation {current.generation}",
            "error": None,
            "accepted": self._generation_summary(current, "accepted"),
            "active": self._generation_summary(active, "active") if active is not None else None,
            "pending": pending,
            "capability_delta": current.delta.value,
            "restart_required": restart_required,
            "repo_facts": facts,
        }

    @staticmethod
    def _parse_log_time(value: str | None, field: str) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigError(f"{field} must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ConfigError(f"{field} must include a timezone offset")
        return parsed

    @staticmethod
    def _runtime_cursor_binding(
        *,
        source: str,
        action: str | None,
        only_failed: bool,
        min_duration_ms: float | None,
        start_time: str | None,
        end_time: str | None,
    ) -> str:
        payload = {
            "action": action,
            "end_time": end_time,
            "min_duration_ms": min_duration_ms,
            "only_failed": only_failed,
            "source": source,
            "start_time": start_time,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]

    @staticmethod
    def _runtime_entry(line: str) -> dict[str, Any]:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, dict):
            timestamp = raw.get("timestamp")
            level = raw.get("level", "INFO")
            message = raw.get("message", "")
            action = raw.get("action")
            duration = raw.get("duration_ms")
            return {
                "timestamp": (
                    timestamp
                    if isinstance(timestamp, str) and timestamp
                    else "1970-01-01T00:00:00+00:00"
                ),
                "source": "runtime",
                "action": action if isinstance(action, str) else None,
                "level": str(level)[:30] or "INFO",
                "message": _redact_host_paths(str(message)),
                "duration_ms": (
                    float(duration)
                    if isinstance(duration, (int, float)) and duration >= 0
                    else None
                ),
            }
        return {
            "timestamp": "1970-01-01T00:00:00+00:00",
            "source": "runtime",
            "action": None,
            "level": "INFO",
            "message": _redact_host_paths(line),
            "duration_ms": None,
        }

    def runtime_logs_read_v2(
        self,
        source: str = "audit",
        *,
        limit: int = 50,
        action: str | None = None,
        only_failed: bool = False,
        min_duration_ms: float | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if source not in {"audit", "runtime"}:
            raise ConfigError("runtime_logs_read source must be 'audit' or 'runtime'")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _MAX_LOG_LIMIT
        ):
            raise ConfigError(f"runtime_logs_read limit must be between 1 and {_MAX_LOG_LIMIT}")
        start = self._parse_log_time(start_time, "start_time")
        end = self._parse_log_time(end_time, "end_time")
        if start is not None and end is not None and start > end:
            raise ConfigError("start_time must not be after end_time")
        if source == "audit":
            if self._read_audit_page is None:
                raise ConfigError("Bounded audit paging is not configured")
            page = self._read_audit_page(
                self._audit_log_path,
                limit=limit,
                action=action,
                only_failed=only_failed,
                min_duration_ms=min_duration_ms,
                start_time=start_time,
                end_time=end_time,
                cursor=cursor,
            )
            entries: list[dict[str, Any]] = []
            for event in page.events:
                details = event.get("details")
                duration = details.get("duration_ms") if isinstance(details, dict) else None
                success = bool(event.get("success", True))
                error_code = details.get("error_code") if isinstance(details, dict) else None
                entries.append(
                    {
                        "timestamp": str(event.get("timestamp") or "1970-01-01T00:00:00+00:00")[
                            :80
                        ],
                        "source": "audit",
                        "action": (
                            str(event["action"])[:160]
                            if isinstance(event.get("action"), str)
                            else None
                        ),
                        "level": "INFO" if success else "ERROR",
                        "message": (
                            "succeeded"
                            if success
                            else f"failed{f' ({error_code})' if isinstance(error_code, str) else ''}"
                        ),
                        "duration_ms": (
                            float(duration)
                            if isinstance(duration, (int, float)) and duration >= 0
                            else None
                        ),
                    }
                )
            return {
                "status": "ok",
                "summary": f"Read {len(entries)} bounded audit log entries",
                "error": None,
                "source": "audit",
                "entries": entries,
                "truncated": bool(page.truncated),
                "next_cursor": page.next_cursor,
            }
        binding = self._runtime_cursor_binding(
            source=source,
            action=action,
            only_failed=only_failed,
            min_duration_ms=min_duration_ms,
            start_time=start_time,
            end_time=end_time,
        )
        raw_lines = list(reversed(self._read_log(self._runtime_log_path, 1_000)))
        snapshot = hashlib.sha256(
            json.dumps(raw_lines, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        offset = 0
        if cursor is not None:
            parts = cursor.split(":", 3)
            if (
                len(parts) != 4
                or parts[0] != "runtime-v1"
                or parts[1] != binding
                or not parts[3].isdigit()
            ):
                raise ConfigError("Runtime log cursor is invalid or belongs to different filters")
            if parts[2] != snapshot:
                raise ConfigError("Runtime log cursor is stale because the log snapshot changed")
            offset = int(parts[3])
        matched: list[dict[str, Any]] = []
        for line in raw_lines:
            entry = self._runtime_entry(line)
            if action is not None and entry["action"] != action:
                continue
            if only_failed and str(entry["level"]).upper() not in {"ERROR", "CRITICAL"}:
                continue
            duration = entry["duration_ms"]
            if min_duration_ms is not None and (
                not isinstance(duration, (int, float)) or duration < min_duration_ms
            ):
                continue
            timestamp = self._parse_log_time(str(entry["timestamp"]), "runtime timestamp")
            if start is not None and (timestamp is None or timestamp < start):
                continue
            if end is not None and (timestamp is None or timestamp > end):
                continue
            matched.append(entry)
        selected = matched[offset : offset + limit]
        has_more = offset + len(selected) < len(matched)
        next_cursor = (
            f"runtime-v1:{binding}:{snapshot}:{offset + len(selected)}"
            if selected and has_more
            else None
        )
        return {
            "status": "ok",
            "summary": f"Read {len(selected)} bounded runtime log entries",
            "error": None,
            "source": "runtime",
            "entries": selected,
            "truncated": has_more or len(raw_lines) >= 1_000,
            "next_cursor": next_cursor,
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
            prefix = self._runtime_log_path.name + "."
            rotations: list[tuple[int, str]] = []
            if self._runtime_log_path.parent.is_dir():
                for candidate in self._runtime_log_path.parent.glob(prefix + "*"):
                    suffix = candidate.name[len(prefix) :]
                    if suffix.isdigit() and candidate.is_file():
                        rotations.append((int(suffix), candidate.name))
            files = [name for _, name in sorted(rotations, reverse=True)]
            if self._runtime_log_path.is_file():
                files.append(self._runtime_log_path.name)
            return {
                "status": "ok",
                "source": "runtime",
                "path": self._runtime_log_path.name,
                "files": files,
                "rotations_included": max(0, len(files) - 1),
                "lines": lines,
                "count": len(lines),
            }
        raise ConfigError("runtime_logs_read source must be 'audit' or 'runtime'")

    # -- writes --------------------------------------------------------------

    def repo_policy(
        self,
        repo_id: str,
        *,
        action: str,
        mutations: list[dict[str, Any]] | None = None,
        generated_paths: list[dict[str, Any]] | None = None,
        issue_writes: dict[str, Any] | None = None,
        preview_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview or apply one exact-state-bound v2 repository policy proposal."""

        if action == "preview":
            if preview_token is not None:
                raise ConfigError("repo_policy preview does not accept preview_token")
            normalized = self._normalize_policy_mutations(mutations or [])
            try:
                canonical_generated = (
                    [
                        rule.as_table()
                        for rule in parse_generated_paths(
                            generated_paths,
                            context=f"repositories.{repo_id}.generated_paths",
                        )
                    ]
                    if generated_paths is not None
                    else None
                )
            except ValueError as exc:
                raise ConfigError(f"Invalid generated_paths declaration: {exc}") from exc
            try:
                canonical_issue_writes = (
                    IssueWritePolicy.from_table(
                        issue_writes,
                        context=f"repositories.{repo_id}.issue_writes",
                    ).as_table()
                    if issue_writes is not None
                    else None
                )
            except IssueWritePolicyError as exc:
                raise ConfigError(f"Invalid issue_writes declaration: {exc}") from exc
            arguments = self._policy_apply_arguments(normalized)
            preview = self.repo_policy_apply(
                repo_id,
                **arguments,
                generated_paths=canonical_generated,
                issue_writes=canonical_issue_writes,
                dry_run=True,
            )
            current = self._store.current()
            if current is None:
                raise ConfigError("No accepted configuration generation")
            token = f"apr-{self._ids.new_hex(24)}"
            preview_payload: dict[str, object] = {
                "kind": "repo_policy_preview_v2",
                "repo_id": repo_id,
                "mutations": normalized,
                "generated_paths": canonical_generated,
                "issue_writes": canonical_issue_writes,
                "expected_generation": current.generation,
                "expected_source_sha256": sha256_text(self._store.read_source_text()),
            }
            try:
                self.pending.payloads.save(token, preview_payload)
            except (RepoForgeError, ValueError, TypeError) as exc:
                raise ConfigError(f"Cannot persist repo_policy preview_token: {exc}") from exc
            return {
                "status": "ok",
                "summary": f"Previewed repository policy for {repo_id}",
                "error": None,
                "repo_id": repo_id,
                "action": "preview",
                "result": "preview",
                "preview_token": token,
                "generation": None,
                "changes": normalized,
                "generated_paths": canonical_generated or [],
                "issue_writes": canonical_issue_writes,
                "operator_instruction": preview.get("safe_next_action"),
            }
        if action != "apply":
            raise ConfigError("repo_policy action must be 'preview' or 'apply'")
        if preview_token is None:
            raise ConfigError("repo_policy apply requires preview_token")
        if mutations or generated_paths is not None or issue_writes is not None:
            raise ConfigError("repo_policy apply accepts only the exact preview_token")
        try:
            stored_payload = self.pending.payloads.read(preview_token)
        except (RepoForgeError, ValueError) as exc:
            raise ConfigError(f"Invalid preview_token: {exc}") from exc
        if stored_payload is None or stored_payload.get("kind") != "repo_policy_preview_v2":
            raise ConfigError(f"Unknown preview_token: {preview_token}")
        if stored_payload.get("repo_id") != repo_id:
            raise ConfigError("preview_token is bound to a different repository")
        current = self._store.current()
        if current is None:
            raise ConfigError("No accepted configuration generation")
        if stored_payload.get("expected_generation") != current.generation or stored_payload.get(
            "expected_source_sha256"
        ) != sha256_text(self._store.read_source_text()):
            self.pending.payloads.delete(preview_token)
            raise ConfigError("repo_policy preview_token is stale")
        stored_mutations = stored_payload.get("mutations")
        if not isinstance(stored_mutations, list):
            raise ConfigError("repo_policy preview_token payload is corrupt")
        normalized = self._normalize_policy_mutations(stored_mutations)
        stored_generated = stored_payload.get("generated_paths")
        if stored_generated is not None and not isinstance(stored_generated, list):
            raise ConfigError("repo_policy preview_token generated_paths payload is corrupt")
        stored_issue_writes = stored_payload.get("issue_writes")
        if stored_issue_writes is not None and not isinstance(stored_issue_writes, dict):
            raise ConfigError("repo_policy preview_token issue_writes payload is corrupt")
        result = self.repo_policy_apply(
            repo_id,
            **self._policy_apply_arguments(normalized),
            generated_paths=stored_generated,
            issue_writes=stored_issue_writes,
            dry_run=False,
        )
        self.pending.payloads.delete(preview_token)
        legacy_status = str(result.get("status"))
        mapped = {
            "applied": "applied",
            "pending_approval": "pending_approval",
            "unchanged": "no_change",
        }.get(legacy_status)
        if mapped is None:
            raise ConfigError(f"repo_policy apply could not complete from preview: {legacy_status}")
        return {
            "status": "ok",
            "summary": f"Applied repository policy request for {repo_id}",
            "error": None,
            "repo_id": repo_id,
            "action": "apply",
            "result": mapped,
            "preview_token": None,
            "generation": result.get("generation"),
            "changes": normalized,
            "generated_paths": stored_generated or [],
            "issue_writes": stored_issue_writes,
            "operator_instruction": result.get("safe_next_action"),
        }

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
        remove_policy_overrides: list[str] | None = None,
        generated_paths: list[dict[str, Any]] | None = None,
        issue_writes: dict[str, Any] | None = None,
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
        try:
            resolved_generated_paths = (
                parse_generated_paths(
                    generated_paths,
                    context=f"repositories.{repo_id}.generated_paths",
                )
                if generated_paths is not None
                else source_item.generated_paths
            )
        except ValueError as exc:
            raise ConfigError(f"Invalid generated_paths declaration: {exc}") from exc
        try:
            resolved_issue_writes = (
                IssueWritePolicy.from_table(
                    issue_writes,
                    context=f"repositories.{repo_id}.issue_writes",
                )
                if issue_writes is not None
                else source_item.issue_writes
            )
        except IssueWritePolicyError as exc:
            raise ConfigError(f"Invalid issue_writes declaration: {exc}") from exc
        if (
            delta_patch.is_empty()
            and not policy_overrides
            and not remove_policy_overrides
            and generated_paths is None
            and issue_writes is None
        ):
            raise ConfigError("repo_policy_apply requires at least one change")
        merged_patch = source_item.policy_patch.merge(delta_patch)
        merged_overrides = dict(source_item.policy_overrides)
        merged_overrides.update(policy_overrides or {})
        for name in remove_policy_overrides or []:
            merged_overrides.pop(name, None)
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
                    item.risk_policy,
                    resolved_generated_paths,
                    resolved_issue_writes,
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
        document = apply_risk_policy(document, repo_id, source_item.risk_policy)
        document = apply_generated_paths(document, repo_id, resolved_generated_paths)
        document = apply_issue_write_policy(document, repo_id, resolved_issue_writes)
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

    @staticmethod
    def _normalize_policy_mutations(
        mutations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if len(mutations) > 100:
            raise ConfigError("repo_policy mutations supports at most 100 entries")
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for index, raw in enumerate(mutations):
            if not isinstance(raw, dict) or set(raw) != {
                "section",
                "name",
                "operation",
                "value",
            }:
                raise ConfigError(
                    f"repo_policy mutations[{index}] must contain section, name, operation, and value"
                )
            section = raw.get("section")
            name = raw.get("name")
            operation = raw.get("operation")
            value = raw.get("value")
            if section not in {"profile", "diagnostic", "formatter", "override"}:
                raise ConfigError(f"repo_policy mutations[{index}].section is invalid")
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 160
                or any(ord(character) < 32 for character in name)
            ):
                raise ConfigError(f"repo_policy mutations[{index}].name is invalid")
            target = (section, name)
            if target in seen:
                raise ConfigError(f"repo_policy mutations contains duplicate target: {target}")
            seen.add(target)
            if operation not in {"set", "remove"}:
                raise ConfigError(f"repo_policy mutations[{index}].operation is invalid")
            normalized_value: str | None = None
            if operation == "remove":
                if value is not None:
                    raise ConfigError(
                        f"repo_policy mutations[{index}].value must be null for remove"
                    )
            elif section == "override":
                if not isinstance(value, str) or len(value) > 20_000:
                    raise ConfigError(
                        f"repo_policy mutations[{index}].value must be a bounded string"
                    )
                normalized_value = value
            else:
                if not isinstance(value, str) or len(value) > 20_000:
                    raise ConfigError(f"repo_policy mutations[{index}].value must be bounded JSON")
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise ConfigError(
                        f"repo_policy mutations[{index}].value must be valid JSON"
                    ) from exc
                if not isinstance(decoded, dict):
                    raise ConfigError(f"repo_policy mutations[{index}].value must encode an object")
                normalized_value = json.dumps(
                    decoded,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            normalized.append(
                {
                    "section": section,
                    "name": name,
                    "operation": operation,
                    "value": normalized_value,
                }
            )
        return normalized

    @staticmethod
    def _policy_apply_arguments(mutations: list[dict[str, Any]]) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "set_profiles": [],
            "remove_profiles": [],
            "set_diagnostics": {},
            "remove_diagnostics": [],
            "set_formatters": {},
            "remove_formatters": [],
            "policy_overrides": {},
            "remove_policy_overrides": [],
        }
        for mutation in mutations:
            section = str(mutation["section"])
            name = str(mutation["name"])
            operation = str(mutation["operation"])
            value = mutation.get("value")
            if operation == "remove":
                key = "remove_policy_overrides" if section == "override" else f"remove_{section}s"
                arguments[key].append(name)
                continue
            if section == "override":
                arguments["policy_overrides"][name] = str(value)
                continue
            decoded = json.loads(str(value))
            if section == "profile":
                arguments["set_profiles"].append({"name": name, **decoded})
            else:
                arguments[f"set_{section}s"][name] = decoded
        return arguments

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
