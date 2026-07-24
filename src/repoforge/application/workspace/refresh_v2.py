"""Forge v2 workspace refresh planning and journaled conflict resolution."""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from ...domain.errors import ErrorCode, RepoForgeError, SecurityError, WorkspaceError
from ...domain.filesystem_transaction import (
    CreateFile,
    TransactionAction,
    TransactionPlan,
    WriteFile,
)
from ...domain.generated_paths import generated_path_rule_for, generated_paths_identity
from ...domain.operations import request_fingerprint
from ...domain.policy import assert_path_allowed, resolve_workspace_path, validate_branch
from ...domain.workspace import (
    WORKSPACE_REFRESH_RECEIPTS,
    VerificationReceipt,
    WorkspaceRecord,
    invalidate_workspace_refresh_receipts,
)
from ..context import ApplicationContext
from ..execution.requests import profile_execution_request
from ..file_transactions import open_file_transaction
from ..fingerprint_cache import prime_fingerprint, read_fingerprint
from ..idempotency import IdempotencyEffectBoundary
from ..outcome_receipts import execute_with_outcome_receipt
from .base_status import collect_workspace_base_status

_PLAN_TOKEN = re.compile(
    r"^refresh-v2:([0-9a-f]{40}(?:[0-9a-f]{24})?):([0-9a-f]{64}):([0-9a-f]{64})$"
)
_MAX_SEMANTIC_CONFLICTS = 100
_MAX_GENERATED_CONFLICTS = 1_000
_MAX_RESOLUTION_BYTES = 2_000_000
_MAX_EVIDENCE_BYTES = 60_000
_JOURNAL_SCHEMA_VERSION = 1


def _changed_line_count(before: str, after: str) -> int:
    matcher = difflib.SequenceMatcher(
        a=before.splitlines(),
        b=after.splitlines(),
        autojunk=False,
    )
    return sum(
        (before_end - before_start) + (after_end - after_start)
        for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes()
        if tag != "equal"
    )


@dataclass(frozen=True, slots=True)
class RefreshResolution:
    path: str
    content: str


@dataclass(frozen=True, slots=True)
class RefreshConflictEvidence:
    path: str
    kind: str
    base: str | None
    ours: str | None
    theirs: str | None
    content_truncated: bool
    next_action: str
    regeneration_command: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RefreshRegenerationReceipt:
    commands: tuple[tuple[str, ...], ...]
    generated_paths: tuple[str, ...]
    source_identity: str
    output_identity: str
    deterministic: bool = True


@dataclass(frozen=True, slots=True)
class RefreshChangeMetrics:
    changed_files: int = 0
    added_lines: int = 0
    deleted_lines: int = 0
    binary_files: int = 0
    total_current_bytes: int = 0


@dataclass(frozen=True, slots=True)
class WorkspaceRefreshV2Command:
    workspace_id: str
    action: str
    expected_head_sha: str
    expected_fingerprint: str
    plan_token: str | None = None
    resolutions: tuple[RefreshResolution, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceRefreshV2Result:
    status: str
    summary: str
    error: None
    workspace_id: str
    action: str
    result: str
    plan_hash: str
    plan_token: str | None
    target_base_sha: str
    head_sha: str
    workspace_fingerprint: str
    prediction_scope: str
    apply_blockers: tuple[str, ...]
    conflicts: tuple[RefreshConflictEvidence, ...]
    warnings: tuple[str, ...]
    changed_paths: tuple[str, ...]
    verify_selector: tuple[str, ...]
    invalidated_receipts: tuple[str, ...]
    transaction_id: str | None
    conflict_scope: str = "none"
    semantic_conflict_count: int = 0
    generated_conflict_count: int = 0
    semantic_conflict_paths: tuple[str, ...] = ()
    generated_conflict_paths: tuple[str, ...] = ()
    regeneration_receipts: tuple[RefreshRegenerationReceipt, ...] = ()
    source_change_metrics: RefreshChangeMetrics = RefreshChangeMetrics()
    generated_change_metrics: RefreshChangeMetrics = RefreshChangeMetrics()
    recreate_eligible: bool = False
    recreate_blockers: tuple[str, ...] = ()
    recommended_action: str = "refresh_preview"
    previous_head_sha: str | None = None

    def __post_init__(self) -> None:
        semantic_paths = tuple(item.path for item in self.conflicts if item.kind != "generated")
        generated_paths = tuple(item.path for item in self.conflicts if item.kind == "generated")
        if semantic_paths and generated_paths:
            scope = "mixed"
        elif semantic_paths:
            scope = "semantic"
        elif generated_paths:
            scope = "generated"
        else:
            scope = "none"
        object.__setattr__(self, "conflict_scope", scope)
        object.__setattr__(self, "semantic_conflict_count", len(semantic_paths))
        object.__setattr__(self, "generated_conflict_count", len(generated_paths))
        object.__setattr__(self, "semantic_conflict_paths", semantic_paths)
        object.__setattr__(self, "generated_conflict_paths", generated_paths)


@dataclass(frozen=True, slots=True)
class _RefreshPlan:
    workspace_id: str
    configured_base: str
    workspace_base_sha: str
    target_base_sha: str
    head_sha: str
    plan_hash: str
    conflicts: tuple[RefreshConflictEvidence, ...]
    already_integrated: bool
    apply_blockers: tuple[str, ...]
    recreate_eligible: bool
    recreate_blockers: tuple[str, ...]
    recommended_action: str
    verify_selector: tuple[str, ...]


FaultInjector = Callable[[str], None]


class _RefreshJournal:
    """Durably bind Git HEAD and workspace registry updates as one recoverable unit."""

    def __init__(
        self,
        ctx: ApplicationContext,
        workspace_id: str,
        workspace: Path,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self.ctx = ctx
        self.workspace_id = workspace_id
        self.workspace = workspace
        self.root = ctx.config.server.state_root / "workspace-refresh-transactions" / workspace_id
        self.manifest_path = self.root / "manifest.json"
        self._fault_injector = fault_injector

    def checkpoint(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    @staticmethod
    def _record_payload(record: WorkspaceRecord) -> dict[str, object]:
        return asdict(record)

    @staticmethod
    def _decode_record(raw: object) -> WorkspaceRecord:
        if not isinstance(raw, dict):
            raise WorkspaceError("Refresh transaction registry snapshot is invalid")
        payload = dict(raw)
        receipt_raw = payload.pop("last_verification", None)
        receipt = VerificationReceipt(**receipt_raw) if isinstance(receipt_raw, dict) else None
        try:
            return WorkspaceRecord(last_verification=receipt, **payload)
        except (TypeError, ValueError) as exc:
            raise WorkspaceError("Refresh transaction registry snapshot is invalid") from exc

    def _write_manifest(self, payload: dict[str, object]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.root / f"manifest.tmp-{os.getpid()}"
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.manifest_path)
        os.chmod(self.manifest_path, 0o600)
        self._fsync_dir(self.root)
        self._fsync_dir(self.root.parent)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def prepare(self, record: WorkspaceRecord, old_head_sha: str, target_sha: str) -> str:
        if self.manifest_path.exists():
            raise WorkspaceError("A workspace refresh transaction is already pending")
        transaction_id = self.ctx.ids.new_hex(32)
        self._write_manifest(
            {
                "schema_version": _JOURNAL_SCHEMA_VERSION,
                "phase": "prepared",
                "transaction_id": transaction_id,
                "workspace_id": self.workspace_id,
                "old_head_sha": old_head_sha,
                "target_sha": target_sha,
                "record": self._record_payload(record),
            }
        )
        self.checkpoint("after_manifest_prepared")
        return transaction_id

    def mark_committed(self) -> None:
        payload = self._read_manifest()
        payload["phase"] = "committed"
        self._write_manifest(payload)
        self.checkpoint("after_commit_marker")

    def recover_pending(self) -> str | None:
        if not self.manifest_path.exists():
            return None
        payload = self._read_manifest()
        phase = payload.get("phase")
        transaction_id = payload.get("transaction_id")
        if not isinstance(transaction_id, str):
            raise WorkspaceError("Refresh transaction id is invalid")
        if phase == "committed":
            self.purge()
            return transaction_id
        if phase != "prepared":
            raise WorkspaceError("Refresh transaction phase is invalid")
        old_head = payload.get("old_head_sha")
        if not isinstance(old_head, str):
            raise WorkspaceError("Refresh transaction old HEAD is invalid")
        record = self._decode_record(payload.get("record"))
        try:
            # A nested resolution transaction may have crashed while the merge was active.
            # Roll it back against that merge state before resetting HEAD, otherwise its
            # backups could later overwrite the restored reviewed tree.
            open_file_transaction(self.ctx, self.workspace).recover_pending()
            self.ctx.git.reset_hard(self.workspace, old_head)
            self.ctx.store.save(record)
            prime_fingerprint(
                self.ctx.fingerprint_cache,
                self.workspace_id,
                self.ctx.git,
                self.workspace,
            )
        except Exception as exc:
            raise WorkspaceError(
                "Workspace refresh recovery could not restore Git and registry state",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=False,
                safe_next_action="Inspect the isolated workspace and private refresh journal.",
            ) from exc
        self.purge()
        return transaction_id

    def rollback(self) -> None:
        self.recover_pending()

    def purge(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
            self._fsync_dir(self.root.parent)
            with contextlib.suppress(OSError):
                self.root.parent.rmdir()

    def _read_manifest(self) -> dict[str, object]:
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError("Refresh transaction manifest is unreadable") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != _JOURNAL_SCHEMA_VERSION:
            raise WorkspaceError("Refresh transaction manifest schema is invalid")
        if raw.get("workspace_id") != self.workspace_id:
            raise WorkspaceError("Refresh transaction belongs to another workspace")
        return raw


_RECREATE_JOURNAL_SCHEMA_VERSION = 1


class _RecreateJournal:
    """Durable roll-forward journal for clean workspace recreation."""

    def __init__(
        self,
        ctx: ApplicationContext,
        workspace_id: str,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self.ctx = ctx
        self.workspace_id = workspace_id
        self.root = ctx.config.server.state_root / "workspace-recreate-transactions" / workspace_id
        self.manifest_path = self.root / "manifest.json"
        self._fault_injector = fault_injector

    @property
    def pending(self) -> bool:
        return self.manifest_path.is_file()

    def checkpoint(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    def prepare(
        self,
        *,
        record: WorkspaceRecord,
        old_head_sha: str,
        target_base_sha: str,
        plan_hash: str,
        expected_fingerprint: str,
        request_digest: str,
        verify_selector: tuple[str, ...],
    ) -> str:
        if self.pending:
            raise WorkspaceError("A workspace recreate transaction is already pending")
        transaction_id = self.ctx.ids.new_hex(32)
        self._write_manifest(
            {
                "schema_version": _RECREATE_JOURNAL_SCHEMA_VERSION,
                "phase": "prepared",
                "transaction_id": transaction_id,
                "workspace_id": record.workspace_id,
                "repo_id": record.repo_id,
                "path": record.path,
                "branch": record.branch,
                "base": record.base,
                "old_head_sha": old_head_sha,
                "target_base_sha": target_base_sha,
                "plan_hash": plan_hash,
                "expected_fingerprint": expected_fingerprint,
                "request_digest": request_digest,
                "verify_selector": list(verify_selector),
                "task_slug": record.metadata.get("task_slug"),
                "workspace_create_idempotency": record.metadata.get("workspace_create_idempotency"),
                "issue_ids": record.metadata.get("issue_ids", []),
            }
        )
        self.checkpoint("after_recreate_manifest_prepared")
        return transaction_id

    def read(self) -> dict[str, object]:
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                "Workspace recreate transaction manifest is unreadable",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=False,
            ) from exc
        if not isinstance(raw, dict):
            raise WorkspaceError("Workspace recreate transaction manifest is invalid")
        if raw.get("schema_version") != _RECREATE_JOURNAL_SCHEMA_VERSION:
            raise WorkspaceError("Workspace recreate transaction manifest schema is invalid")
        if raw.get("workspace_id") != self.workspace_id:
            raise WorkspaceError("Workspace recreate transaction belongs to another workspace")
        required_strings = (
            "phase",
            "transaction_id",
            "repo_id",
            "path",
            "branch",
            "base",
            "old_head_sha",
            "target_base_sha",
            "plan_hash",
            "expected_fingerprint",
            "request_digest",
        )
        if any(not isinstance(raw.get(field), str) for field in required_strings):
            raise WorkspaceError("Workspace recreate transaction fields are invalid")
        selector = raw.get("verify_selector")
        if not isinstance(selector, list) or not all(isinstance(item, str) for item in selector):
            raise WorkspaceError("Workspace recreate verify selector is invalid")
        return raw

    def mark_phase(self, phase: str) -> None:
        payload = self.read()
        payload["phase"] = phase
        self._write_manifest(payload)

    def purge(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
            _RefreshJournal._fsync_dir(self.root.parent)
            with contextlib.suppress(OSError):
                self.root.parent.rmdir()

    def _write_manifest(self, payload: dict[str, object]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.root / f"manifest.tmp-{os.getpid()}"
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.manifest_path)
        os.chmod(self.manifest_path, 0o600)
        _RefreshJournal._fsync_dir(self.root)
        _RefreshJournal._fsync_dir(self.root.parent)


class WorkspaceRefreshV2:
    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        fault_injector: FaultInjector | None = None,
        file_fault_injector: FaultInjector | None = None,
        recreate_fault_injector: FaultInjector | None = None,
    ) -> None:
        self.ctx = ctx
        self._fault_injector = fault_injector
        self._file_fault_injector = file_fault_injector
        self._recreate_fault_injector = recreate_fault_injector

    def execute(self, command: WorkspaceRefreshV2Command) -> WorkspaceRefreshV2Result:
        if command.action not in {"preview", "apply", "recreate_from_latest_base"}:
            raise ValueError(
                "workspace_refresh action must be 'preview', 'apply', or "
                "'recreate_from_latest_base'"
            )
        if command.action == "recreate_from_latest_base":
            return self._execute_recreate(command)
        _, _, workspace = self.ctx.workspace(command.workspace_id)
        recovery_pending = (
            self.ctx.config.server.state_root
            / "workspace-refresh-transactions"
            / command.workspace_id
            / "manifest.json"
        ).is_file()
        boundary = IdempotencyEffectBoundary()
        audit_details = {
            "workspace_id": command.workspace_id,
            "action": command.action,
            "resolution_count": len(command.resolutions),
            "recovery_pending": recovery_pending,
        }

        def operation() -> WorkspaceRefreshV2Result:
            with self.ctx.locks.lock(command.workspace_id):
                journal = _RefreshJournal(
                    self.ctx,
                    command.workspace_id,
                    workspace,
                    fault_injector=self._fault_injector,
                )
                if recovery_pending:
                    boundary.begin()
                journal.recover_pending()
                record = self.ctx.store.load(command.workspace_id)
                repo = self.ctx.repository_for_workspace(record)
                validate_branch(record.branch, repo)
                if record.branch == record.base or record.branch in repo.protected_branches:
                    raise WorkspaceError("Protected or base branches cannot be refreshed")
                head = self.ctx.git.head_sha(workspace)
                fingerprint = read_fingerprint(
                    self.ctx.fingerprint_cache,
                    command.workspace_id,
                    self.ctx.git,
                    workspace,
                ).fingerprint
                if head != command.expected_head_sha or fingerprint != command.expected_fingerprint:
                    raise self._stale("workspace HEAD or fingerprint changed")
                plan = self._plan(record, repo, workspace, head, fingerprint)
                token = self._plan_token(plan, fingerprint)
                if command.action == "preview":
                    return self._preview_result(plan, token, fingerprint)
                if command.plan_token is None:
                    raise WorkspaceError("workspace_refresh apply requires plan_token")
                if command.plan_token != token:
                    raise self._stale("plan token does not match the current reviewed state")
                if plan.apply_blockers:
                    raise WorkspaceError(
                        "Workspace refresh apply is blocked: " + ", ".join(plan.apply_blockers),
                        safe_next_action="Restore or commit working-tree changes, then preview again.",
                    )
                resolutions = self._validate_resolutions(plan, command.resolutions, repo, workspace)
                if plan.already_integrated:
                    return WorkspaceRefreshV2Result(
                        "ok",
                        "The reviewed base is already integrated",
                        None,
                        command.workspace_id,
                        "apply",
                        "current",
                        plan.plan_hash,
                        None,
                        plan.target_base_sha,
                        head,
                        fingerprint,
                        "committed_head",
                        (),
                        plan.conflicts,
                        (),
                        (),
                        (),
                        (),
                        None,
                    )
                original_record = replace(record, metadata=dict(record.metadata))
                transaction_id = journal.prepare(record, head, plan.target_base_sha)
                try:
                    boundary.begin()
                    merged = self.ctx.git.begin_merge_no_ff(workspace, repo, plan.target_base_sha)
                    journal.checkpoint("after_merge_started")
                    if merged.status == "current":
                        journal.mark_committed()
                        journal.purge()
                        return self._preview_result(
                            plan, None, fingerprint, action="apply", result="current"
                        )
                    if tuple(sorted(merged.conflict_paths)) != tuple(
                        sorted(item.path for item in plan.conflicts)
                    ):
                        raise self._stale("merge conflict evidence changed during apply")
                    resolution_paths = tuple(sorted(resolutions))
                    if resolution_paths:
                        file_engine = open_file_transaction(
                            self.ctx,
                            workspace,
                            fault_injector=self._file_fault_injector,
                        )
                        file_engine.recover_pending()
                        actions: list[TransactionAction] = []
                        for relative_path in resolution_paths:
                            target = resolve_workspace_path(workspace, relative_path, repo)
                            data = resolutions[relative_path].encode("utf-8")
                            if target.exists():
                                if target.is_symlink() or not target.is_file():
                                    raise SecurityError(
                                        f"Refresh resolution target must be a regular file: {relative_path}"
                                    )
                                actions.append(WriteFile(relative_path, data, preserve_mode=True))
                            else:
                                actions.append(CreateFile(relative_path, data, 0o644))
                        file_engine.commit(TransactionPlan(tuple(actions)))
                        self.ctx.git.stage_paths(workspace, repo, resolution_paths)
                    journal.checkpoint("after_semantic_resolutions")
                    regenerated_paths, regeneration_receipts = self._regenerate_generated_conflicts(
                        plan,
                        repo,
                        workspace,
                    )
                    journal.checkpoint("after_regeneration_verified")
                    remaining = self.ctx.git.unmerged_paths(workspace, repo)
                    if remaining:
                        raise WorkspaceError(
                            "Refresh resolutions did not resolve every conflict: "
                            + ", ".join(remaining)
                        )
                    new_head = self.ctx.git.commit_merge(workspace)
                    invalidated = invalidate_workspace_refresh_receipts(record)
                    record.metadata["workspace_base_sha"] = plan.target_base_sha
                    record.metadata["last_refresh_target_sha"] = plan.target_base_sha
                    record.metadata["last_refresh_at"] = self.ctx.clock.now_iso()
                    record.metadata["refresh_commit_sha"] = new_head
                    self._persist_regeneration_receipts(
                        record,
                        regeneration_receipts,
                        refresh_commit_sha=new_head,
                        target_base_sha=plan.target_base_sha,
                        plan_hash=plan.plan_hash,
                    )
                    self.ctx.store.save(record)
                    changed_paths = tuple(
                        sorted(
                            set(
                                self.ctx.git.changed_paths_between(
                                    workspace,
                                    repo,
                                    head,
                                    new_head,
                                )
                            ).union(resolution_paths, regenerated_paths)
                        )
                    )
                    source_change_metrics, generated_change_metrics = self._change_metrics(
                        workspace,
                        repo,
                        head,
                        new_head,
                        changed_paths,
                    )
                    warnings: tuple[str, ...] = ()
                    final_fingerprint = prime_fingerprint(
                        self.ctx.fingerprint_cache,
                        command.workspace_id,
                        self.ctx.git,
                        workspace,
                    ).fingerprint
                    journal.mark_committed()
                    journal.purge()
                    return WorkspaceRefreshV2Result(
                        "ok",
                        f"Applied reviewed base refresh to {command.workspace_id}",
                        None,
                        command.workspace_id,
                        "apply",
                        "applied",
                        plan.plan_hash,
                        None,
                        plan.target_base_sha,
                        new_head,
                        final_fingerprint,
                        "committed_head",
                        (),
                        plan.conflicts,
                        warnings,
                        changed_paths,
                        changed_paths,
                        tuple(invalidated),
                        transaction_id,
                        regeneration_receipts=regeneration_receipts,
                        source_change_metrics=source_change_metrics,
                        generated_change_metrics=generated_change_metrics,
                    )
                except Exception as primary:
                    try:
                        journal.rollback()
                    except Exception as rollback_exc:
                        raise WorkspaceError(
                            "Workspace refresh failed and journal rollback also failed",
                            code=ErrorCode.STATE_PERSISTENCE_FAILED,
                            retryable=False,
                            safe_next_action="Inspect the isolated workspace and private refresh journal.",
                        ) from rollback_exc
                    boundary.rollback()
                    # Preserve the in-memory object for injected stores that retain references.
                    record.metadata.clear()
                    record.metadata.update(original_record.metadata)
                    record.last_verification = original_record.last_verification
                    if isinstance(primary, RepoForgeError):
                        raise
                    raise WorkspaceError(
                        "Workspace refresh apply failed; the reviewed Git and registry state was restored",
                        retryable=True,
                    ) from primary

        if command.action != "apply" and not recovery_pending:
            return self.ctx.audited("workspace_refresh", audit_details, operation)
        return execute_with_outcome_receipt(
            self.ctx,
            "workspace_refresh",
            asdict(command),
            operation,
            details=audit_details,
            serialize=asdict,
            effect_boundary=boundary,
        )

    def _execute_recreate(self, command: WorkspaceRefreshV2Command) -> WorkspaceRefreshV2Result:
        if command.resolutions:
            raise WorkspaceError(
                "recreate_from_latest_base does not accept conflict resolutions",
                unchanged_state=(
                    "The workspace worktree, local branch, registry record, remote branch, and pull "
                    "request were not modified.",
                ),
            )
        request_digest = request_fingerprint(asdict(command))
        journal = _RecreateJournal(
            self.ctx,
            command.workspace_id,
            fault_injector=self._recreate_fault_injector,
        )
        boundary = IdempotencyEffectBoundary()
        audit_details: dict[str, object] = {
            "workspace_id": command.workspace_id,
            "action": command.action,
            "recovery_pending": journal.pending,
        }

        def operation() -> WorkspaceRefreshV2Result:
            with self.ctx.locks.lock(command.workspace_id):
                if journal.pending:
                    boundary.begin()
                    return self._resume_recreate(journal, boundary)
                record = self.ctx.store.load(command.workspace_id)
                replay = self._replayed_recreate(record, request_digest)
                if replay is not None:
                    boundary.record_replay(reconciled=True)
                    audit_details["idempotent_replay"] = True
                    return replay
                repo = self.ctx.repository_for_workspace(record)
                workspace = Path(record.path)
                validate_branch(record.branch, repo)
                if record.branch == record.base or record.branch in repo.protected_branches:
                    raise self._recreate_refusal(("protected_or_base_branch",))
                if not workspace.is_dir() or not self.ctx.git.is_worktree(workspace):
                    raise WorkspaceError(
                        "Workspace worktree is unavailable for recreate preview binding",
                        code=ErrorCode.STALE_STATE,
                        retryable=True,
                        unchanged_state=(
                            "The workspace registry, local branch, remote branch, and pull request "
                            "were not modified.",
                        ),
                    )
                head = self.ctx.git.head_sha(workspace)
                fingerprint = read_fingerprint(
                    self.ctx.fingerprint_cache,
                    command.workspace_id,
                    self.ctx.git,
                    workspace,
                ).fingerprint
                if head != command.expected_head_sha or fingerprint != command.expected_fingerprint:
                    raise self._stale("workspace HEAD or fingerprint changed")
                plan = self._plan(record, repo, workspace, head, fingerprint)
                token = self._plan_token(plan, fingerprint)
                if command.plan_token is None:
                    raise WorkspaceError(
                        "recreate_from_latest_base requires a reviewed plan_token",
                        unchanged_state=(
                            "The workspace worktree, local branch, registry record, remote branch, "
                            "and pull request were not modified.",
                        ),
                    )
                if command.plan_token != token:
                    raise self._stale("plan token does not match the current reviewed state")
                if not plan.recreate_eligible:
                    self.ctx.record_metric(
                        "workspace_refresh.recreate_refusal",
                        success=False,
                        duration_ms=0.0,
                        error_code="RECREATE_INELIGIBLE",
                    )
                    raise self._recreate_refusal(plan.recreate_blockers)
                boundary.begin()
                journal.prepare(
                    record=record,
                    old_head_sha=head,
                    target_base_sha=plan.target_base_sha,
                    plan_hash=plan.plan_hash,
                    expected_fingerprint=fingerprint,
                    request_digest=request_digest,
                    verify_selector=plan.verify_selector,
                )
                return self._resume_recreate(journal, boundary)

        return execute_with_outcome_receipt(
            self.ctx,
            "workspace_refresh",
            asdict(command),
            operation,
            details=audit_details,
            serialize=asdict,
            effect_boundary=boundary,
            deferred_exceptions=(KeyboardInterrupt,),
        )

    def _resume_recreate(
        self,
        journal: _RecreateJournal,
        boundary: IdempotencyEffectBoundary,
    ) -> WorkspaceRefreshV2Result:
        payload = journal.read()
        record = self.ctx.store.load(journal.workspace_id)
        repo = self.ctx.repository_for_workspace(record)
        workspace = Path(record.path)
        branch = str(payload["branch"])
        target_sha = str(payload["target_base_sha"])
        old_head = str(payload["old_head_sha"])
        transaction_id = str(payload["transaction_id"])
        plan_hash = str(payload["plan_hash"])
        request_digest = str(payload["request_digest"])
        raw_verify_selector = payload["verify_selector"]
        if not isinstance(raw_verify_selector, list) or not all(
            isinstance(item, str) for item in raw_verify_selector
        ):
            raise WorkspaceError(
                "Workspace recreate transaction verify selector is invalid",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=False,
            )
        verify_selector = tuple(str(item) for item in raw_verify_selector)
        if (
            record.repo_id != payload["repo_id"]
            or record.path != payload["path"]
            or record.branch != branch
            or record.base != payload["base"]
            or record.metadata.get("task_slug") != payload.get("task_slug")
            or record.metadata.get("workspace_create_idempotency")
            != payload.get("workspace_create_idempotency")
            or list(record.metadata.get("issue_ids", [])) != payload.get("issue_ids")
        ):
            raise WorkspaceError(
                "Workspace recreate task binding changed while recovery was pending",
                code=ErrorCode.STALE_STATE,
                retryable=False,
            )
        validate_branch(branch, repo)

        if workspace.exists():
            if not self.ctx.git.is_worktree(workspace):
                raise WorkspaceError(
                    "Workspace recreate destination exists but is not the registered Git worktree",
                    code=ErrorCode.STATE_PERSISTENCE_FAILED,
                    retryable=False,
                )
            current_branch = self.ctx.git.current_branch(workspace)
            current_head = self.ctx.git.head_sha(workspace)
            if current_branch != branch:
                raise WorkspaceError(
                    "Workspace recreate destination is bound to a different local branch",
                    code=ErrorCode.STATE_PERSISTENCE_FAILED,
                    retryable=False,
                )
            if current_head == old_head:
                self.ctx.git.ensure_clean(workspace, context="workspace recreate")
                self.ctx.git.remove_worktree(repo, workspace, branch, True)
                journal.checkpoint("after_recreate_worktree_removed")
            elif current_head != target_sha:
                raise WorkspaceError(
                    "Workspace recreate destination has an unexpected HEAD",
                    code=ErrorCode.STATE_PERSISTENCE_FAILED,
                    retryable=False,
                    details={
                        "old_head_sha": old_head,
                        "target_base_sha": target_sha,
                        "actual_head_sha": current_head,
                    },
                )

        if not workspace.exists():
            self.ctx.git.delete_local_branch(repo, branch)
            journal.mark_phase("worktree_removed")
            created_head = self.ctx.git.create_worktree(repo, workspace, branch, target_sha)
            if created_head != target_sha:
                raise WorkspaceError(
                    "Recreated worktree did not resolve to the reviewed target base",
                    code=ErrorCode.STATE_PERSISTENCE_FAILED,
                    retryable=False,
                )
            journal.checkpoint("after_recreate_worktree_created")
            journal.mark_phase("worktree_created")

        final_head = self.ctx.git.head_sha(workspace)
        if final_head != target_sha or self.ctx.git.current_branch(workspace) != branch:
            raise WorkspaceError(
                "Recreated workspace identity does not match the reviewed target",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=False,
            )
        final_fingerprint = prime_fingerprint(
            self.ctx.fingerprint_cache,
            record.workspace_id,
            self.ctx.git,
            workspace,
        ).fingerprint
        invalidate_workspace_refresh_receipts(record)
        record.metadata["workspace_base_sha"] = target_sha
        record.metadata["last_recreate_target_sha"] = target_sha
        record.metadata["last_recreate_at"] = self.ctx.clock.now_iso()
        record.metadata["last_recreate_transaction_id"] = transaction_id
        record.metadata["last_recreate_plan_hash"] = plan_hash
        record.metadata["last_recreate_previous_head_sha"] = old_head
        record.metadata["last_recreate_request_fingerprint"] = request_digest
        record.metadata["last_recreate_workspace_fingerprint"] = final_fingerprint
        record.metadata["last_recreate_verify_selector"] = list(verify_selector)
        self.ctx.store.save(record)
        journal.checkpoint("after_recreate_registry_saved")
        journal.mark_phase("registry_saved")

        result = WorkspaceRefreshV2Result(
            status="ok",
            summary=f"Recreated clean workspace {record.workspace_id} from the latest base",
            error=None,
            workspace_id=record.workspace_id,
            action="recreate_from_latest_base",
            result="recreated",
            plan_hash=plan_hash,
            plan_token=None,
            target_base_sha=target_sha,
            head_sha=final_head,
            workspace_fingerprint=final_fingerprint,
            prediction_scope="latest_base_recreate",
            apply_blockers=(),
            conflicts=(),
            warnings=(),
            changed_paths=verify_selector,
            verify_selector=verify_selector,
            invalidated_receipts=tuple(WORKSPACE_REFRESH_RECEIPTS),
            transaction_id=transaction_id,
            recreate_eligible=True,
            recreate_blockers=(),
            recommended_action="continue",
            previous_head_sha=old_head,
        )
        boundary.record_result(result)
        journal.purge()
        self.ctx.record_metric(
            "workspace_refresh.safe_recreate",
            success=True,
            duration_ms=0.0,
            error_code=None,
        )
        return result

    def _replayed_recreate(
        self,
        record: WorkspaceRecord,
        request_digest: str,
    ) -> WorkspaceRefreshV2Result | None:
        if record.metadata.get("last_recreate_request_fingerprint") != request_digest:
            return None
        transaction_id = record.metadata.get("last_recreate_transaction_id")
        target_sha = record.metadata.get("last_recreate_target_sha")
        old_head = record.metadata.get("last_recreate_previous_head_sha")
        plan_hash = record.metadata.get("last_recreate_plan_hash")
        final_fingerprint = record.metadata.get("last_recreate_workspace_fingerprint")
        raw_selector = record.metadata.get("last_recreate_verify_selector")
        if (
            not isinstance(transaction_id, str)
            or not isinstance(target_sha, str)
            or not isinstance(old_head, str)
            or not isinstance(plan_hash, str)
            or not isinstance(final_fingerprint, str)
            or not isinstance(raw_selector, list)
            or not all(isinstance(item, str) for item in raw_selector)
        ):
            raise WorkspaceError(
                "Persisted workspace recreate replay evidence is incomplete",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=False,
            )
        workspace = Path(record.path)
        if (
            not workspace.is_dir()
            or not self.ctx.git.is_worktree(workspace)
            or self.ctx.git.head_sha(workspace) != target_sha
            or self.ctx.git.current_branch(workspace) != record.branch
        ):
            raise WorkspaceError(
                "Persisted workspace recreate result no longer matches the worktree",
                code=ErrorCode.STALE_STATE,
                retryable=True,
            )
        return WorkspaceRefreshV2Result(
            status="ok",
            summary=f"Replayed authoritative workspace recreate result for {record.workspace_id}",
            error=None,
            workspace_id=record.workspace_id,
            action="recreate_from_latest_base",
            result="recreated",
            plan_hash=plan_hash,
            plan_token=None,
            target_base_sha=target_sha,
            head_sha=target_sha,
            workspace_fingerprint=final_fingerprint,
            prediction_scope="latest_base_recreate",
            apply_blockers=(),
            conflicts=(),
            warnings=(),
            changed_paths=tuple(str(item) for item in raw_selector),
            verify_selector=tuple(str(item) for item in raw_selector),
            invalidated_receipts=tuple(WORKSPACE_REFRESH_RECEIPTS),
            transaction_id=transaction_id,
            recreate_eligible=True,
            recreate_blockers=(),
            recommended_action="continue",
            previous_head_sha=old_head,
        )

    @staticmethod
    def _recreate_refusal(blockers: tuple[str, ...]) -> WorkspaceError:
        rendered = ", ".join(blockers) if blockers else "eligibility_not_proven"
        return WorkspaceError(
            "Workspace recreate is blocked: " + rendered,
            code=ErrorCode.WORKSPACE_INVALID,
            retryable=False,
            details={"recreate_blockers": list(blockers)},
            safe_next_action=(
                "Preserve or publish the current work, remove external/PR bindings, or use a "
                "reviewed workspace_refresh merge preview instead."
            ),
            unchanged_state=(
                "The workspace worktree, local branch, registry record, remote branch, and pull "
                "request were not modified.",
            ),
        )

    def _plan(
        self,
        record: WorkspaceRecord,
        repo: Any,
        workspace: Path,
        head: str,
        fingerprint: str,
    ) -> _RefreshPlan:
        base = collect_workspace_base_status(
            self.ctx,
            record,
            repo,
            workspace,
            fetch_remote=True,
        )
        current_head = self.ctx.git.head_sha(workspace)
        current_fingerprint = read_fingerprint(
            self.ctx.fingerprint_cache,
            record.workspace_id,
            self.ctx.git,
            workspace,
        ).fingerprint
        if current_head != head or current_fingerprint != fingerprint:
            raise self._stale("workspace changed while computing refresh plan")
        if not base.remote_available or base.remote_base_sha is None:
            raise WorkspaceError(
                "REMOTE_BASE_UNAVAILABLE: latest remote base cannot be reviewed",
                code=ErrorCode.COMMAND_FAILED,
                retryable=True,
                safe_next_action="Restore remote connectivity and create a new refresh preview.",
            )
        merge = self.ctx.git.preview_merge(workspace, repo, base.remote_base_sha)
        conflicts = self._conflict_evidence(
            workspace,
            repo,
            merge.merge_base_sha,
            head,
            base.remote_base_sha,
            merge.conflict_paths,
        )
        semantic_count = sum(item.kind != "generated" for item in conflicts)
        generated_count = sum(item.kind == "generated" for item in conflicts)
        if semantic_count > _MAX_SEMANTIC_CONFLICTS:
            raise WorkspaceError(
                f"Refresh preview exceeds the {_MAX_SEMANTIC_CONFLICTS}-semantic-conflict limit"
            )
        if generated_count > _MAX_GENERATED_CONFLICTS:
            raise WorkspaceError(
                f"Refresh preview exceeds the {_MAX_GENERATED_CONFLICTS}-generated-conflict limit"
            )
        recreate_payload: dict[str, object] = {
            "eligible": base.recreate_eligible,
            "blockers": base.recreate_blockers,
            "recommended_action": base.recommended_action,
            "task_slug": record.metadata.get("task_slug"),
            "workspace_create_idempotency": record.metadata.get("workspace_create_idempotency"),
            "issue_ids": record.metadata.get("issue_ids", []),
            "published_state": base.published_state,
            "external_write_count": record.metadata.get("external_write_count", 0),
        }
        payload: dict[str, object] = {
            "configured_base": record.base,
            "workspace_base_sha": base.workspace_base_sha,
            "target_base_sha": base.remote_base_sha,
            "head_sha": head,
            "strategy": "merge_no_ff",
            "conflicts": [
                {
                    "path": item.path,
                    "kind": item.kind,
                    "base": item.base,
                    "ours": item.ours,
                    "theirs": item.theirs,
                    "content_truncated": item.content_truncated,
                    "regeneration_command": list(item.regeneration_command),
                }
                for item in conflicts
            ],
            "version": 2,
            "workspace_id": record.workspace_id,
            "recreate": recreate_payload,
        }
        recreate_identity = {
            key: value
            for key, value in recreate_payload.items()
            if key not in {"eligible", "blockers", "recommended_action"}
        }
        plan_identity = {**payload, "recreate": recreate_identity}
        plan_hash = hashlib.sha256(
            json.dumps(
                plan_identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        clean = not bool(self.ctx.git.status_porcelain(workspace).strip())
        blockers = () if clean else ("working_tree_not_clean",)
        return _RefreshPlan(
            record.workspace_id,
            record.base,
            base.workspace_base_sha,
            base.remote_base_sha,
            head,
            plan_hash,
            conflicts,
            merge.already_integrated,
            blockers,
            base.recreate_eligible,
            tuple(base.recreate_blockers),
            base.recommended_action,
            tuple(base.verify_selector),
        )

    def _conflict_evidence(
        self,
        workspace: Path,
        repo: Any,
        merge_base_sha: str,
        head_sha: str,
        target_sha: str,
        paths: tuple[str, ...],
    ) -> tuple[RefreshConflictEvidence, ...]:
        remaining = _MAX_EVIDENCE_BYTES
        evidence: list[RefreshConflictEvidence] = []
        for raw_path in sorted(paths):
            path = assert_path_allowed(raw_path, repo)
            texts: list[str | None] = []
            truncated = False
            for snapshot in (merge_base_sha, head_sha, target_sha):
                text, used, item_truncated = self._snapshot_text(
                    workspace,
                    repo,
                    snapshot,
                    path,
                    remaining,
                )
                texts.append(text)
                remaining -= used
                truncated = truncated or item_truncated
            base, ours, theirs = texts
            rule = generated_path_rule_for(repo.generated_paths, path)
            if rule is not None:
                kind = "generated"
                command = rule.regeneration_command
                next_action = (
                    "Merge source inputs, then regenerate via `"
                    + " ".join(command)
                    + "`; do not hand-merge this generated path."
                )
            else:
                command = ()
                if base is None and ours is not None and theirs is not None:
                    kind = "add_add"
                elif ours is None or theirs is None:
                    kind = "delete_modify"
                else:
                    kind = "content"
                next_action = "Provide one reviewed resolution for this path."
            evidence.append(
                RefreshConflictEvidence(
                    path,
                    kind,
                    base,
                    ours,
                    theirs,
                    truncated,
                    next_action,
                    command,
                )
            )
        return tuple(evidence)

    def _snapshot_text(
        self,
        workspace: Path,
        repo: Any,
        snapshot: str,
        path: str,
        remaining: int,
    ) -> tuple[str | None, int, bool]:
        try:
            blob = self.ctx.git.read_snapshot_blob(workspace, repo, snapshot, path)
        except RepoForgeError as exc:
            if exc.code is ErrorCode.NOT_FOUND:
                return None, 0, False
            raise
        if b"\x00" in blob.data:
            return None, 0, True
        try:
            text = blob.data.decode("utf-8")
        except UnicodeDecodeError:
            return None, 0, True
        if remaining <= 0:
            return "", 0, bool(text)
        encoded = text.encode("utf-8")
        if len(encoded) <= remaining:
            return text, len(encoded), False
        clipped = encoded[:remaining]
        while clipped:
            try:
                return clipped.decode("utf-8"), len(clipped), True
            except UnicodeDecodeError:
                clipped = clipped[:-1]
        return "", 0, True

    @staticmethod
    def _plan_token(plan: _RefreshPlan, fingerprint: str) -> str:
        binding = hashlib.sha256(
            json.dumps(
                {
                    "workspace_id": plan.workspace_id,
                    "head_sha": plan.head_sha,
                    "workspace_fingerprint": fingerprint,
                    "target_base_sha": plan.target_base_sha,
                    "plan_hash": plan.plan_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return f"refresh-v2:{plan.target_base_sha}:{plan.plan_hash}:{binding}"

    @staticmethod
    def _validate_resolutions(
        plan: _RefreshPlan,
        resolutions: tuple[RefreshResolution, ...],
        repo: Any,
        workspace: Path,
    ) -> dict[str, str]:
        expected = {item.path for item in plan.conflicts if item.kind != "generated"}
        provided: dict[str, str] = {}
        resulting_bytes = 0
        changed_lines = 0
        for resolution in resolutions:
            path = assert_path_allowed(resolution.path, repo)
            target = resolve_workspace_path(workspace, path, repo)
            if path in provided:
                raise WorkspaceError(f"Provide exactly one resolution for conflict path: {path}")
            if "\x00" in resolution.content:
                raise SecurityError(f"Refresh resolution contains NUL bytes: {path}")
            encoded = resolution.content.encode("utf-8")
            if len(encoded) > min(repo.max_total_changed_bytes, _MAX_RESOLUTION_BYTES):
                raise WorkspaceError(f"Refresh resolution exceeds the per-file byte limit: {path}")
            before = ""
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise SecurityError(f"Refresh resolution target must be a regular file: {path}")
                try:
                    before = target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise SecurityError(
                        f"Refresh resolution target must be UTF-8 text: {path}"
                    ) from exc
            resulting_bytes += len(encoded)
            changed_lines += _changed_line_count(before, resolution.content)
            provided[path] = resolution.content
        if set(provided) != expected:
            missing = sorted(expected - set(provided))
            extra = sorted(set(provided) - expected)
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if extra:
                details.append("unexpected=" + ",".join(extra))
            raise WorkspaceError(
                "Provide exactly one resolution for every conflict path"
                + (": " + "; ".join(details) if details else "")
            )
        violations: list[str] = []
        if len(provided) > repo.max_changed_files:
            violations.append(f"changed files {len(provided)} > {repo.max_changed_files}")
        if changed_lines > repo.max_diff_lines:
            violations.append(f"diff lines {changed_lines} > {repo.max_diff_lines}")
        if resulting_bytes > repo.max_total_changed_bytes:
            violations.append(
                f"changed file bytes {resulting_bytes} > {repo.max_total_changed_bytes}"
            )
        if violations:
            raise WorkspaceError(
                "Change budget exceeded: "
                + "; ".join(violations)
                + ". Split the task or raise the explicit repository limits in config."
            )
        return provided

    def _regenerate_generated_conflicts(
        self,
        plan: _RefreshPlan,
        repo: Any,
        workspace: Path,
    ) -> tuple[tuple[str, ...], tuple[RefreshRegenerationReceipt, ...]]:
        generated_conflicts = tuple(item for item in plan.conflicts if item.kind == "generated")
        if not generated_conflicts:
            return (), ()
        commands = tuple(sorted({item.regeneration_command for item in generated_conflicts}))
        if any(not command for command in commands):
            raise WorkspaceError("Generated refresh conflict has no reviewed regeneration command")
        command_set = set(commands)
        generated_rules = tuple(
            rule for rule in repo.generated_paths if rule.regeneration_command in command_set
        )
        source_paths = tuple(
            sorted(
                path
                for path in self.ctx.git.changed_paths(workspace, repo)
                if generated_path_rule_for(repo.generated_paths, path) is None
            )
        )
        source_identity = self._path_identity(
            workspace,
            repo,
            source_paths,
            binding={
                "workspace_id": plan.workspace_id,
                "head_sha": plan.head_sha,
                "target_base_sha": plan.target_base_sha,
            },
        )
        request = profile_execution_request(
            workspace_id=plan.workspace_id,
            workspace_root=workspace,
            command_cwd=workspace,
            commands=commands,
            working_directory_policy=".",
            timeout_seconds=repo.adhoc_timeout_seconds,
            output_limit=self.ctx.config.server.max_tool_output_chars,
        )

        def run_commands(session: Any) -> None:
            for command in commands:
                session.execute(command)

        def observed_generated_paths() -> tuple[str, ...]:
            changed_paths = self.ctx.git.changed_paths(workspace, repo)
            return tuple(
                sorted(
                    path
                    for path in changed_paths
                    if any(rule.matches(path) for rule in generated_rules)
                )
            )

        conflict_paths = {item.path for item in generated_conflicts}
        with self.ctx.execution.prepare(request) as session:
            run_commands(session)
            regenerated_paths = observed_generated_paths()
            self._validate_regenerated_paths(
                workspace,
                repo,
                regenerated_paths,
                conflict_paths,
            )
            first_output_identity = generated_paths_identity(workspace, regenerated_paths)
            if first_output_identity is None:
                raise SecurityError("Cannot compute regenerated output identity")
            self.ctx.git.stage_paths(workspace, repo, regenerated_paths)
            self._assert_no_unstaged_regeneration_effects(workspace)

            run_commands(session)
            second_paths = observed_generated_paths()
            self._validate_regenerated_paths(workspace, repo, second_paths, conflict_paths)
            second_output_identity = generated_paths_identity(workspace, second_paths)
            if second_output_identity is None:
                raise SecurityError("Cannot compute regenerated output identity")
            if second_paths != regenerated_paths or second_output_identity != first_output_identity:
                raise WorkspaceError(
                    "Regeneration is nondeterministic: first output "
                    f"{first_output_identity}, second output {second_output_identity}"
                )
            self.ctx.git.stage_paths(workspace, repo, second_paths)
            self._assert_no_unstaged_regeneration_effects(workspace)

        receipt = RefreshRegenerationReceipt(
            commands=commands,
            generated_paths=regenerated_paths,
            source_identity=source_identity,
            output_identity=first_output_identity,
        )
        return regenerated_paths, (receipt,)

    @staticmethod
    def _persist_regeneration_receipts(
        record: WorkspaceRecord,
        receipts: tuple[RefreshRegenerationReceipt, ...],
        *,
        refresh_commit_sha: str,
        target_base_sha: str,
        plan_hash: str,
    ) -> None:
        if not receipts:
            return
        existing = record.metadata.get("generated_path_receipts_v1", ())
        retained = list(existing) if isinstance(existing, (list, tuple)) else []
        for receipt in receipts:
            retained.append(
                {
                    "schema_version": 1,
                    "commands": [list(command) for command in receipt.commands],
                    "generated_paths": list(receipt.generated_paths),
                    "source_identity": receipt.source_identity,
                    "output_identity": receipt.output_identity,
                    "deterministic": receipt.deterministic,
                    "refresh_commit_sha": refresh_commit_sha,
                    "target_base_sha": target_base_sha,
                    "plan_hash": plan_hash,
                }
            )
        record.metadata["generated_path_receipts_v1"] = retained[-64:]

    def _validate_regenerated_paths(
        self,
        workspace: Path,
        repo: Any,
        regenerated_paths: tuple[str, ...],
        conflict_paths: set[str],
    ) -> None:
        if not conflict_paths.issubset(regenerated_paths):
            missing = sorted(conflict_paths - set(regenerated_paths))
            raise WorkspaceError(
                "Regeneration did not produce every generated conflict path: " + ", ".join(missing)
            )
        for path in regenerated_paths:
            target = resolve_workspace_path(workspace, path, repo)
            if target.is_symlink() or not target.is_file():
                raise SecurityError(f"Regenerated output must be a regular file: {path}")

    def _assert_no_unstaged_regeneration_effects(self, workspace: Path) -> None:
        unstaged = self._unstaged_paths(self.ctx.git.status_porcelain(workspace))
        if unstaged:
            raise SecurityError(
                "Regeneration command left undeclared or unstaged changes: " + ", ".join(unstaged)
            )

    @staticmethod
    def _path_identity(
        workspace: Path,
        repo: Any,
        paths: tuple[str, ...],
        *,
        binding: dict[str, str] | None = None,
    ) -> str:
        entries: list[dict[str, str]] = []
        for path in paths:
            target = resolve_workspace_path(workspace, path, repo)
            if target.is_symlink():
                raise SecurityError(f"Identity path must not be a symlink: {path}")
            if not target.exists():
                entries.append({"path": path, "state": "missing"})
                continue
            if not target.is_file():
                raise SecurityError(f"Identity path must be a regular file: {path}")
            entries.append(
                {
                    "path": path,
                    "state": "present",
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            )
        payload = {"binding": binding or {}, "paths": entries}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _change_metrics(
        self,
        workspace: Path,
        repo: Any,
        old_head: str,
        new_head: str,
        changed_paths: tuple[str, ...],
    ) -> tuple[RefreshChangeMetrics, RefreshChangeMetrics]:
        buckets: dict[str, dict[str, int]] = {
            "source": {
                "changed_files": 0,
                "added_lines": 0,
                "deleted_lines": 0,
                "binary_files": 0,
                "total_current_bytes": 0,
            },
            "generated": {
                "changed_files": 0,
                "added_lines": 0,
                "deleted_lines": 0,
                "binary_files": 0,
                "total_current_bytes": 0,
            },
        }
        for path in changed_paths:
            bucket_name = (
                "generated"
                if generated_path_rule_for(repo.generated_paths, path) is not None
                else "source"
            )
            bucket = buckets[bucket_name]
            before = self._snapshot_bytes(workspace, repo, old_head, path)
            after = self._snapshot_bytes(workspace, repo, new_head, path)
            bucket["changed_files"] += 1
            bucket["total_current_bytes"] += len(after or b"")
            line_metrics = self._line_metrics(before, after)
            if line_metrics is None:
                bucket["binary_files"] += 1
            else:
                added, deleted = line_metrics
                bucket["added_lines"] += added
                bucket["deleted_lines"] += deleted
        return RefreshChangeMetrics(**buckets["source"]), RefreshChangeMetrics(
            **buckets["generated"]
        )

    def _snapshot_bytes(
        self,
        workspace: Path,
        repo: Any,
        snapshot: str,
        path: str,
    ) -> bytes | None:
        try:
            return self.ctx.git.read_snapshot_blob(workspace, repo, snapshot, path).data
        except RepoForgeError as exc:
            if exc.code is ErrorCode.NOT_FOUND:
                return None
            raise

    @staticmethod
    def _line_metrics(before: bytes | None, after: bytes | None) -> tuple[int, int] | None:
        values = (before or b"", after or b"")
        if any(b"\x00" in value for value in values):
            return None
        try:
            before_lines = values[0].decode("utf-8").splitlines()
            after_lines = values[1].decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return None
        added = 0
        deleted = 0
        matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
        for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
            if tag in {"replace", "delete"}:
                deleted += before_end - before_start
            if tag in {"replace", "insert"}:
                added += after_end - after_start
        return added, deleted

    @staticmethod
    def _unstaged_paths(status: str) -> tuple[str, ...]:
        paths: list[str] = []
        for line in status.splitlines():
            if line.startswith("?? ") or (len(line) >= 4 and line[1] != " "):
                paths.append(line[3:])
        return tuple(sorted(set(paths)))

    @staticmethod
    def _preview_result(
        plan: _RefreshPlan,
        token: str | None,
        fingerprint: str,
        *,
        action: str = "preview",
        result: str = "preview",
    ) -> WorkspaceRefreshV2Result:
        return WorkspaceRefreshV2Result(
            "ok",
            f"Reviewed committed-HEAD refresh plan for {plan.workspace_id}",
            None,
            plan.workspace_id,
            action,
            result,
            plan.plan_hash,
            token,
            plan.target_base_sha,
            plan.head_sha,
            fingerprint,
            "committed_head",
            plan.apply_blockers,
            plan.conflicts,
            (),
            (),
            (),
            tuple(WORKSPACE_REFRESH_RECEIPTS),
            None,
            recreate_eligible=plan.recreate_eligible,
            recreate_blockers=plan.recreate_blockers,
            recommended_action=plan.recommended_action,
        )

    @staticmethod
    def _stale(message: str) -> WorkspaceError:
        return WorkspaceError(
            f"STALE_REFRESH_PREVIEW: {message}",
            code=ErrorCode.STALE_STATE,
            retryable=True,
            safe_next_action="Read workspace status and create a new refresh preview.",
            unchanged_state=("The workspace branch and working tree were not refreshed.",),
        )
