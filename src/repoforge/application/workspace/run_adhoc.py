"""Audited ad-hoc command runner for per-repository relaxed execution mode.

Principle: **iterate relaxed, gate strict**. This runner lets an agent working in a
repository the owner has explicitly configured as ``execution_mode = "relaxed"`` run
one exact allowlisted-runner command without a shell -- but the result is evidence
only. It never populates ``fresh.last_verification`` and can never satisfy
``require_verification_before_commit`` (enforced in
``src/repoforge/application/workspace/commit.py``); only an enrolled verification
profile can do that.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from ...config import RepositoryConfig
from ...domain.adhoc import (
    CommandClass,
    EffectClass,
    ExecutionMode,
    classify_adhoc_effect,
    effect_exceeds_declaration,
    effect_to_command_class,
    extract_git_argv_segments,
    extract_push_destination_refs,
    scan_script_for_blocked_git_forms,
    validate_adhoc_argv,
    validate_adhoc_script,
    validate_adhoc_sequence,
    validate_adhoc_shell_runner,
    validate_adhoc_stdin,
)
from ...domain.circuit_breakers import CircuitBreakerCategory, circuit_breaker_blocked
from ...domain.credential_profiles import resolve_credential_profile_env
from ...domain.errors import CommandError, ErrorCode, RepoForgeError, SecurityError, WorkspaceError
from ...domain.execution_environment import build_execution_evidence
from ...domain.execution_profiles import available_execution_profiles
from ...domain.operation_identity import bind_worker_identity
from ...domain.operation_task import OperationRetryability, OperationState
from ...domain.operation_worker import OperationWorkerBinding
from ...domain.policy import normalize_relative_path
from ...ports.background_tasks import BackgroundTaskRunner
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandResult
from ..context import ApplicationContext
from ..dto import to_data
from ..execution.requests import adhoc_execution_request
from ..fingerprint_cache import prime_fingerprint, read_fingerprint
from ..operations.manager import OperationManager

_KIND = "workspace_run_adhoc"
_NETWORK_POLICY_LABEL = "advisory_local_only"
_ARGV_RECURRENCE_THRESHOLD = 3
_MAX_CHANGED_PATHS_REPORTED = 200
_OPERATION_LEASE_GRACE_SECONDS = 60
#: Same rationale and value as run_profile.py's own step heartbeat: a long-running
#: command's operation record would otherwise go quiet for its whole duration, and a
#: slow (but healthy) command becomes indistinguishable from a hung one until the
#: repository's adhoc_timeout_seconds fires (#377 AC: "streaming works within connector
#: timeouts"). Deliberately a periodic re-emit of the existing progress channel, not a
#: rewrite of subprocess I/O -- the command itself still runs as one blocking call.
_PROGRESS_HEARTBEAT_SECONDS = 30.0


@contextlib.contextmanager
def _command_progress_heartbeat(
    progress: Callable[[str, int, int, str, str], None] | None,
    *,
    label: str,
    interval_seconds: float = _PROGRESS_HEARTBEAT_SECONDS,
) -> Iterator[None]:
    """Re-emit "running" progress on a timer while the wrapped command executes."""
    if progress is None:
        yield
        return
    started = time.monotonic()
    stop = threading.Event()

    def tick() -> None:
        while not stop.wait(interval_seconds):
            elapsed = time.monotonic() - started
            progress("running", 0, 1, "commands", f"running {label} (elapsed {elapsed:.0f}s)")

    thread = threading.Thread(target=tick, name="adhoc-progress-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=interval_seconds)


@dataclass(frozen=True, slots=True)
class WorkspaceRunAdhocCommand:
    workspace_id: str
    argv: tuple[str, ...] | None = None
    # Reviewed shell-script form (#377): mutually exclusive with argv. `shell` names the
    # interpreter (validated against repo.adhoc_shell_runners); the actual argv run is
    # (shell, "-c", script) -- not authoritatively content-inspected like a git argv is,
    # though #407 layers a best-effort scan on top (see scan_script_for_blocked_git_forms).
    script: str | None = None
    shell: str | None = None
    working_directory: str | None = None
    background: bool = False
    expected_fingerprint: str | None = None
    expected_head_sha: str | None = None
    mutability: str = "read_only"
    stdin_text: str | None = None
    declared_effect: str | None = None
    cancellation_token: CancellationToken | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceRunAdhocResult:
    workspace_id: str
    argv: list[str]
    runner: str
    working_directory: str
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: float
    fingerprint_before: str
    fingerprint_after: str
    fingerprint_changed: bool
    changed_paths: list[str]
    changed_paths_truncated: bool
    head_sha: str
    head_sha_before: str
    mutability: str
    command_class: str | None
    declared_effect: str
    effect_class: str | None
    effect_mismatch: bool
    read_only_violation: bool
    network_policy: str
    evidence_only: bool
    satisfies_commit_gate: bool
    verification_invalidated: bool
    gate_guidance: str
    enrollment_nudge: str | None
    next_safe_actions: list[dict[str, object]]
    execution_evidence: dict[str, object] = field(default_factory=dict)
    # Defaulted (unlike most fields here) so a background result stored by an earlier
    # release -- before this field existed -- still reconstructs via **stored across a
    # deploy, instead of failing with a missing-argument error.
    output_artifact_reference: str | None = None
    output_artifact_status: str = "not_applicable"


@dataclass(frozen=True, slots=True)
class WorkspaceRunAdhocBackgroundResult:
    operation_id: str
    phase: str
    safe_next_action: str


@dataclass(frozen=True, slots=True)
class WorkspaceRunAdhocSequenceCommand:
    """Bounded fail-fast argv sequence (#443). Reachable only through workspace_exec's
    durable-admission-queue path, not the older self-admitting workspace_run_adhoc
    service method -- so there is no separate background field here: foreground vs.
    background is entirely an operation-admission concern the durable queue already
    handles (see WorkspaceExecutor), identical for a single command or a sequence."""

    workspace_id: str
    argv_sequence: tuple[tuple[str, ...], ...]
    working_directory: str | None = None
    expected_fingerprint: str | None = None
    expected_head_sha: str | None = None
    mutability: str = "read_only"
    declared_effect: str | None = None
    cancellation_token: CancellationToken | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceRunAdhocSequenceResult:
    workspace_id: str
    #: One dict per element that ran, in order, shaped for `_command_evidence()`
    #: (argv, returncode, duration_ms, stdout, stderr, output_artifact_reference,
    #: output_artifact_status) -- the same shape `workspace_run_profile`'s multi-step
    #: commands list already uses.
    commands: list[dict[str, object]]
    stopped_early: bool
    fingerprint_before: str
    fingerprint_after: str
    fingerprint_changed: bool
    changed_paths: list[str]
    changed_paths_truncated: bool
    head_sha: str
    head_sha_before: str
    mutability: str
    declared_effect: str
    #: True when any element's observed effect exceeded declared_effect (#382). There is
    #: no single "observed_effect" field here, mirroring all_content_inspected below: a
    #: sequence's elements may have heterogeneous effects, so only the aggregate
    #: mismatch/inspection facts are reported, not one value that would misrepresent them.
    effect_mismatch: bool
    read_only_violation: bool
    #: True only when every element was a git command classify_adhoc_command actually
    #: inspected -- the weakest link decides: one opaque (non-git) element makes the
    #: overall "this sequence's content was inspected" claim false.
    all_content_inspected: bool
    network_policy: str
    evidence_only: bool
    satisfies_commit_gate: bool
    verification_invalidated: bool
    gate_guidance: str
    next_safe_actions: list[dict[str, object]]
    # Defaulted so a sequence result already stored by the prior release (before this
    # field existed) still reconstructs via **stored across a deploy, same reasoning as
    # WorkspaceRunAdhocResult's own output_artifact_reference/status fields.
    execution_evidence: dict[str, object] = field(default_factory=dict)


_GATE_GUIDANCE = (
    "This ad-hoc run is evidence only: it never satisfies require_verification_before_commit. "
    "Run an enrolled verification profile (workspace_verify / workspace_run_profile) on the exact "
    "tree immediately before workspace_commit."
)


def _resolve_declared_effect(declared_effect: str | None, mutability: str) -> EffectClass:
    """The caller's stated intent (#382), defaulted from mutability when omitted.

    This default is itself never an authorization -- see EffectClass's own docstring --
    it only sets the baseline effect_mismatch is compared against.
    """
    if declared_effect is not None:
        return EffectClass(declared_effect)
    return EffectClass.WORKSPACE if mutability == "workspace" else EffectClass.READ_ONLY


_MAX_ADHOC_TIMEOUT_SECONDS = 3_600


def _adhoc_timeout_remedy(budget_seconds: int) -> str:
    """Name the budget that expired, and the two legitimate ways past it."""
    return (
        f"The ad-hoc budget for this repository is {budget_seconds}s "
        "(repositories.<id>.adhoc_timeout_seconds), not a platform limit: an operator can raise it "
        f"to {_MAX_ADHOC_TIMEOUT_SECONDS}s. A long job that is already reviewed and named -- a full "
        "test recording, a coverage build -- belongs in a reviewed profile with its own "
        "timeout_seconds instead of the ad-hoc runner, which keeps one documented command as its "
        "own reproducible generator. Do not split the job into hand-written chunks to fit the "
        "budget: the output stops being reproducible by the command that is supposed to produce it."
    )


def _adhoc_missing_executable_remedy(executable: str, repo_id: str) -> str:
    """Name the two reviewed ways to make an allowlisted-but-absent runner resolvable,
    instead of leaving a raw OS-level "not found" with no actionable next step (#380)."""
    return (
        f"{executable!r} is allowlisted for repositories.{repo_id} but is not installed, "
        "or is not on the constrained runtime PATH (repositories.<id>.execution_profiles, "
        "server.path_prefixes). Install it where the runtime PATH resolves it, or ask the "
        "repository owner to enroll a reviewed execution_profiles entry that provides it "
        f"({', '.join(available_execution_profiles())})."
    )


def _strict_mode_error(repo_id: str) -> RepoForgeError:
    return RepoForgeError(
        f"Repository {repo_id!r} is enrolled in strict execution mode; the ad-hoc runner is disabled",
        code=ErrorCode.EXECUTION_MODE_STRICT,
        unchanged_state=("The workspace, configuration, and remote state were not modified.",),
        safe_next_action=(
            "Use an enrolled workspace_run_diagnostic template or workspace_run_profile instead. "
            "Relaxed execution mode can only be enabled by the repository owner via "
            f'repositories.{repo_id}.execution_mode = "relaxed" in configuration.'
        ),
    )


def _adhoc_error(
    message: str,
    code: ErrorCode,
    *,
    retryable: bool = False,
    mutation_possible: bool = False,
) -> RepoForgeError:
    unchanged = (
        (
            "No configuration, commit, or remote state changed; workspace paths named in the "
            "error may have changed.",
        )
        if mutation_possible
        else ("The workspace, configuration, commit history, and remote state were not modified.",)
    )
    return RepoForgeError(
        message,
        code=code,
        retryable=retryable,
        unchanged_state=unchanged,
        safe_next_action=(
            "Review the reported workspace paths and restore them explicitly before retrying."
            if mutation_possible
            else "Correct the reported condition and retry."
        ),
    )


def _assert_push_destination_not_protected(argv: tuple[str, ...], repo: RepositoryConfig) -> None:
    """Block an ad-hoc ``git push`` whose explicit refspec names a protected branch (#407).

    ``_assert_git_command_allowed`` (invoked via ``classify_adhoc_effect`` before this
    runs) already blocks force/mirror/delete forms regardless of destination; an ordinary
    non-force push straight to a protected branch was not previously checked at all on
    this path -- only at branch-attach time, never from ad-hoc. ``git push``/``git push
    <remote>`` with no explicit refspec is not checked here because its implicit
    destination is the workspace's own current branch, which cannot already be protected.
    """
    for destination in extract_push_destination_refs(argv):
        if destination in repo.protected_branches:
            raise circuit_breaker_blocked(
                CircuitBreakerCategory.PROTECTED_REF_WRITE,
                f"git push: destination branch {destination!r} is protected",
                safe_next_action=(
                    "Push to a non-protected branch through the ad-hoc runner, use "
                    "workspace_push against a reviewed branch, or ask the operator to "
                    "perform this write directly if the protected ref genuinely needs to "
                    "change."
                ),
                unchanged_state=(
                    "The workspace, configuration, and remote state were not modified.",
                ),
            )


def _gh_credential_scrub_env(workspace_root: Path) -> tuple[tuple[str, str], ...]:
    """Point ``gh``'s config/credential lookup at an empty, never-created scratch directory
    instead of the ambient ``$HOME/.config/gh`` (#407).

    Ad-hoc/``workspace_exec`` runs the operator's real ``HOME`` unmodified (many legitimate
    non-git tools need it), which means an ad-hoc ``gh`` call would otherwise silently
    authenticate with the operator's own on-disk OAuth token with no opt-in at all --
    exactly the gap #407 requires closed ("keep remote-write credentials out of generic
    shell environments by default"). Overriding only ``GH_CONFIG_DIR`` (not ``HOME``) is
    narrow enough to leave every other ambient-``HOME``-dependent tool untouched, mirroring
    the same override the typed GitHub-read path already applies in
    ``adapters/github/repository_observation.py:_base_environment``. A repository that
    enrolls the ``github`` credential profile (#381 catalog) and supplies ``GH_TOKEN``/
    ``GITHUB_TOKEN`` still authenticates normally -- ``gh`` prefers a token env var over its
    on-disk config regardless of ``GH_CONFIG_DIR``.
    """
    return (("GH_CONFIG_DIR", str(workspace_root / ".repoforge-empty-gh-config")),)


def _resolve_working_directory(workspace: Path, working_directory: str | None) -> Path:
    if working_directory is None:
        return workspace
    relative = normalize_relative_path(working_directory)
    unresolved = workspace / relative
    if unresolved.is_symlink():
        raise SecurityError("Ad-hoc working_directory cannot be a symlink")
    candidate = unresolved.resolve(strict=False)
    try:
        candidate.relative_to(workspace.resolve(strict=True))
    except ValueError as exc:
        raise SecurityError("Ad-hoc working_directory escapes workspace") from exc
    if not candidate.is_dir():
        raise WorkspaceError(f"Ad-hoc working_directory does not exist: {working_directory}")
    return candidate


def _argv_shape_key(argv: tuple[str, ...]) -> str:
    """A stable, bounded key identifying this exact argv shape for recurrence tracking."""
    return hashlib.sha256("\x00".join(argv).encode("utf-8", "surrogatepass")).hexdigest()


def _safe_error_message(text: str, *, limit: int = 2_000) -> str:
    cleaned = "".join(ch for ch in text if ch in "\n\t\r" or ord(ch) >= 32).strip()
    if not cleaned:
        return "Background workspace_run_adhoc failed"
    if len(cleaned) <= limit:
        return cleaned
    marker = "\n... durable error excerpt omitted ...\n"
    if limit <= len(marker):
        return cleaned[:limit]
    available = limit - len(marker)
    head_size = available // 2
    tail_size = available - head_size
    return cleaned[:head_size] + marker + cleaned[-tail_size:]


class WorkspaceAdhocRunner:
    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        operations: OperationManager | None = None,
        background_tasks: BackgroundTaskRunner | None = None,
    ) -> None:
        self.ctx = ctx
        self.operations = operations
        self.background_tasks = background_tasks
        self._cancel_tokens: dict[str, CancellationToken] = {}
        self._cancel_tokens_lock = threading.Lock()

    def execute_claimed(
        self,
        c: WorkspaceRunAdhocCommand,
        *,
        cancellation_token: CancellationToken,
        progress: Callable[[str, int, int, str, str], None],
    ) -> WorkspaceRunAdhocResult:
        """Execute already-claimed ad-hoc work without recursive admission."""
        progress("running", 0, 1, "commands", "running reviewed ad-hoc command")
        result = self.execute(
            replace(c, background=False, cancellation_token=cancellation_token),
            progress=progress,
        )
        if not isinstance(result, WorkspaceRunAdhocResult):
            raise RepoForgeError(
                "Claimed ad-hoc execution returned a background operation",
                code=ErrorCode.INTERNAL_ERROR,
            )
        progress("running", 1, 1, "commands", "completed reviewed ad-hoc command")
        return result

    def execute_sequence_claimed(
        self,
        c: WorkspaceRunAdhocSequenceCommand,
        *,
        cancellation_token: CancellationToken,
        progress: Callable[[str, int, int, str, str], None],
    ) -> WorkspaceRunAdhocSequenceResult:
        """Execute already-claimed ad-hoc sequence work without recursive admission."""
        progress("running", 0, 1, "commands", "running reviewed ad-hoc command sequence")
        result = self.execute_sequence(
            replace(c, cancellation_token=cancellation_token), progress=progress
        )
        progress("running", 1, 1, "commands", "completed reviewed ad-hoc command sequence")
        return result

    def execute(
        self,
        c: WorkspaceRunAdhocCommand,
        *,
        progress: Callable[[str, int, int, str, str], None] | None = None,
    ) -> WorkspaceRunAdhocResult | WorkspaceRunAdhocBackgroundResult:
        _, repo, path = self.ctx.workspace(c.workspace_id)
        if repo.execution_mode is not ExecutionMode.RELAXED:
            raise _strict_mode_error(repo.repo_id)
        if c.mutability not in {"read_only", "workspace"}:
            raise _adhoc_error(
                f"Ad-hoc mutability must be 'read_only' or 'workspace'; got {c.mutability!r}",
                ErrorCode.ADHOC_ARGV_INVALID,
            )
        argv: tuple[str, ...]
        if c.script is not None:
            shell = validate_adhoc_shell_runner(c.shell or "", repo.adhoc_shell_runners)
            script = validate_adhoc_script(c.script)
            argv = (shell, "-c", script)
            # classify_adhoc_effect only inspects when argv[0] == "git" literally, so the
            # script body as a whole is opaque to it exactly like every non-git argv
            # runner already is -- not a new gap. scan_script_for_blocked_git_forms
            # (#407) is a best-effort, additive scan for a git-shaped invocation embedded
            # in the script text; it is not authoritative (see its own docstring).
            scan_script_for_blocked_git_forms(script)
            for segment in extract_git_argv_segments(script):
                _assert_push_destination_not_protected(segment, repo)
            effect_class = None
        else:
            if c.argv is None:
                raise _adhoc_error(
                    "Ad-hoc command requires either argv or script",
                    ErrorCode.ADHOC_ARGV_INVALID,
                )
            argv = validate_adhoc_argv(c.argv, repo.effective_adhoc_runners())
            # Content-inspect the exact argv: blocks irreversible/history-rewriting git
            # forms (raising ADHOC_COMMAND_FORBIDDEN before any process starts) and infers
            # the git command's effect (#382). Non-git runners return None (opaque).
            effect_class = classify_adhoc_effect(argv)
            _assert_push_destination_not_protected(argv, repo)
        command_class = None if effect_class is None else effect_to_command_class(effect_class)
        declared_effect = _resolve_declared_effect(c.declared_effect, c.mutability)
        effect_mismatch = effect_class is not None and effect_exceeds_declaration(
            effect_class, declared_effect
        )
        stdin_text = validate_adhoc_stdin(c.stdin_text)
        declared_mutating = c.mutability == "workspace"
        effective_mutating = declared_mutating or command_class is CommandClass.MUTATING
        if command_class is CommandClass.MUTATING and not declared_mutating:
            raise _adhoc_error(
                f"{argv[0]} {argv[1] if len(argv) > 1 else ''}".strip()
                + " changes workspace or history state; call with mutability='workspace' and both "
                "expected_head_sha and expected_fingerprint so the run is bound to reviewed state",
                ErrorCode.ADHOC_ARGV_INVALID,
            )
        if effective_mutating:
            missing = [
                name
                for name, value in (
                    ("expected_head_sha", c.expected_head_sha),
                    ("expected_fingerprint", c.expected_fingerprint),
                )
                if value is None
            ]
            if missing:
                raise _adhoc_error(
                    "Mutating ad-hoc runs require an exact-state lock; missing: "
                    + ", ".join(missing),
                    ErrorCode.ADHOC_ARGV_INVALID,
                )
        command_cwd = _resolve_working_directory(path, c.working_directory)
        working_directory_display = str(command_cwd.relative_to(path.resolve(strict=True)) or ".")

        audit_details: dict[str, object] = {
            "workspace_id": c.workspace_id,
            "runner": argv[0],
            "argv_length": len(argv),
            "network_policy": _NETWORK_POLICY_LABEL,
            "expected_fingerprint": c.expected_fingerprint,
            "expected_head_sha": c.expected_head_sha,
            "mutability": c.mutability,
            "command_class": command_class.value if command_class is not None else None,
            "declared_effect": declared_effect.value,
            "effect_class": effect_class.value if effect_class is not None else None,
            "effect_mismatch": effect_mismatch,
            # Length only. Standard input is caller-supplied content that may carry a
            # patch, a token, or anything else, and the audit log is not the place for it.
            "stdin_length": len(stdin_text) if stdin_text is not None else 0,
        }

        def record_command_failure(exc: CommandError) -> None:
            audit_details["exit_code"] = exc.details.get("exit_code")
            if exc.details.get("cancelled"):
                audit_details["cancelled"] = True
            if exc.code is ErrorCode.COMMAND_TIMEOUT and not exc.safe_next_action:
                # The executor knows only the number of seconds it waited. Which reviewed
                # budget that number came from -- and that it is an operator-adjustable
                # field rather than a platform limit -- is known here, and withholding it
                # is what makes an agent invent a chunked workaround for a job that simply
                # needed a bigger budget or a profile of its own.
                exc.safe_next_action = _adhoc_timeout_remedy(repo.adhoc_timeout_seconds)
            if exc.code is ErrorCode.NOT_FOUND and not exc.safe_next_action:
                executable = exc.details.get("executable")
                if isinstance(executable, str):
                    exc.safe_next_action = _adhoc_missing_executable_remedy(
                        executable, repo.repo_id
                    )

        def run_body(
            cancel_token: CancellationToken | None,
            workspace_lock_held: bool = False,
        ) -> WorkspaceRunAdhocResult:
            lock_scope = (
                contextlib.nullcontext()
                if workspace_lock_held
                else self.ctx.locks.lock(c.workspace_id)
            )
            with lock_scope:
                fresh, locked_repo, locked_workspace = self.ctx.workspace(c.workspace_id)
                before_paths = self.ctx.git.changed_paths(locked_workspace, locked_repo)
                before = read_fingerprint(
                    self.ctx.fingerprint_cache, c.workspace_id, self.ctx.git, locked_workspace
                )
                before_fingerprint = before.fingerprint
                head_before = self.ctx.git.head_sha(locked_workspace)
                if (
                    c.expected_fingerprint is not None
                    and c.expected_fingerprint != before_fingerprint
                ):
                    raise WorkspaceError(
                        "Workspace changed since the verification plan was reviewed"
                    )
                if c.expected_head_sha is not None and c.expected_head_sha != head_before:
                    raise WorkspaceError(
                        "STALE_STATE: workspace HEAD changed since the ad-hoc run was reviewed",
                        code=ErrorCode.STALE_STATE,
                        retryable=True,
                        details={
                            "expected_head_sha": c.expected_head_sha,
                            "actual_head_sha": head_before,
                        },
                        unchanged_state=(
                            "No command was started; the workspace and remote state were not modified.",
                        ),
                        safe_next_action=(
                            "Read workspace_status for the current HEAD and fingerprint, then reissue "
                            "the ad-hoc run bound to the reviewed values."
                        ),
                    )
                audit_details["fingerprint_source"] = before.source
                audit_details["head_sha_before"] = head_before

                started = time.monotonic()
                result: CommandResult | None = None
                command_error: CommandError | None = None
                execution_evidence_data: dict[str, object] = {}
                execution_request = adhoc_execution_request(
                    workspace_id=c.workspace_id,
                    workspace_root=locked_workspace,
                    command_cwd=command_cwd,
                    argv=argv,
                    working_directory_policy=c.working_directory or ".",
                    timeout_seconds=locked_repo.adhoc_timeout_seconds,
                    output_limit=self.ctx.config.server.max_tool_output_chars,
                    cancel_token=cancel_token,
                    stdin_text=stdin_text,
                    extra_env=(
                        *resolve_credential_profile_env(locked_repo.credential_profiles),
                        *_gh_credential_scrub_env(locked_workspace),
                    ),
                )
                try:
                    with (
                        _command_progress_heartbeat(progress, label=argv[0]),
                        self.ctx.execution.prepare(execution_request) as session,
                    ):
                        result = session.execute(argv).result
                        inspection = session.inspect()
                        execution_evidence_data = to_data(
                            build_execution_evidence(
                                execution_request.requested_policy,
                                inspection.identity,
                                inspection.effective_policy,
                                inspection.warnings,
                            )
                        )
                except CommandError as exc:
                    command_error = exc
                    record_command_failure(exc)
                duration_ms = round((time.monotonic() - started) * 1000, 3)
                audit_details["duration_ms"] = duration_ms

                after = prime_fingerprint(
                    self.ctx.fingerprint_cache, c.workspace_id, self.ctx.git, locked_workspace
                )
                after_fingerprint = after.fingerprint
                fingerprint_changed = after_fingerprint != before_fingerprint
                audit_details["fingerprint_changed"] = fingerprint_changed
                # A git command RepoForge classified read-only that nonetheless changed the
                # working tree is a contract violation worth surfacing loudly (defense in depth
                # against misclassification); the verification invalidation below still protects
                # the commit gate.
                read_only_violation = (
                    command_class is CommandClass.READ_ONLY and fingerprint_changed
                )
                audit_details["read_only_violation"] = read_only_violation

                verification_invalidated = False
                if fingerprint_changed and fresh.last_verification is not None:
                    fresh.last_verification = None
                    self.ctx.store.save(fresh)
                    verification_invalidated = True

                try:
                    after_paths = self.ctx.git.changed_paths(locked_workspace, locked_repo)
                    combined_paths = sorted(set(before_paths) | set(after_paths))
                except SecurityError:
                    combined_paths = sorted(before_paths)

                if command_error is not None:
                    audit_details["exit_code"] = command_error.details.get("exit_code")
                    raise command_error

                assert result is not None
                nudge: str | None = None
                tracker = self.ctx.nudge_tracker
                if tracker is not None:
                    shape_key = _argv_shape_key(argv)
                    if tracker.observe_adhoc_argv(c.workspace_id, shape_key, self.ctx.now_epoch()):
                        nudge = (
                            "This exact ad-hoc command shape has recurred at least "
                            f"{_ARGV_RECURRENCE_THRESHOLD} times in this workspace. Consider "
                            "asking the repository owner to enroll it as a workspace_run_diagnostic "
                            "template so future runs are validated and evidence-tracked."
                        )

                next_actions: list[dict[str, object]] = []
                if read_only_violation:
                    next_actions.append(
                        {
                            "action": "workspace_status",
                            "reason": (
                                "A command declared read-only changed the workspace tree; review the "
                                "unexpected mutation and restore paths if it was unintended."
                            ),
                            "required": True,
                        }
                    )
                elif fingerprint_changed:
                    next_actions.append(
                        {
                            "action": "workspace_status",
                            "reason": "The ad-hoc command changed the workspace fingerprint.",
                            "required": True,
                        }
                    )
                next_actions.append(
                    {
                        "action": "workspace_run_profile",
                        "reason": _GATE_GUIDANCE,
                        "required": True,
                    }
                )

                changed_paths = combined_paths[:_MAX_CHANGED_PATHS_REPORTED]
                return WorkspaceRunAdhocResult(
                    workspace_id=c.workspace_id,
                    argv=list(argv),
                    runner=argv[0],
                    working_directory=working_directory_display,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    stdout_truncated=result.stdout_truncated,
                    stderr_truncated=result.stderr_truncated,
                    output_artifact_reference=result.output_artifact_reference,
                    output_artifact_status=result.output_artifact_status,
                    duration_ms=duration_ms,
                    fingerprint_before=before_fingerprint,
                    fingerprint_after=after_fingerprint,
                    fingerprint_changed=fingerprint_changed,
                    changed_paths=changed_paths,
                    changed_paths_truncated=len(combined_paths) > _MAX_CHANGED_PATHS_REPORTED,
                    head_sha=self.ctx.git.head_sha(locked_workspace),
                    head_sha_before=head_before,
                    mutability=c.mutability,
                    command_class=command_class.value if command_class is not None else None,
                    declared_effect=declared_effect.value,
                    effect_class=effect_class.value if effect_class is not None else None,
                    effect_mismatch=effect_mismatch,
                    read_only_violation=read_only_violation,
                    network_policy=_NETWORK_POLICY_LABEL,
                    evidence_only=True,
                    satisfies_commit_gate=False,
                    verification_invalidated=verification_invalidated,
                    gate_guidance=_GATE_GUIDANCE,
                    enrollment_nudge=nudge,
                    next_safe_actions=next_actions,
                    execution_evidence=execution_evidence_data,
                )

        if not c.background:
            return self.ctx.audited(
                _KIND,
                audit_details,
                lambda: run_body(c.cancellation_token, False),
            )

        return self._start_background(c, run_body, audit_details)

    def execute_sequence(
        self,
        c: WorkspaceRunAdhocSequenceCommand,
        *,
        progress: Callable[[str, int, int, str, str], None] | None = None,
    ) -> WorkspaceRunAdhocSequenceResult:
        """Bounded fail-fast argv sequence (#443): every element is validated up front
        (fails closed before element 1 runs if any element is invalid), then elements run
        in order under one held workspace lock, stopping at the first non-zero exit.
        Fingerprint/HEAD preconditions and verification-invalidation bracket the whole
        sequence, exactly like a single ad-hoc run's own bracketing -- not per element."""
        _, repo, path = self.ctx.workspace(c.workspace_id)
        if repo.execution_mode is not ExecutionMode.RELAXED:
            raise _strict_mode_error(repo.repo_id)
        if c.mutability not in {"read_only", "workspace"}:
            raise _adhoc_error(
                f"Ad-hoc mutability must be 'read_only' or 'workspace'; got {c.mutability!r}",
                ErrorCode.ADHOC_ARGV_INVALID,
            )
        validated = validate_adhoc_sequence(c.argv_sequence, repo.effective_adhoc_runners())
        effect_classes = [classify_adhoc_effect(argv) for argv in validated]
        for argv in validated:
            _assert_push_destination_not_protected(argv, repo)
        command_classes = [
            None if effect is None else effect_to_command_class(effect) for effect in effect_classes
        ]
        declared_effect = _resolve_declared_effect(c.declared_effect, c.mutability)
        sequence_effect_mismatch = any(
            effect_exceeds_declaration(effect, declared_effect)
            for effect in effect_classes
            if effect is not None
        )
        declared_mutating = c.mutability == "workspace"
        any_mutating_class = any(cls is CommandClass.MUTATING for cls in command_classes)
        effective_mutating = declared_mutating or any_mutating_class
        if any_mutating_class and not declared_mutating:
            raise _adhoc_error(
                "One or more commands in this sequence changes workspace or history "
                "state; call with mutability='workspace' and both expected_head_sha and "
                "expected_fingerprint so the run is bound to reviewed state",
                ErrorCode.ADHOC_ARGV_INVALID,
            )
        if effective_mutating:
            missing = [
                name
                for name, value in (
                    ("expected_head_sha", c.expected_head_sha),
                    ("expected_fingerprint", c.expected_fingerprint),
                )
                if value is None
            ]
            if missing:
                raise _adhoc_error(
                    "Mutating ad-hoc sequences require an exact-state lock; missing: "
                    + ", ".join(missing),
                    ErrorCode.ADHOC_ARGV_INVALID,
                )
        command_cwd = _resolve_working_directory(path, c.working_directory)

        audit_details: dict[str, object] = {
            "workspace_id": c.workspace_id,
            "sequence_length": len(validated),
            "network_policy": _NETWORK_POLICY_LABEL,
            "expected_fingerprint": c.expected_fingerprint,
            "expected_head_sha": c.expected_head_sha,
            "mutability": c.mutability,
            "declared_effect": declared_effect.value,
            "effect_mismatch": sequence_effect_mismatch,
        }

        def run_body() -> WorkspaceRunAdhocSequenceResult:
            with self.ctx.locks.lock(c.workspace_id):
                fresh, locked_repo, locked_workspace = self.ctx.workspace(c.workspace_id)
                before_paths = self.ctx.git.changed_paths(locked_workspace, locked_repo)
                before = read_fingerprint(
                    self.ctx.fingerprint_cache, c.workspace_id, self.ctx.git, locked_workspace
                )
                before_fingerprint = before.fingerprint
                head_before = self.ctx.git.head_sha(locked_workspace)
                if (
                    c.expected_fingerprint is not None
                    and c.expected_fingerprint != before_fingerprint
                ):
                    raise WorkspaceError(
                        "Workspace changed since the verification plan was reviewed"
                    )
                if c.expected_head_sha is not None and c.expected_head_sha != head_before:
                    raise WorkspaceError(
                        "STALE_STATE: workspace HEAD changed since the ad-hoc sequence was "
                        "reviewed",
                        code=ErrorCode.STALE_STATE,
                        retryable=True,
                        details={
                            "expected_head_sha": c.expected_head_sha,
                            "actual_head_sha": head_before,
                        },
                        unchanged_state=(
                            "No command was started; the workspace and remote state were "
                            "not modified.",
                        ),
                        safe_next_action=(
                            "Read workspace_status for the current HEAD and fingerprint, "
                            "then reissue the ad-hoc sequence bound to the reviewed values."
                        ),
                    )
                audit_details["fingerprint_source"] = before.source
                audit_details["head_sha_before"] = head_before

                commands: list[dict[str, object]] = []
                stopped_early = False
                command_error: CommandError | None = None
                sequence_execution_evidence: dict[str, object] = {}
                # One total budget for the whole sequence, not `adhoc_timeout_seconds`
                # per element: without this, an N-element sequence could hold the
                # workspace lock for up to N times the repository's single-command
                # budget (review finding F-007).
                sequence_deadline = time.monotonic() + locked_repo.adhoc_timeout_seconds
                for index, argv in enumerate(validated):
                    remaining = sequence_deadline - time.monotonic()
                    if remaining <= 0:
                        stopped_early = True
                        audit_details["failed_element_index"] = index
                        audit_details["stop_reason"] = "sequence_budget_exhausted"
                        command_error = CommandError(
                            "SEQUENCE_BUDGET_EXHAUSTED: this sequence's total time "
                            f"budget ({locked_repo.adhoc_timeout_seconds}s) was exhausted "
                            f"before element {index + 1}/{len(validated)} could start",
                            code=ErrorCode.COMMAND_TIMEOUT,
                            retryable=False,
                            safe_next_action=(
                                "Split this into fewer commands per call, or ask the "
                                "repository owner to raise adhoc_timeout_seconds."
                            ),
                        )
                        break
                    element_timeout = max(1, min(locked_repo.adhoc_timeout_seconds, int(remaining)))
                    started = time.monotonic()
                    execution_request = adhoc_execution_request(
                        workspace_id=c.workspace_id,
                        workspace_root=locked_workspace,
                        command_cwd=command_cwd,
                        argv=argv,
                        working_directory_policy=c.working_directory or ".",
                        timeout_seconds=element_timeout,
                        output_limit=self.ctx.config.server.max_tool_output_chars,
                        cancel_token=c.cancellation_token,
                        stdin_text=None,
                        extra_env=(
                            *resolve_credential_profile_env(locked_repo.credential_profiles),
                            *_gh_credential_scrub_env(locked_workspace),
                        ),
                    )
                    try:
                        with (
                            _command_progress_heartbeat(
                                progress, label=f"{argv[0]} (element {index + 1}/{len(validated)})"
                            ),
                            self.ctx.execution.prepare(execution_request) as session,
                        ):
                            result = session.execute(argv).result
                            inspection = session.inspect()
                            sequence_execution_evidence = to_data(
                                build_execution_evidence(
                                    execution_request.requested_policy,
                                    inspection.identity,
                                    inspection.effective_policy,
                                    inspection.warnings,
                                )
                            )
                    except CommandError as exc:
                        command_error = exc
                        audit_details["failed_element_index"] = index
                        audit_details["exit_code"] = exc.details.get("exit_code")
                        if exc.code is ErrorCode.NOT_FOUND and not exc.safe_next_action:
                            executable = exc.details.get("executable")
                            if isinstance(executable, str):
                                exc.safe_next_action = _adhoc_missing_executable_remedy(
                                    executable, repo.repo_id
                                )
                        break
                    duration_ms = round((time.monotonic() - started) * 1000, 3)
                    commands.append(
                        {
                            "argv": list(argv),
                            "returncode": result.returncode,
                            "duration_ms": duration_ms,
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                            "output_artifact_reference": result.output_artifact_reference,
                            "output_artifact_status": result.output_artifact_status,
                        }
                    )
                    if result.returncode != 0:
                        stopped_early = index < len(validated) - 1
                        break

                after = prime_fingerprint(
                    self.ctx.fingerprint_cache, c.workspace_id, self.ctx.git, locked_workspace
                )
                after_fingerprint = after.fingerprint
                fingerprint_changed = after_fingerprint != before_fingerprint
                audit_details["fingerprint_changed"] = fingerprint_changed
                read_only_violation = not effective_mutating and fingerprint_changed
                audit_details["read_only_violation"] = read_only_violation

                verification_invalidated = False
                if fingerprint_changed and fresh.last_verification is not None:
                    fresh.last_verification = None
                    self.ctx.store.save(fresh)
                    verification_invalidated = True

                try:
                    after_paths = self.ctx.git.changed_paths(locked_workspace, locked_repo)
                    combined_paths = sorted(set(before_paths) | set(after_paths))
                except SecurityError:
                    combined_paths = sorted(before_paths)

                if command_error is not None:
                    raise command_error

                next_actions: list[dict[str, object]] = []
                if read_only_violation:
                    next_actions.append(
                        {
                            "action": "workspace_status",
                            "reason": (
                                "A command sequence declared read-only changed the "
                                "workspace tree; review the unexpected mutation and "
                                "restore paths if it was unintended."
                            ),
                            "required": True,
                        }
                    )
                elif fingerprint_changed:
                    next_actions.append(
                        {
                            "action": "workspace_status",
                            "reason": "The ad-hoc sequence changed the workspace fingerprint.",
                            "required": True,
                        }
                    )
                next_actions.append(
                    {
                        "action": "workspace_run_profile",
                        "reason": _GATE_GUIDANCE,
                        "required": True,
                    }
                )

                changed_paths = combined_paths[:_MAX_CHANGED_PATHS_REPORTED]
                return WorkspaceRunAdhocSequenceResult(
                    workspace_id=c.workspace_id,
                    commands=commands,
                    stopped_early=stopped_early,
                    fingerprint_before=before_fingerprint,
                    fingerprint_after=after_fingerprint,
                    fingerprint_changed=fingerprint_changed,
                    changed_paths=changed_paths,
                    changed_paths_truncated=len(combined_paths) > _MAX_CHANGED_PATHS_REPORTED,
                    head_sha=self.ctx.git.head_sha(locked_workspace),
                    head_sha_before=head_before,
                    mutability=c.mutability,
                    declared_effect=declared_effect.value,
                    effect_mismatch=sequence_effect_mismatch,
                    read_only_violation=read_only_violation,
                    all_content_inspected=all(cls is not None for cls in command_classes),
                    network_policy=_NETWORK_POLICY_LABEL,
                    evidence_only=True,
                    satisfies_commit_gate=False,
                    verification_invalidated=verification_invalidated,
                    gate_guidance=_GATE_GUIDANCE,
                    next_safe_actions=next_actions,
                    execution_evidence=sequence_execution_evidence,
                )

        return self.ctx.audited(_KIND, audit_details, run_body)

    # ------------------------------------------------------------------
    # Background execution via the existing durable-operations pipeline.
    # Mirrors WorkspaceProfileRunner's background path (same admission,
    # cancellation, and result-persistence contract).
    # ------------------------------------------------------------------

    def _register_cancel_token(self, operation_id: str, token: CancellationToken) -> None:
        with self._cancel_tokens_lock:
            self._cancel_tokens[operation_id] = token

    def _unregister_cancel_token(self, operation_id: str) -> None:
        with self._cancel_tokens_lock:
            self._cancel_tokens.pop(operation_id, None)

    def _persist_worker_binding(self, operation_id: str, child_pid: int) -> None:
        """Durably record the spawned child so a later process can reap/cancel it.

        A background command's subprocess uses ``start_new_session=True`` so its
        pgid equals its own pid. Runs on the executor thread the moment the child
        is bound; any failure here must never disturb the run itself.
        """
        bindings = self.ctx.worker_bindings
        if bindings is None or child_pid <= 0:
            return
        reaper = self.ctx.reaper
        child_token = reaper.read_start_token(child_pid) if reaper is not None else None
        server_pid = os.getpid()
        server_token = reaper.read_start_token(server_pid) if reaper is not None else None
        with contextlib.suppress(Exception):
            binding = OperationWorkerBinding(
                operation_id=operation_id,
                child_pid=child_pid,
                child_pgid=child_pid,
                child_start_token=child_token,
                server_pid=server_pid,
                server_start_token=server_token,
                owner_generation=getattr(self.ctx, "config_generation", 0) or None,
                created_at=self.ctx.clock.now_iso(),
            )
            identity_store = getattr(self.ctx, "operation_identities", None)
            if identity_store is not None:
                identity = identity_store.read(operation_id)
                if identity is not None:
                    binding = bind_worker_identity(binding, identity.value.reference)
            bindings.put(binding)

    def _delete_worker_binding(self, operation_id: str) -> None:
        bindings = self.ctx.worker_bindings
        if bindings is None:
            return
        with contextlib.suppress(Exception):
            bindings.delete(operation_id)

    def request_live_cancel(self, operation_id: str) -> bool:
        with self._cancel_tokens_lock:
            token = self._cancel_tokens.get(operation_id)
        if token is not None:
            token.cancel()
            return True
        # Cross-process fallback: the in-memory token lives only in the process
        # that launched the run. After a restart a cancel request lands in a
        # different process, so signal the durably bound child group directly.
        bindings = self.ctx.worker_bindings
        reaper = self.ctx.reaper
        if bindings is None or reaper is None:
            return False
        binding = None
        with contextlib.suppress(Exception):
            binding = bindings.get(operation_id)
        if binding is None:
            return False
        outcome = None
        with contextlib.suppress(Exception):
            outcome = reaper.reap(binding)
        if outcome is not None and not outcome.still_alive:
            bindings.delete_if_unchanged(binding)
        return outcome is not None

    def _start_background(
        self,
        c: WorkspaceRunAdhocCommand,
        run_body: Callable[[CancellationToken | None, bool], WorkspaceRunAdhocResult],
        audit_details: dict[str, object],
    ) -> WorkspaceRunAdhocBackgroundResult:
        operations = self.operations
        background_tasks = self.background_tasks
        result_store = self.ctx.operation_result_store
        if operations is None or background_tasks is None or result_store is None:
            raise RepoForgeError(
                "Background workspace_run_adhoc requires the durable operations manager, "
                "operation result store, and background task runner to be configured",
                code=ErrorCode.CONFIG_INVALID,
            )

        lock_cm = self.ctx.locks.lock(
            c.workspace_id,
            timeout_seconds=0,
            metadata={"purpose": "workspace_run_adhoc_background"},
        )
        lock_cm.__enter__()

        now = self.ctx.clock.now_iso()
        _record, repo, _workspace = self.ctx.workspace(c.workspace_id)
        owner_id = f"worker-{self.ctx.ids.new_hex(24)}"
        lease_expires_at = (
            datetime.fromisoformat(now)
            + timedelta(seconds=repo.adhoc_timeout_seconds + _OPERATION_LEASE_GRACE_SECONDS)
        ).isoformat()
        try:
            with self.ctx.locks.lock(
                "background-adhoc-admission",
                timeout_seconds=2,
                metadata={"purpose": "background_adhoc_admission"},
            ):
                cap = self.ctx.config.server.max_background_profiles
                running = sum(
                    1
                    for candidate in operations.list_records(max_records=2_000).records
                    if candidate.kind == _KIND and candidate.state is OperationState.RUNNING
                )
                if running >= cap:
                    raise RepoForgeError(
                        f"Background workspace_run_adhoc is at its configured concurrency cap "
                        f"of {cap} running operation(s)",
                        code=ErrorCode.RUNTIME_UNAVAILABLE,
                        retryable=True,
                        safe_next_action=(
                            f"Wait for a running background ad-hoc run to finish "
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
        cancel_token = CancellationToken(
            on_bind=lambda child_pid: self._persist_worker_binding(operation_id, child_pid)
        )
        self._register_cancel_token(operation_id, cancel_token)

        def finish_terminal(exc: Exception | None, result: WorkspaceRunAdhocResult | None) -> None:
            finish_now = self.ctx.clock.now_iso()
            try:
                operations.status(operation_id)
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
            if cancel_token.is_cancelled():
                with contextlib.suppress(RepoForgeError):
                    operations.cancelled(
                        operation_id,
                        owner_id=owner_id,
                        now=finish_now,
                    )
                return
            failure = exc or RepoForgeError(
                "Background ad-hoc run completed without a result", code=ErrorCode.INTERNAL_ERROR
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
            result: WorkspaceRunAdhocResult | None = None
            try:
                try:
                    result = self.ctx.audited(
                        _KIND,
                        audit_details,
                        lambda: run_body(cancel_token, True),
                    )
                except Exception as exc:
                    failure = exc
            finally:
                self._unregister_cancel_token(operation_id)
                lock_cm.__exit__(None, None, None)
            finish_terminal(failure, result)
            self._delete_worker_binding(operation_id)

        scheduled = background_tasks.submit(operation_id, run)
        if not scheduled:
            self._unregister_cancel_token(operation_id)
            lock_cm.__exit__(None, None, None)
            operations.fail(
                operation_id,
                error_code=ErrorCode.INTERNAL_ERROR.value,
                error_message="Background task runner could not accept the ad-hoc run",
                owner_id=owner_id,
            )
            raise RepoForgeError(
                "Background task runner rejected the ad-hoc run", code=ErrorCode.INTERNAL_ERROR
            )

        return WorkspaceRunAdhocBackgroundResult(
            operation_id=operation_id,
            phase="running",
            safe_next_action=(
                "Poll operation_status; the workspace lock is held until the run completes."
            ),
        )
