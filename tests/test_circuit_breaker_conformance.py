"""Conformance suite for the non-bypassable circuit-breaker contract (#385).

Every backend RepoForge ships must satisfy the assertions in this module -- today that is
only the ``native_uncontained`` backend under ``governed_strict``/``governed_relaxed``
(#383's ``trusted_host`` lease and #384's ``sandboxed`` backend do not exist yet, which is
exactly what AC4/AC5 require: no backend-conformance suite may depend on an unsafe backend
existing first). When a second backend lands, the trigger functions below should grow a
backend parameter rather than being duplicated into a second test module -- the assertions
themselves (which error code, which hook point) must not change per backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.adapters.subprocess import SubprocessCommandExecutor
from repoforge.config import RepositoryConfig, ServerConfig
from repoforge.domain import policy
from repoforge.domain.adhoc import classify_adhoc_command
from repoforge.domain.circuit_breakers import (
    CIRCUIT_BREAKER_MATRIX,
    CircuitBreakerCategory,
    CircuitBreakerSpec,
    EnforcementHookPoint,
)
from repoforge.domain.errors import RepoForgeError
from repoforge.domain.execution_environment import NATIVE_ADVISORY_ENFORCEMENT, EnforcementLevel


def test_matrix_has_exactly_one_entry_per_category() -> None:
    assert {spec.category for spec in CIRCUIT_BREAKER_MATRIX} == set(CircuitBreakerCategory)


def test_enforced_categories_each_have_their_own_error_code() -> None:
    """#385 AC3: "dedicated typed errors," not one shared bucket -- a regression that
    collapses two enforced categories back onto the same code must fail here."""
    enforced = [spec for spec in CIRCUIT_BREAKER_MATRIX if spec.enforced_today]
    codes = [spec.error_code for spec in enforced]
    assert len(codes) == len(set(codes))
    assert len(enforced) >= 4


def test_hook_points_are_named_from_the_closed_set() -> None:
    for spec in CIRCUIT_BREAKER_MATRIX:
        assert spec.hook_point in EnforcementHookPoint


_DEMO_REPO = RepositoryConfig(
    repo_id="demo",
    path=Path("/dev/null"),
    protected_branches=("main", "master"),
)


def _trigger_protected_ref_write() -> None:
    policy.validate_adopted_branch("main", _DEMO_REPO)


def _trigger_destructive_remote_operation() -> None:
    classify_adhoc_command(("git", "push", "--force", "origin", "main"))


def _trigger_irreversible_local_operation() -> None:
    classify_adhoc_command(("git", "clean", "--force"))


def _trigger_secret_material_exposure(tmp_path: Path) -> None:
    executor = SubprocessCommandExecutor(ServerConfig(tmp_path / "w", tmp_path / "s"))
    executor.run_isolated(
        ("echo", "s3cr3t-token"),
        cwd=tmp_path,
        environment={},
        secrets=("s3cr3t-token",),
    )


_TRIGGERS = {
    CircuitBreakerCategory.PROTECTED_REF_WRITE: lambda tmp_path: _trigger_protected_ref_write(),
    CircuitBreakerCategory.DESTRUCTIVE_REMOTE_OPERATION: (
        lambda tmp_path: _trigger_destructive_remote_operation()
    ),
    CircuitBreakerCategory.IRREVERSIBLE_LOCAL_OPERATION: (
        lambda tmp_path: _trigger_irreversible_local_operation()
    ),
    CircuitBreakerCategory.SECRET_MATERIAL_EXPOSURE: _trigger_secret_material_exposure,
}


@pytest.mark.parametrize(
    "spec",
    [spec for spec in CIRCUIT_BREAKER_MATRIX if spec.enforced_today],
    ids=lambda spec: spec.category.value,
)
def test_enforced_category_raises_its_declared_code_and_attribution(
    spec: CircuitBreakerSpec, tmp_path: Path
) -> None:
    trigger = _TRIGGERS[spec.category]
    with pytest.raises(RepoForgeError) as excinfo:
        trigger(tmp_path)
    assert excinfo.value.code is spec.error_code
    if spec.attributed:
        assert excinfo.value.details.get("circuit_breaker_category") == spec.category.value
        assert excinfo.value.details.get("enforcement_hook_point") == spec.hook_point.value


def test_every_enforced_category_has_a_registered_trigger() -> None:
    """Fails closed if a category is marked enforced_today=True without a live trigger
    above to prove it -- the alternative (silently skipping) would let the matrix claim
    enforcement the suite never actually exercises."""
    enforced_categories = {spec.category for spec in CIRCUIT_BREAKER_MATRIX if spec.enforced_today}
    assert enforced_categories <= set(_TRIGGERS)


def test_structurally_prevented_categories_are_explicitly_marked_not_enforced() -> None:
    """CONTROL_PLANE_STATE_MUTATION and REPOSITORY_DELETION have no raise site to trigger
    because no tool exposes either capability at all -- the matrix must say so explicitly
    (enforced_today=False) rather than implying an exception-based guard that doesn't
    exist."""
    structural = {
        CircuitBreakerCategory.CONTROL_PLANE_STATE_MUTATION,
        CircuitBreakerCategory.REPOSITORY_DELETION,
    }
    for spec in CIRCUIT_BREAKER_MATRIX:
        if spec.category in structural:
            assert spec.enforced_today is False


def test_enforcement_assessment_grades_every_dimension_honestly() -> None:
    """#385: the resource-limit dimensions are graded, not silently assumed enforced.
    Only timeout/output/process_cleanup are truthfully ENFORCED on the native backend
    today; the rest -- including the three dimensions #385 adds (socket, mount, symlink)
    -- must stay UNSUPPORTED until a backend actually enforces them (#384)."""
    payload = NATIVE_ADVISORY_ENFORCEMENT.payload()
    assert set(payload) == {
        "network",
        "filesystem",
        "timeout",
        "output",
        "process_cleanup",
        "cpu",
        "memory",
        "disk",
        "subprocess_count",
        "network_bytes",
        "socket",
        "mount",
        "symlink",
    }
    for level in payload.values():
        assert level in {member.value for member in EnforcementLevel}
    truthfully_enforced = {"timeout", "output", "process_cleanup"}
    for dimension in truthfully_enforced:
        assert payload[dimension] == EnforcementLevel.ENFORCED.value
    for dimension in set(payload) - truthfully_enforced:
        assert payload[dimension] != EnforcementLevel.ENFORCED.value
