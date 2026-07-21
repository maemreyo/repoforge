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
from ...domain.generated_paths import generated_path_rule_for
from ...domain.policy import assert_path_allowed, resolve_workspace_path, validate_branch
from ...domain.workspace import (
    WORKSPACE_REFRESH_RECEIPTS,
    VerificationReceipt,
    WorkspaceRecord,
    invalidate_workspace_refresh_receipts,
)
from ..context import ApplicationContext
from ..file_transactions import open_file_transaction
from ..fingerprint_cache import prime_fingerprint, read_fingerprint
from ..idempotency import IdempotencyEffectBoundary
from ..outcome_receipts import execute_with_outcome_receipt
from .base_status import collect_workspace_base_status

_PLAN_TOKEN = re.compile(
    r"^refresh-v2:([0-9a-f]{40}(?:[0-9a-f]{24})?):([0-9a-f]{64}):([0-9a-f]{64})$"
)
_MAX_CONFLICTS = 100
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


class WorkspaceRefreshV2:
    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        fault_injector: FaultInjector | None = None,
        file_fault_injector: FaultInjector | None = None,
    ) -> None:
        self.ctx = ctx
        self._fault_injector = fault_injector
        self._file_fault_injector = file_fault_injector

    def execute(self, command: WorkspaceRefreshV2Command) -> WorkspaceRefreshV2Result:
        if command.action not in {"preview", "apply"}:
            raise ValueError("workspace_refresh action must be 'preview' or 'apply'")
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
                            ).union(resolution_paths)
                        )
                    )
                    warnings = self._generated_warnings(plan, resolution_paths)
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
        if len(merge.conflict_paths) > _MAX_CONFLICTS:
            raise WorkspaceError(f"Refresh preview exceeds the {_MAX_CONFLICTS}-conflict limit")
        conflicts = self._conflict_evidence(
            workspace,
            repo,
            merge.merge_base_sha,
            head,
            base.remote_base_sha,
            merge.conflict_paths,
        )
        payload = {
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
        }
        plan_hash = hashlib.sha256(
            json.dumps(
                payload,
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
        expected = {item.path for item in plan.conflicts}
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

    @staticmethod
    def _generated_warnings(
        plan: _RefreshPlan,
        resolution_paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        resolved = set(resolution_paths)
        return tuple(
            f"{item.path} is generated; merge source inputs and regenerate with: "
            + " ".join(item.regeneration_command)
            for item in plan.conflicts
            if item.path in resolved and item.kind == "generated"
        )

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
