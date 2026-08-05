"""Non-bypassable circuit-breaker contract (#385).

The categories every host-bypass (#383) and sandbox (#384) backend must enforce before
broad shell execution is authorized under any posture, independent of either of those
backends existing yet -- this module is the prerequisite contract, not a consumer of it.
See ``docs/architecture/autonomy-policy-model.md`` §6 (non-bypassable controls) and §15
(this contract) for the policy rationale; cite terms from there rather than re-deriving
them here.

Resource-limit dimensions (disk, memory, CPU, socket, mount, symlink, ...) are graded by
:class:`~repoforge.domain.execution_environment.EnforcementAssessment`, not duplicated in
:data:`CIRCUIT_BREAKER_MATRIX` -- that dataclass is already the source of truth for "is
this dimension actually enforced," and #385 only adds the three dimensions (socket,
mount, symlink) that were previously undeclared. This module covers the six
*protected-operation* categories #385 additionally requires dedicated typed errors for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .errors import ErrorCode, SecurityError


class CircuitBreakerCategory(str, Enum):
    """One entry per non-bypassable control §6 of the autonomy policy model names.

    Not every category has an active reject call site today: ``CONTROL_PLANE_STATE_MUTATION``
    and ``REPOSITORY_DELETION`` are prevented by construction (no tool exposes a path to
    either effect at all) rather than by an exception a caller can trigger and observe.
    That is itself the hard safeguard -- see :data:`CIRCUIT_BREAKER_MATRIX`'s
    ``enforced_today`` field, which distinguishes "blocked by an explicit typed error" from
    "blocked because there is nothing to call."
    """

    PROTECTED_REF_WRITE = "protected_ref_write"
    DESTRUCTIVE_REMOTE_OPERATION = "destructive_remote_operation"
    IRREVERSIBLE_LOCAL_OPERATION = "irreversible_local_operation"
    SECRET_MATERIAL_EXPOSURE = "secret_material_exposure"
    CONTROL_PLANE_STATE_MUTATION = "control_plane_state_mutation"
    REPOSITORY_DELETION = "repository_deletion"


class EnforcementHookPoint(str, Enum):
    """When, in a command's lifecycle, a circuit breaker fires.

    Names the existing three-phase convention every ad-hoc execution call site already
    follows (``ExecutionCoordinator.prepare()`` -> ``CoordinatedExecutionSession.execute()``
    -> ``.inspect()``, e.g. ``application/workspace/run_adhoc.py``) rather than introducing a
    new hook-registration mechanism -- #385's AC1 asks the contract to define these phases,
    not to restructure how call sites reach them.
    """

    PRE_LAUNCH = "pre_launch"
    RUNTIME = "runtime"
    POST_EXECUTION = "post_execution"


@dataclass(frozen=True, slots=True)
class CircuitBreakerSpec:
    """One row of the circuit-breaker policy matrix (#385 AC1).

    ``attributed`` is true only for raise sites that construct their error through
    :func:`circuit_breaker_blocked`, which stamps the ``circuit_breaker_category`` and
    ``enforcement_hook_point`` keys into ``details``. ``SECRET_MATERIAL_EXPOSURE`` predates
    #385 and is raised inside a low-level subprocess adapter shared by many unrelated
    callers (``CommandExecutor.run_isolated`` etc.) -- routing it through the new helper
    would mean touching that shared, already-hardened executor for attribution metadata
    alone. Its dedicated ``ErrorCode`` (already distinct before #385) is still the AC3
    proof; only the hook-point/category detail payload is unavailable for it.
    """

    category: CircuitBreakerCategory
    error_code: ErrorCode
    hook_point: EnforcementHookPoint
    enforced_today: bool
    implementation_note: str
    attributed: bool = True


CIRCUIT_BREAKER_MATRIX: tuple[CircuitBreakerSpec, ...] = (
    CircuitBreakerSpec(
        category=CircuitBreakerCategory.PROTECTED_REF_WRITE,
        error_code=ErrorCode.PROTECTED_REF_WRITE_BLOCKED,
        hook_point=EnforcementHookPoint.PRE_LAUNCH,
        enforced_today=True,
        implementation_note="domain/policy.py:validate_adopted_branch",
    ),
    CircuitBreakerSpec(
        category=CircuitBreakerCategory.DESTRUCTIVE_REMOTE_OPERATION,
        error_code=ErrorCode.DESTRUCTIVE_REMOTE_OPERATION_BLOCKED,
        hook_point=EnforcementHookPoint.PRE_LAUNCH,
        enforced_today=True,
        implementation_note=(
            "domain/adhoc.py:_assert_git_command_allowed (force/mirror/delete push forms, "
            "malformed --force-with-lease)"
        ),
    ),
    CircuitBreakerSpec(
        category=CircuitBreakerCategory.IRREVERSIBLE_LOCAL_OPERATION,
        error_code=ErrorCode.IRREVERSIBLE_LOCAL_OPERATION_BLOCKED,
        hook_point=EnforcementHookPoint.PRE_LAUNCH,
        enforced_today=True,
        implementation_note=(
            "domain/adhoc.py:_assert_git_command_allowed (history-rewriting subcommands, "
            "--exec/-x, reflog expire/delete, update-ref -d, clean --force)"
        ),
    ),
    CircuitBreakerSpec(
        category=CircuitBreakerCategory.SECRET_MATERIAL_EXPOSURE,
        error_code=ErrorCode.CREDENTIAL_LEAK_BLOCKED,
        hook_point=EnforcementHookPoint.PRE_LAUNCH,
        enforced_today=True,
        implementation_note=(
            "adapters/subprocess/command_executor.py, adapters/git/transport.py, "
            "domain/repository_auth_broker.py; sanitize_egress_data() in domain/egress.py "
            "additionally redacts every MCP tool response at the service.py._result() boundary"
        ),
        attributed=False,
    ),
    CircuitBreakerSpec(
        category=CircuitBreakerCategory.CONTROL_PLANE_STATE_MUTATION,
        error_code=ErrorCode.SECURITY_POLICY_VIOLATION,
        hook_point=EnforcementHookPoint.PRE_LAUNCH,
        enforced_today=False,
        implementation_note=(
            "no tool exposes a write path to RepoForge control-plane state outside its own "
            "typed write path; prevented by construction, not by a raise site to test against"
        ),
    ),
    CircuitBreakerSpec(
        category=CircuitBreakerCategory.REPOSITORY_DELETION,
        error_code=ErrorCode.SECURITY_POLICY_VIOLATION,
        hook_point=EnforcementHookPoint.PRE_LAUNCH,
        enforced_today=False,
        implementation_note=(
            "no tool exposes repository deletion; prevented by construction, not by a raise "
            "site to test against"
        ),
    ),
)

_SPEC_BY_CATEGORY: dict[CircuitBreakerCategory, CircuitBreakerSpec] = {
    spec.category: spec for spec in CIRCUIT_BREAKER_MATRIX
}


def circuit_breaker_blocked(
    category: CircuitBreakerCategory,
    message: str,
    *,
    safe_next_action: str,
    unchanged_state: tuple[str, ...] = (),
) -> SecurityError:
    """Construct the dedicated typed error a circuit breaker in ``category`` must raise.

    Every call site enforcing a category in :data:`CIRCUIT_BREAKER_MATRIX` should raise
    through this helper rather than constructing :class:`SecurityError` directly, so the
    error code and hook-point attribution stay bound to the matrix instead of drifting
    from it at individual call sites.
    """

    spec = _SPEC_BY_CATEGORY[category]
    return SecurityError(
        message,
        code=spec.error_code,
        safe_next_action=safe_next_action,
        unchanged_state=unchanged_state,
        details={
            "circuit_breaker_category": category.value,
            "enforcement_hook_point": spec.hook_point.value,
        },
    )


@dataclass(frozen=True, slots=True)
class OperatorOverrideAttestation:
    """The shape an operator-only override or waiver record must attest to justify
    bypassing a non-bypassable-by-default control (autonomy-policy-model.md §6 point 1,
    point 6).

    This is an interface/contract only: #385 does not implement issuance, storage, or
    wiring into any raise site above. #375 (protected-ref override authority) and #395
    (publication-override waiver) own building the actual override/waiver mechanisms,
    each of which must produce a value of this shape -- not a shared runtime type they
    both instantiate, since neither mechanism exists yet and guessing their storage or
    issuance needs ahead of that work would risk designing against the wrong constraints.
    """

    override_id: str
    category: CircuitBreakerCategory
    actor: str
    reason: str
    granted_at: datetime
    expires_at: datetime | None
