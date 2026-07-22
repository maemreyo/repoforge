import contextlib
import hashlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from ...domain.command_source import dirty_command_source_paths
from ...domain.errors import CommandError, ErrorCode, RepoForgeError, SecurityError, WorkspaceError
from ...domain.execution_environment import build_execution_evidence
from ...domain.operation_task import OperationRetryability, OperationState
from ...domain.policy import normalize_relative_path
from ...domain.retry_guidance import (
    NOT_FOUND_CODES,
    FailureSignature,
    RetryGuidance,
    clear_reusable_failure,
    fast_fail_guidance,
    not_found_guidance,
    record_and_compare,
    record_reusable_failure,
    reusable_failure,
)
from ...domain.retry_guidance import (
    clear as clear_retry_guidance,
)
from ...domain.verification import get_profile, select_verification_profile
from ...domain.verification_steps import (
    HygieneBaselinePolicy,
    VerificationStep,
    VerificationStepKind,
    compile_legacy_steps,
    no_regression_receipt,
)
from ...domain.workspace import VerificationReceipt, is_commit_sha
from ...ports.background_tasks import BackgroundTaskRunner
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandResult
from ...ports.execution_environment import ExecutionReceipt
from ..code_intelligence import CodeIntelligenceAnalyzer, CodeIntelligenceCommand
from ..context import ApplicationContext
from ..dto import to_data
from ..execution.requests import profile_execution_request
from ..fingerprint_cache import prime_fingerprint, read_fingerprint
from ..operations.manager import OperationManager
from ..verification_reuse import (
    command_source_identity,
    config_identity,
    failure_reuse_binding,
    profile_target_identity,
)
from .hygiene_status import WorkspaceHygieneStatusCommand, WorkspaceHygieneStatusReader

_KIND = "workspace_run_profile"
_PROGRESS_HEARTBEAT_JOIN_SECONDS = 1.0
_ProgressCallback = Callable[[str, int, int, str, str], None]
#: How often to re-emit "still running" progress for one step while its
#: command executes. A verification step wraps one opaque subprocess (e.g.
#: `make test`) that can legitimately run for many minutes with nothing
#: finer-grained to report; without a heartbeat, progress_message and
#: updated_at freeze for the whole duration, and a slow command becomes
#: indistinguishable from a hung one until the profile's own timeout fires.
_PROGRESS_HEARTBEAT_SECONDS = 30.0
_OPERATION_LEASE_SECONDS = 90
_REUSABLE_PROFILE_STEP_KINDS = frozenset(
    {
        VerificationStepKind.HYGIENE,
        VerificationStepKind.STATIC_ANALYSIS,
        VerificationStepKind.TYPECHECK,
        VerificationStepKind.SECURITY,
        VerificationStepKind.CONTRACT,
        VerificationStepKind.BUILD,
    }
)


def _operation_lease_deadline(now: str) -> str:
    return (datetime.fromisoformat(now) + timedelta(seconds=_OPERATION_LEASE_SECONDS)).isoformat()


_NON_REUSABLE_PROFILE_CODES = frozenset(
    {
        ErrorCode.COMMAND_TIMEOUT,
        ErrorCode.LOCK_TIMEOUT,
        ErrorCode.RUNTIME_UNAVAILABLE,
        ErrorCode.RUNTIME_RELOADING,
        ErrorCode.STATE_PERSISTENCE_FAILED,
        ErrorCode.NOT_FOUND,
    }
)


@contextlib.contextmanager
def _step_progress_heartbeat(
    on_progress: _ProgressCallback | None,
    *,
    step_index: int,
    total: int,
    kind: str,
    interval_seconds: float = _PROGRESS_HEARTBEAT_SECONDS,
) -> Iterator[None]:
    """Re-emit "running" progress on a timer while the wrapped command executes."""
    if on_progress is None:
        yield
        return
    started = time.monotonic()
    stop = threading.Event()

    def tick() -> None:
        while not stop.wait(interval_seconds):
            elapsed = time.monotonic() - started
            on_progress(
                "running",
                step_index,
                total,
                "steps",
                f"running {kind} (step {step_index + 1}/{total}, elapsed {elapsed:.0f}s)",
            )

    thread = threading.Thread(target=tick, name="run-profile-progress-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=interval_seconds)


def _apply_retry_guidance(
    exc: RepoForgeError,
    *,
    guidances: list[RetryGuidance],
    repeat: int,
) -> None:
    if not guidances:
        return
    exc.details["retry_guidance"] = {
        "identical_failure_repeat": repeat,
        "statements": [g.statement for g in guidances],
    }
    combined_action = " ".join(g.safe_next_action for g in guidances)
    exc.safe_next_action = (
        f"{exc.safe_next_action} {combined_action}" if exc.safe_next_action else combined_action
    )


@dataclass(frozen=True, slots=True)
class WorkspaceRunProfileCommand:
    workspace_id: str
    profile_name: str | None = None
    background: bool = False
    force_rerun: bool = False
    expected_fingerprint: str | None = None
    cancellation_token: CancellationToken | None = None
    before_command: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceRunProfileResult:
    workspace_id: str
    repo_id: str
    profile: str
    description: str
    verification: bool
    fingerprint: str
    commands: list[dict[str, object]]
    change_metrics: dict[str, object]
    satisfies_commit_gate: bool
    used_default: bool
    head_sha: str
    command_source_dirty: bool
    command_source_dirty_paths: list[str]
    command_source_warning: str | None
    completed_steps: list[dict[str, object]]
    failed_step: dict[str, object] | None
    failure_domain: str | None
    not_run_steps: list[dict[str, object]]
    business_tests_ran: bool
    valid_tdd_red_evidence: bool
    hygiene_receipt: dict[str, object] | None
    execution_evidence: dict[str, object]
    working_directory: str | None = None


_COMMAND_SOURCE_WARNING_TEMPLATE = (
    "This run is not representative of the enrolled command chain: {paths} "
    "differ from the workspace base. Consider workspace_run_diagnostic or the "
    "audited ad-hoc runner (where enabled) for a targeted check instead."
)


@dataclass(frozen=True, slots=True)
class WorkspaceRunProfileBackgroundResult:
    operation_id: str
    phase: str
    safe_next_action: str


def _safe_error_message(text: str, *, limit: int = 2_000) -> str:
    """Bound and sanitize a raw exception while preserving diagnostic head and tail."""
    cleaned = "".join(ch for ch in text if ch in "\n\t\r" or ord(ch) >= 32).strip()
    if not cleaned:
        return "Background workspace_run_profile failed"
    if len(cleaned) <= limit:
        return cleaned
    marker = "\n... durable error excerpt omitted ...\n"
    if limit <= len(marker):
        return cleaned[:limit]
    available = limit - len(marker)
    head_size = available // 2
    tail_size = available - head_size
    return cleaned[:head_size] + marker + cleaned[-tail_size:]


class WorkspaceProfileRunner:
    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        operations: OperationManager | None = None,
        background_tasks: BackgroundTaskRunner | None = None,
    ):
        self.ctx: ApplicationContext = ctx
        self.operations = operations
        self.background_tasks = background_tasks
        self._cancel_tokens: dict[str, CancellationToken] = {}
        self._cancel_tokens_lock = threading.Lock()

    @staticmethod
    def public(
        r: CommandResult,
        *,
        stage_index: int,
        duration_ms: float,
        cumulative_duration_ms: float,
    ) -> dict[str, object]:
        return {
            "argv": list(r.argv),
            "returncode": r.returncode,
            "stdout": r.stdout,
            "stderr": r.stderr,
            "stage_index": stage_index,
            "duration_ms": round(duration_ms, 3),
            "cumulative_duration_ms": round(cumulative_duration_ms, 3),
        }

    @staticmethod
    def receipt(r: CommandResult) -> dict[str, object]:
        return {
            "argv": list(r.argv),
            "returncode": r.returncode,
            "stdout_sha256": hashlib.sha256(r.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(r.stderr.encode()).hexdigest(),
        }

    def _register_cancel_token(self, operation_id: str, token: CancellationToken) -> None:
        with self._cancel_tokens_lock:
            self._cancel_tokens[operation_id] = token

    def _unregister_cancel_token(self, operation_id: str) -> None:
        with self._cancel_tokens_lock:
            self._cancel_tokens.pop(operation_id, None)

    def request_live_cancel(self, operation_id: str) -> bool:
        """Signal the process group of a live background run; a no-op if none is bound."""
        with self._cancel_tokens_lock:
            token = self._cancel_tokens.get(operation_id)
        if token is None:
            return False
        token.cancel()
        return True

    def _running_operation_id(self, workspace_id: str) -> str | None:
        if self.operations is None:
            return None
        for task in self.operations.list_records(max_records=2_000).records:
            if (
                task.kind == _KIND
                and task.workspace_id == workspace_id
                and task.state is OperationState.RUNNING
            ):
                return task.operation_id
        return None

    def execute(
        self, c: WorkspaceRunProfileCommand
    ) -> WorkspaceRunProfileResult | WorkspaceRunProfileBackgroundResult:
        record, repo, path = self.ctx.workspace(c.workspace_id)
        if c.profile_name is None:
            profile, used_default = select_verification_profile(repo, None)
        else:
            profile = get_profile(repo, c.profile_name)
            used_default = False
        steps = profile.steps or compile_legacy_steps(profile.commands)
        command_cwd = path
        if profile.working_directory:
            relative = normalize_relative_path(profile.working_directory)
            unresolved = path / relative
            if unresolved.is_symlink():
                raise SecurityError("Profile working_directory cannot be a symlink")
            command_cwd = unresolved.resolve(strict=False)
            try:
                _ = command_cwd.relative_to(path.resolve(strict=True))
            except ValueError as exc:
                raise SecurityError("Profile working_directory escapes workspace") from exc
            if not command_cwd.is_dir():
                raise WorkspaceError(
                    f"Profile working_directory does not exist: {profile.working_directory}"
                )

        target = f"profile:{profile.name}"
        target_identity = profile_target_identity(profile, steps)

        def run_body(
            cancel_token: CancellationToken | None,
            on_before_command: Callable[[], None] | None,
            on_progress: _ProgressCallback | None,
        ) -> WorkspaceRunProfileResult:
            fresh = self.ctx.store.load(c.workspace_id)
            run_started = time.monotonic()
            before = read_fingerprint(
                self.ctx.fingerprint_cache, c.workspace_id, self.ctx.git, path
            )
            before_fingerprint = before.fingerprint
            if c.expected_fingerprint is not None and c.expected_fingerprint != before_fingerprint:
                raise WorkspaceError("Workspace changed since the verification plan was reviewed")
            stage_telemetry: list[tuple[float, float]] = []

            def emit_step_progress(
                step_index: int,
                verification_step: VerificationStep,
                *,
                completed: bool,
                duration_ms: float | None = None,
            ) -> None:
                if on_progress is None:
                    return
                ordinal = step_index + 1
                total = len(steps)
                kind = verification_step.kind.value
                if completed:
                    assert duration_ms is not None
                    message = f"completed {kind} (step {ordinal}/{total}, {duration_ms:.3f} ms)"
                    current = ordinal
                else:
                    message = f"running {kind} (step {ordinal}/{total})"
                    current = step_index
                on_progress("running", current, total, "steps", message)

            # Command-source integrity stamp (issue #170): a zero-cost guard when the
            # profile has no declared/derived command-source paths; otherwise one cheap,
            # path-restricted diff against the recorded workspace base plus the already
            # -needed working-tree changed-paths read. Evidence only -- never blocks.
            command_source_dirty_paths: tuple[str, ...] = ()
            if profile.command_source_paths:
                changed_since_base: set[str] = set(self.ctx.git.changed_paths(path, repo))
                base_sha = fresh.metadata.get("workspace_base_sha")
                if isinstance(base_sha, str) and is_commit_sha(base_sha):
                    head_sha = self.ctx.git.head_sha(path)
                    if base_sha != head_sha:
                        changed_since_base.update(
                            self.ctx.git.changed_paths_between(path, repo, base_sha, head_sha)
                        )
                command_source_dirty_paths = dirty_command_source_paths(
                    frozenset(changed_since_base), profile.command_source_paths
                )
            command_source_dirty = bool(command_source_dirty_paths)
            audit_details["command_source_dirty"] = command_source_dirty
            if command_source_dirty_paths:
                audit_details["command_source_dirty_paths"] = list(command_source_dirty_paths)

            reuse_binding = None

            def record_command_failure(
                exc: CommandError,
                verification_step: VerificationStep,
                step_index: int,
                stage_duration_ms: float,
            ) -> None:
                command = verification_step.command
                fallback = command[0] if command else None
                completed_steps = [
                    {
                        **item.public(),
                        "duration_ms": round(stage_telemetry[index][0], 3),
                        "cumulative_duration_ms": round(stage_telemetry[index][1], 3),
                    }
                    for index, item in enumerate(steps[:step_index])
                ]
                failed_step = verification_step.public()
                not_run_steps = [item.public() for item in steps[step_index + 1 :]]
                business_tests_ran = any(
                    item.kind is VerificationStepKind.BUSINESS_TESTS
                    for item in steps[: step_index + 1]
                )
                cumulative_duration_ms = (time.monotonic() - run_started) * 1_000
                failed_step["duration_ms"] = round(stage_duration_ms, 3)
                failed_step["cumulative_duration_ms"] = round(cumulative_duration_ms, 3)
                exc.details.update(
                    {
                        "steps_completed": step_index,
                        "completed_steps": completed_steps,
                        "failed_step": failed_step,
                        "failure_domain": verification_step.kind.value,
                        "not_run_steps": not_run_steps,
                        "business_tests_ran": business_tests_ran,
                        "valid_tdd_red_evidence": False,
                    }
                )
                audit_details["failed_command"] = exc.details.get("command", fallback)
                audit_details["exit_code"] = exc.details.get("exit_code")
                audit_details["steps_completed"] = step_index
                audit_details["failed_step"] = failed_step
                audit_details["failure_domain"] = verification_step.kind.value
                audit_details["business_tests_ran"] = business_tests_ran
                audit_details["duration_ms"] = round(stage_duration_ms, 3)
                audit_details["cumulative_duration_ms"] = round(cumulative_duration_ms, 3)
                cancelled = bool(exc.details.get("cancelled")) or bool(
                    cancel_token is not None and cancel_token.is_cancelled()
                )
                if cancelled:
                    exc.details["cancelled"] = True
                    audit_details["cancelled"] = True
                    return
                if repo.diagnostics:
                    diagnostic_ids = sorted(repo.diagnostics)
                    exc.details["available_diagnostics"] = diagnostic_ids
                    targeted_hint = (
                        "A reviewed targeted alternative is enrolled for this repository "
                        f"({', '.join(diagnostic_ids)}); prefer workspace_run_diagnostic to iterate "
                        "instead of rerunning the full profile."
                    )
                    exc.safe_next_action = (
                        f"{exc.safe_next_action} {targeted_hint}"
                        if exc.safe_next_action
                        else targeted_hint
                    )
                affected_candidates: list[dict[str, object]] = []
                if verification_step.kind in _REUSABLE_PROFILE_STEP_KINDS:
                    with contextlib.suppress(Exception):
                        analysis = CodeIntelligenceAnalyzer(self.ctx).analyze_current(
                            CodeIntelligenceCommand(
                                c.workspace_id,
                                expected_head_sha=self.ctx.git.head_sha(path).lower(),
                                expected_fingerprint=before_fingerprint,
                            )
                        )
                        for candidate in analysis.result.affected_tests[:8]:
                            if (
                                candidate.diagnostic_id is None
                                or candidate.diagnostic_id not in repo.diagnostics
                            ):
                                continue
                            payload = to_data(candidate)
                            if isinstance(payload, dict):
                                affected_candidates.append(payload)
                if affected_candidates:
                    exc.details["affected_test_candidates"] = affected_candidates
                    audit_details["affected_test_candidates"] = affected_candidates
                    first = affected_candidates[0]
                    diagnostic = first.get("diagnostic_id")
                    selector = first.get("selector")
                    candidate_hint = (
                        "Run workspace_run_diagnostic with "
                        f"diagnostic_id={diagnostic!r} and selector={selector!r} before "
                        "rerunning the broader profile."
                    )
                    exc.safe_next_action = (
                        f"{exc.safe_next_action} {candidate_hint}"
                        if exc.safe_next_action
                        else candidate_hint
                    )
                error_code = exc.code.value
                raw_exit_code = exc.details.get("exit_code")
                exit_code = raw_exit_code if isinstance(raw_exit_code, int) else None
                signature = FailureSignature(error_code, step_index, exit_code)
                repeat, repeat_guidance = record_and_compare(
                    fresh.metadata,
                    target=target,
                    fingerprint=before_fingerprint,
                    signature=signature,
                )
                rendered_failure = str(exc).lower()
                suspected_network_failure = any(
                    marker in rendered_failure
                    for marker in (
                        "connection refused",
                        "connection reset",
                        "network is unreachable",
                        "name or service not known",
                        "temporary failure in name resolution",
                        "dns",
                        "http 429",
                        "http 502",
                        "http 503",
                        "http 504",
                    )
                )
                reusable = (
                    reuse_binding is not None
                    and not exc.retryable
                    and exit_code is not None
                    and verification_step.kind in _REUSABLE_PROFILE_STEP_KINDS
                    and exc.code not in _NON_REUSABLE_PROFILE_CODES
                    and not suspected_network_failure
                )
                if reusable and reuse_binding is not None:
                    recorded = record_reusable_failure(
                        fresh.metadata,
                        target=target,
                        binding=reuse_binding,
                        evidence={
                            "complete": True,
                            "error_code": error_code,
                            "exit_code": exit_code,
                            "failed_step_index": step_index,
                            "completed_steps": completed_steps,
                            "failed_step": failed_step,
                            "failure_domain": verification_step.kind.value,
                            "not_run_steps": not_run_steps,
                            "business_tests_ran": business_tests_ran,
                            "valid_tdd_red_evidence": False,
                            **(
                                {"affected_test_candidates": affected_candidates}
                                if affected_candidates
                                else {}
                            ),
                        },
                    )
                    if recorded:
                        audit_details["failure_reuse_recorded"] = True
                        audit_details["reuse_binding"] = reuse_binding.digest
                self.ctx.store.save(fresh)
                audit_details["retry_repeat"] = repeat
                guidances: list[RetryGuidance] = []
                if error_code in NOT_FOUND_CODES:
                    guidances.append(not_found_guidance())
                elif repeat_guidance is not None:
                    guidances.append(repeat_guidance)
                if profile.verification:
                    duration_seconds = time.monotonic() - run_started
                    fast_guidance = fast_fail_guidance(
                        duration_seconds,
                        threshold_seconds=self.ctx.config.server.fast_fail_threshold_seconds,
                    )
                    if fast_guidance is not None:
                        guidances.append(fast_guidance)
                _apply_retry_guidance(exc, guidances=guidances, repeat=repeat)

            def raise_reused_failure(evidence: dict[str, object]) -> None:
                if reuse_binding is None:
                    return
                raw_code = evidence.get("error_code")
                try:
                    code = ErrorCode(str(raw_code))
                except ValueError:
                    code = ErrorCode.COMMAND_FAILED
                raw_step = evidence.get("failed_step_index")
                failed_step_index = raw_step if isinstance(raw_step, int) else None
                raw_exit = evidence.get("exit_code")
                exit_code = raw_exit if isinstance(raw_exit, int) else None
                repeat, guidance = record_and_compare(
                    fresh.metadata,
                    target=target,
                    fingerprint=before_fingerprint,
                    signature=FailureSignature(code.value, failed_step_index, exit_code),
                )
                self.ctx.store.save(fresh)
                details = {str(key): value for key, value in evidence.items() if key != "complete"}
                details.update(
                    {
                        "failure_reused": True,
                        "reuse_binding": reuse_binding.digest,
                    }
                )
                audit_details.update(
                    {
                        "failure_reused": True,
                        "reuse_binding": reuse_binding.digest,
                        "retry_repeat": repeat,
                    }
                )
                cached_action = (
                    "Investigate the cached failed-step evidence. Use force_rerun=true only "
                    "when an external condition changed without changing the bound snapshot."
                )
                raw_candidates = details.get("affected_test_candidates")
                if isinstance(raw_candidates, list) and raw_candidates:
                    first = raw_candidates[0]
                    if isinstance(first, dict):
                        cached_action += (
                            " Run workspace_run_diagnostic with "
                            f"diagnostic_id={first.get('diagnostic_id')!r} and "
                            f"selector={first.get('selector')!r}."
                        )
                error = CommandError(
                    "Reused an exact-bound deterministic failure without rerunning the command.",
                    code=code,
                    retryable=False,
                    safe_next_action=cached_action,
                    details=details,
                )
                _apply_retry_guidance(
                    error,
                    guidances=[guidance] if guidance is not None else [],
                    repeat=repeat,
                )
                raise error

            timeout = profile.timeout_seconds or self.ctx.config.server.verification_timeout_seconds
            environment_hash: str | None = None
            requested_policy_hash = ""
            effective_policy_hash = ""
            execution_evidence_data: dict[str, object] = {}
            hygiene_receipt_data: dict[str, object] | None = None

            def accepted_no_regression_step(
                verification_step: VerificationStep,
            ) -> CommandResult | None:
                nonlocal hygiene_receipt_data
                if (
                    profile.baseline_policy is not HygieneBaselinePolicy.NO_REGRESSION
                    or verification_step.kind is not VerificationStepKind.HYGIENE
                ):
                    return None
                status = WorkspaceHygieneStatusReader(self.ctx)._read(
                    WorkspaceHygieneStatusCommand(c.workspace_id)
                )
                receipt = no_regression_receipt(
                    base_sha=status.base_sha,
                    workspace_fingerprint=status.workspace_fingerprint,
                    formatter_contract_hash=status.formatter_contract_hash,
                    environment_identity=status.environment_identity,
                    preexisting_count=len(status.preexisting),
                    introduced_count=len(status.introduced),
                    changed_path_finding_count=len(status.changed_path_findings),
                    output_truncated=status.output_truncated,
                )
                if receipt is None:
                    return None
                hygiene_receipt_data = receipt.as_dict()
                audit_details["baseline_policy"] = profile.baseline_policy.value
                audit_details["hygiene_receipt_hash"] = receipt.receipt_hash
                audit_details["preexisting_hygiene_findings"] = receipt.preexisting_count
                return CommandResult(
                    verification_step.command,
                    str(command_cwd),
                    0,
                    "Accepted by the reviewed no_regression hygiene policy.",
                    "",
                )

            execution_request = profile_execution_request(
                workspace_id=c.workspace_id,
                workspace_root=path,
                command_cwd=command_cwd,
                commands=tuple(step.command for step in steps),
                working_directory_policy=profile.working_directory or ".",
                timeout_seconds=timeout,
                output_limit=self.ctx.config.server.max_tool_output_chars,
                cancel_token=cancel_token,
            )
            results: list[CommandResult] = []
            with self.ctx.execution.prepare(execution_request) as session:
                identity = session.prepared.identity
                environment_hash = identity.identity_hash
                reuse_binding = failure_reuse_binding(
                    fingerprint=before_fingerprint,
                    target_identity=target_identity,
                    command_source_identity_value=command_source_identity(
                        path, profile.command_source_paths
                    ),
                    config_identity_value=config_identity(self.ctx.config.source_path),
                    environment_identity=environment_hash,
                )
                if not c.force_rerun and reuse_binding is not None:
                    cached = reusable_failure(
                        fresh.metadata,
                        target=target,
                        binding=reuse_binding,
                    )
                    if cached is not None:
                        raise_reused_failure(cached)
                receipts: list[ExecutionReceipt] = []
                for step_index, verification_step in enumerate(steps):
                    emit_step_progress(step_index, verification_step, completed=False)
                    command = verification_step.command
                    accepted = accepted_no_regression_step(verification_step)
                    if accepted is not None:
                        receipts.append(
                            ExecutionReceipt(
                                argv=command,
                                session_start_identity_hash=identity.identity_hash,
                                result=accepted,
                                requested_policy_hash=session.prepared.requested_policy_hash,
                                effective_policy_hash=session.prepared.effective_policy_hash,
                                effective_policy=session.prepared.effective_policy,
                            )
                        )
                        stage_telemetry.append((0.0, (time.monotonic() - run_started) * 1_000))
                        emit_step_progress(
                            step_index,
                            verification_step,
                            completed=True,
                            duration_ms=0.0,
                        )
                        continue
                    if on_before_command is not None:
                        on_before_command()
                    stage_started = time.monotonic()
                    try:
                        with _step_progress_heartbeat(
                            on_progress,
                            step_index=step_index,
                            total=len(steps),
                            kind=verification_step.kind.value,
                            interval_seconds=_PROGRESS_HEARTBEAT_SECONDS,
                        ):
                            receipts.append(session.execute(command))
                    except CommandError as exc:
                        record_command_failure(
                            exc,
                            verification_step,
                            step_index,
                            (time.monotonic() - stage_started) * 1_000,
                        )
                        raise
                    stage_duration_ms = (time.monotonic() - stage_started) * 1_000
                    stage_telemetry.append(
                        (
                            stage_duration_ms,
                            (time.monotonic() - run_started) * 1_000,
                        )
                    )
                    emit_step_progress(
                        step_index,
                        verification_step,
                        completed=True,
                        duration_ms=stage_duration_ms,
                    )
                inspection = session.inspect()
                requested_policy_hash = inspection.requested_policy_hash
                effective_policy_hash = inspection.effective_policy_hash
                execution_evidence_data = to_data(
                    build_execution_evidence(
                        execution_request.requested_policy,
                        inspection.identity,
                        inspection.effective_policy,
                        inspection.warnings,
                    )
                )
                results = [receipt.result for receipt in receipts]
            _ = self.ctx.git.changed_paths(path, repo)
            metrics = self.ctx.git.enforce_change_budget(path, repo)
            fingerprint = prime_fingerprint(
                self.ctx.fingerprint_cache,
                c.workspace_id,
                self.ctx.git,
                path,
            )
            fp = fingerprint.fingerprint
            cleared_retry_history = clear_retry_guidance(fresh.metadata, target=target)
            cleared_reuse_history = clear_reusable_failure(fresh.metadata, target=target)
            command_source_warning = (
                _COMMAND_SOURCE_WARNING_TEMPLATE.format(paths=", ".join(command_source_dirty_paths))
                if command_source_dirty_paths
                else None
            )
            if profile.verification:
                fresh.last_verification = VerificationReceipt(
                    profile=profile.name,
                    fingerprint=fp,
                    completed_at=self.ctx.clock.now_iso(),
                    commands=[self.receipt(r) for r in results],
                    environment_identity_hash=environment_hash,
                    command_source_dirty=command_source_dirty,
                    command_source_dirty_paths=list(command_source_dirty_paths),
                    requested_policy_hash=requested_policy_hash,
                    effective_policy_hash=effective_policy_hash,
                    execution_evidence=execution_evidence_data,
                )
                self.ctx.store.save(fresh)
            elif cleared_retry_history or cleared_reuse_history:
                self.ctx.store.save(fresh)
            return WorkspaceRunProfileResult(
                workspace_id=c.workspace_id,
                repo_id=record.repo_id,
                profile=profile.name,
                description=profile.description,
                verification=profile.verification,
                fingerprint=fp,
                commands=[
                    self.public(
                        result,
                        stage_index=index,
                        duration_ms=stage_telemetry[index][0],
                        cumulative_duration_ms=stage_telemetry[index][1],
                    )
                    for index, result in enumerate(results)
                ],
                change_metrics=metrics,
                satisfies_commit_gate=profile.verification,
                used_default=used_default,
                head_sha=self.ctx.git.head_sha(path),
                working_directory=profile.working_directory,
                command_source_dirty=command_source_dirty,
                command_source_dirty_paths=list(command_source_dirty_paths),
                command_source_warning=command_source_warning,
                completed_steps=[step.public() for step in steps],
                failed_step=None,
                failure_domain=None,
                not_run_steps=[],
                business_tests_ran=any(
                    step.kind is VerificationStepKind.BUSINESS_TESTS for step in steps
                ),
                valid_tdd_red_evidence=False,
                hygiene_receipt=hygiene_receipt_data,
                execution_evidence=execution_evidence_data,
            )

        audit_details: dict[str, object] = {
            "workspace_id": c.workspace_id,
            "profile": profile.name,
            "used_default": used_default,
            "force_rerun": c.force_rerun,
            "expected_fingerprint": c.expected_fingerprint,
        }

        if not c.background:

            def op() -> WorkspaceRunProfileResult:
                with self.ctx.locks.lock(c.workspace_id):
                    return run_body(c.cancellation_token, c.before_command, None)

            return self.ctx.audited(
                "workspace_run_profile",
                audit_details,
                op,
            )

        return self._start_background(c, run_body, audit_details)

    def _start_background(
        self,
        c: WorkspaceRunProfileCommand,
        run_body: Callable[
            [CancellationToken | None, Callable[[], None] | None, _ProgressCallback | None],
            WorkspaceRunProfileResult,
        ],
        audit_details: dict[str, object],
    ) -> WorkspaceRunProfileBackgroundResult:
        operations = self.operations
        background_tasks = self.background_tasks
        result_store = self.ctx.operation_result_store
        if operations is None or background_tasks is None or result_store is None:
            raise RepoForgeError(
                "Background workspace_run_profile requires the durable operations manager, "
                "operation result store, and background task runner to be configured",
                code=ErrorCode.CONFIG_INVALID,
            )

        lock_cm = self.ctx.locks.lock(
            c.workspace_id,
            timeout_seconds=0,
            metadata={"purpose": "workspace_run_profile_background"},
        )
        try:
            lock_cm.__enter__()
        except RepoForgeError as exc:
            if exc.code is ErrorCode.LOCK_TIMEOUT:
                holder = self._running_operation_id(c.workspace_id)
                suffix = f" (currently running as operation {holder!r})" if holder else ""
                raise RepoForgeError(
                    f"Workspace {c.workspace_id!r} is locked by another running operation{suffix}",
                    code=ErrorCode.LOCK_TIMEOUT,
                    retryable=True,
                    safe_next_action=(
                        f"Poll operation_status for {holder!r} and retry once it completes."
                        if holder
                        else "Poll workspace_status and retry once the current mutation completes."
                    ),
                    details={
                        "workspace_id": c.workspace_id,
                        **({"operation_id": holder} if holder else {}),
                    },
                ) from exc
            raise

        now = self.ctx.clock.now_iso()
        owner_id = f"worker-{self.ctx.ids.new_hex(24)}"
        lease_expires_at = _operation_lease_deadline(now)
        try:
            with self.ctx.locks.lock(
                "background-profile-admission",
                timeout_seconds=2,
                metadata={"purpose": "background_profile_admission"},
            ):
                cap = self.ctx.config.server.max_background_profiles
                running = sum(
                    1
                    for candidate in operations.list_records(max_records=2_000).records
                    if candidate.kind == _KIND and candidate.state is OperationState.RUNNING
                )
                if running >= cap:
                    raise RepoForgeError(
                        f"Background workspace_run_profile is at its configured concurrency cap "
                        f"of {cap} running operation(s)",
                        code=ErrorCode.RUNTIME_UNAVAILABLE,
                        retryable=True,
                        safe_next_action=(
                            f"Wait for a running background profile to finish "
                            f"(max_background_profiles={cap}) and retry, or poll "
                            f"operation_list with scope='workspace:{c.workspace_id}' for progress."
                        ),
                        details={"max_background_profiles": cap, "running": running},
                    )
                task = operations.create(
                    kind=_KIND,
                    phase="queued",
                    cancel_supported=True,
                    workspace_id=c.workspace_id,
                    now=now,
                )
                try:
                    task = operations.start(
                        task.operation_id,
                        owner_id=owner_id,
                        lease_expires_at=lease_expires_at,
                        now=now,
                    )
                except Exception:
                    # The operation record was created but never reached "running"; fail it
                    # closed rather than leaving a PENDING record that recovery does not scan.
                    with contextlib.suppress(Exception):
                        operations.fail(
                            task.operation_id,
                            error_code=ErrorCode.INTERNAL_ERROR.value,
                            error_message="Background admission could not transition to running",
                        )
                    raise
        except Exception:
            lock_cm.__exit__(None, None, None)
            raise

        operation_id = task.operation_id
        cancel_token = CancellationToken()
        self._register_cancel_token(operation_id, cancel_token)

        def on_before_command() -> None:
            current = operations.status(operation_id)
            if current.cancellation_requested_at is not None:
                cancel_token.cancel()
                raise RepoForgeError(
                    "Background profile run was cancelled before the next command started",
                    code=ErrorCode.COMMAND_FAILED,
                    details={"cancelled": True},
                )

        def on_progress(
            phase: str,
            current: int,
            total: int,
            unit: str,
            message: str,
        ) -> None:
            progress_now = self.ctx.clock.now_iso()
            with contextlib.suppress(RepoForgeError):
                operations.progress(
                    operation_id,
                    phase=phase,
                    current=current,
                    total=total,
                    unit=unit,
                    message=message,
                    owner_id=owner_id,
                    lease_expires_at=_operation_lease_deadline(progress_now),
                    now=progress_now,
                )

        def finish_terminal(
            exc: Exception | None,
            result: WorkspaceRunProfileResult | None,
        ) -> None:
            finish_now = self.ctx.clock.now_iso()
            try:
                current = operations.status(operation_id)
            except RepoForgeError:
                return
            if exc is None and result is not None:
                try:
                    result_store.save(operation_id, to_data(result))
                    operations.succeed(
                        operation_id,
                        result_reference=f"{_KIND}:{operation_id}",
                        owner_id=owner_id,
                        now=finish_now,
                    )
                except Exception as persist_exc:
                    with contextlib.suppress(Exception):
                        result_store.delete(operation_id)
                    with contextlib.suppress(RepoForgeError):
                        operations.fail(
                            operation_id,
                            error_code=ErrorCode.STATE_PERSISTENCE_FAILED.value,
                            error_message=_safe_error_message(str(persist_exc)),
                            retryability=OperationRetryability.MANUAL,
                            owner_id=owner_id,
                            now=finish_now,
                        )
                return
            with contextlib.suppress(Exception):
                result_store.delete(operation_id)
            if current.cancellation_requested_at is not None or cancel_token.is_cancelled():
                with contextlib.suppress(RepoForgeError):
                    operations.cancelled(
                        operation_id,
                        owner_id=owner_id,
                        now=finish_now,
                    )
                return
            failure = exc or RepoForgeError(
                "Background profile completed without a result",
                code=ErrorCode.INTERNAL_ERROR,
            )
            code = str(
                getattr(
                    getattr(failure, "code", None),
                    "value",
                    getattr(failure, "code", "INTERNAL_ERROR"),
                )
            )
            try:
                normalized = ErrorCode(code)
            except ValueError:
                normalized = ErrorCode.INTERNAL_ERROR
            retryable = bool(getattr(failure, "retryable", False))
            with contextlib.suppress(RepoForgeError):
                operations.fail(
                    operation_id,
                    error_code=normalized.value,
                    error_message=_safe_error_message(str(failure)),
                    retryability=(
                        OperationRetryability.AUTOMATIC
                        if retryable
                        else OperationRetryability.MANUAL
                    ),
                    owner_id=owner_id,
                    now=finish_now,
                )

        def run() -> None:
            failure: Exception | None = None
            result: WorkspaceRunProfileResult | None = None
            try:
                try:
                    result = self.ctx.audited(
                        "workspace_run_profile",
                        audit_details,
                        lambda: run_body(cancel_token, on_before_command, on_progress),
                    )
                except Exception as exc:
                    failure = exc
            finally:
                # Release the lock before marking the operation terminal, matching the
                # documented order: terminate/finish -> release lock -> mark terminal.
                self._unregister_cancel_token(operation_id)
                lock_cm.__exit__(None, None, None)
            finish_terminal(failure, result)

        try:
            scheduled = background_tasks.submit(operation_id, run)
        except Exception as exc:
            # A raised exception from submit() must not leave the operation stuck in
            # RUNNING while holding the workspace lock forever; unwind exactly like a
            # rejected submission (unregister -> release lock -> fail closed) before
            # propagating the original failure.
            self._unregister_cancel_token(operation_id)
            lock_cm.__exit__(None, None, None)
            with contextlib.suppress(Exception):
                operations.fail(
                    operation_id,
                    error_code=ErrorCode.INTERNAL_ERROR.value,
                    error_message=_safe_error_message(
                        f"Background task runner raised while accepting the profile run: {exc}"
                    ),
                    owner_id=owner_id,
                )
            raise

        if not scheduled:
            # An operation_id collision is not expected to happen in practice (the id space
            # is a random 24-byte hex string); fail closed rather than run silently untracked.
            self._unregister_cancel_token(operation_id)
            lock_cm.__exit__(None, None, None)
            operations.fail(
                operation_id,
                error_code=ErrorCode.INTERNAL_ERROR.value,
                error_message="Background task runner could not accept the profile run",
                owner_id=owner_id,
            )
            raise RepoForgeError(
                "Background task runner rejected the profile run",
                code=ErrorCode.INTERNAL_ERROR,
            )

        return WorkspaceRunProfileBackgroundResult(
            operation_id=operation_id,
            phase="running",
            safe_next_action=(
                "Poll operation_status; the workspace lock is held until the run completes."
            ),
        )
