"""Typed verification DAGs and content-addressed iteration-cache contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, NoReturn

from .errors import ErrorCode, RepoForgeError
from .execution_plan import (
    ExecutionPlan,
    PlanStageBoundary,
    PlanStageMutability,
    StageFailurePolicy,
    validate_execution_plan,
)
from .execution_receipt import ArtifactDigest

VERIFICATION_DAG_SCHEMA_VERSION = 1
ITERATION_CACHE_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_PLAN_ID = re.compile(r"^plan-[0-9a-f]{24}$")
_RECEIPT_ID = re.compile(r"^receipt-[0-9a-f]{24}$")
_CACHE_ID = re.compile(r"^cache-[0-9a-f]{24}$")
_SHA64 = re.compile(r"^[0-9a-f]{64}$")


class CachePolicy(str, Enum):
    DISABLED = "disabled"
    READ_ONLY = "read_only"


class DagFailureSeverity(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class CacheMissReason(str, Enum):
    NOT_CACHEABLE = "not_cacheable"
    NOT_FOUND = "not_found"
    CORRUPT = "corrupt"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_MISMATCH = "artifact_mismatch"


@dataclass(frozen=True, slots=True)
class VerificationDagStage:
    stage_id: str
    version: int
    dependencies: tuple[str, ...]
    kind: str
    target: str
    selector: str | None
    environment_adapter: str
    working_directory: str
    timeout_seconds: int
    network_policy: str
    filesystem_policy: str
    process_policy: str
    credential_policy: str
    mutability: str
    required_risk_level: str
    artifact_paths: tuple[str, ...]
    cache_policy: CachePolicy
    failure_severity: DagFailureSeverity
    final: bool

    def __post_init__(self) -> None:
        for field, value in (
            ("stage_id", self.stage_id),
            ("kind", self.kind),
            ("target", self.target),
            ("environment_adapter", self.environment_adapter),
            ("network_policy", self.network_policy),
            ("filesystem_policy", self.filesystem_policy),
            ("process_policy", self.process_policy),
            ("credential_policy", self.credential_policy),
            ("mutability", self.mutability),
            ("required_risk_level", self.required_risk_level),
        ):
            if _SAFE_ID.fullmatch(value) is None:
                _invalid(f"Verification DAG {field} is invalid")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            _invalid("Verification DAG stage version must be a positive integer")
        if len(self.dependencies) > 64 or len(set(self.dependencies)) != len(self.dependencies):
            _invalid("Verification DAG dependencies must be unique and bounded")
        if self.stage_id in self.dependencies:
            _invalid("Verification DAG stage cannot depend on itself")
        if not self.working_directory or len(self.working_directory) > 512:
            _invalid("Verification DAG working directory is invalid")
        if not 1 <= self.timeout_seconds <= 86_400:
            _invalid("Verification DAG stage timeout is invalid")
        if self.mutability not in {"read_only", "workspace_write"}:
            _invalid("Verification DAG mutability is unsupported")
        if self.cache_policy is CachePolicy.READ_ONLY and (
            self.mutability != "read_only" or self.final
        ):
            _invalid("Only read-only non-final DAG stages may be cacheable")
        if self.final and self.cache_policy is not CachePolicy.DISABLED:
            _invalid("Final verification stage must be non-cacheable")
        if len(self.artifact_paths) > 64:
            _invalid("Verification DAG artifact list exceeds the reviewed bound")
        for path in self.artifact_paths:
            if (
                not path
                or len(path) > 512
                or path.startswith("/")
                or ".." in path.split("/")
                or any(ord(character) < 32 for character in path)
            ):
                _invalid("Verification DAG artifact path is unsafe")

    def definition_payload(self) -> dict[str, object]:
        return {
            "artifact_paths": list(self.artifact_paths),
            "cache_policy": self.cache_policy.value,
            "credential_policy": self.credential_policy,
            "dependencies": list(self.dependencies),
            "environment_adapter": self.environment_adapter,
            "failure_severity": self.failure_severity.value,
            "filesystem_policy": self.filesystem_policy,
            "final": self.final,
            "kind": self.kind,
            "mutability": self.mutability,
            "network_policy": self.network_policy,
            "process_policy": self.process_policy,
            "required_risk_level": self.required_risk_level,
            "selector": self.selector,
            "stage_id": self.stage_id,
            "target": self.target,
            "timeout_seconds": self.timeout_seconds,
            "version": self.version,
            "working_directory": self.working_directory,
        }

    @property
    def definition_hash(self) -> str:
        return _digest(self.definition_payload())


@dataclass(frozen=True, slots=True)
class VerificationDag:
    dag_id: str
    dag_hash: str
    plan_id: str
    plan_hash: str
    stages: tuple[VerificationDagStage, ...]
    final_stage_id: str
    schema_version: int = VERIFICATION_DAG_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class IterationCacheKey:
    cache_key: str
    workspace_identity: str
    declared_input_hash: str
    stage_definition_hash: str
    target_identity: str
    working_directory: str
    environment_identity: str
    toolchain_hash: str
    lockfile_hash: str
    config_generation: str
    policy_hash: str
    provider_hash: str
    network_policy: str
    dependency_receipt_hashes: tuple[str, ...]
    schema_version: int = ITERATION_CACHE_SCHEMA_VERSION

    def payload(self) -> dict[str, object]:
        return {
            "config_generation": self.config_generation,
            "declared_input_hash": self.declared_input_hash,
            "dependency_receipt_hashes": list(self.dependency_receipt_hashes),
            "environment_identity": self.environment_identity,
            "lockfile_hash": self.lockfile_hash,
            "network_policy": self.network_policy,
            "policy_hash": self.policy_hash,
            "provider_hash": self.provider_hash,
            "schema_version": self.schema_version,
            "stage_definition_hash": self.stage_definition_hash,
            "target_identity": self.target_identity,
            "toolchain_hash": self.toolchain_hash,
            "working_directory": self.working_directory,
            "workspace_identity": self.workspace_identity,
        }


@dataclass(frozen=True, slots=True)
class IterationCacheEntry:
    entry_id: str
    key: IterationCacheKey
    source_receipt_id: str
    artifact_digests: tuple[ArtifactDigest, ...]
    created_at: str
    schema_version: int = ITERATION_CACHE_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class CacheLookup:
    hit: bool
    reason: CacheMissReason | None
    entry: IterationCacheEntry | None


def _invalid(message: str) -> NoReturn:
    raise RepoForgeError(
        message,
        code=ErrorCode.STATE_INVALID,
        safe_next_action="Recompile the verification DAG or discard the incompatible cache entry.",
    )


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _validate_sha(value: str, field: str) -> None:
    if _SHA64.fullmatch(value) is None:
        _invalid(f"Iteration cache {field} is invalid")


def _topological(stages: tuple[VerificationDagStage, ...]) -> tuple[VerificationDagStage, ...]:
    if not stages or len(stages) > 128:
        _invalid("Verification DAG must contain between 1 and 128 stages")
    by_id = {stage.stage_id: stage for stage in stages}
    if len(by_id) != len(stages):
        _invalid("Verification DAG stage ids must be unique")
    known = set(by_id)
    for stage in stages:
        unknown = sorted(set(stage.dependencies) - known)
        if unknown:
            _invalid(
                f"Verification DAG stage {stage.stage_id!r} has unknown dependencies: {unknown}"
            )

    indegree = {stage_id: 0 for stage_id in by_id}
    children: dict[str, list[str]] = {stage_id: [] for stage_id in by_id}
    for stage in stages:
        indegree[stage.stage_id] = len(stage.dependencies)
        for dependency in stage.dependencies:
            children[dependency].append(stage.stage_id)
    ready = sorted(stage_id for stage_id, count in indegree.items() if count == 0)
    ordered: list[VerificationDagStage] = []
    while ready:
        stage_id = ready.pop(0)
        ordered.append(by_id[stage_id])
        for child in sorted(children[stage_id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(ordered) != len(stages):
        _invalid("Verification DAG contains a dependency cycle")
    return tuple(ordered)


def create_verification_dag(
    *,
    plan_id: str,
    plan_hash: str,
    stages: tuple[VerificationDagStage, ...],
    final_stage_id: str,
) -> VerificationDag:
    if _PLAN_ID.fullmatch(plan_id) is None or _SHA64.fullmatch(plan_hash) is None:
        _invalid("Verification DAG plan identity is invalid")
    ordered = _topological(stages)
    by_id = {stage.stage_id: stage for stage in ordered}
    final = by_id.get(final_stage_id)
    if final is None or not final.final:
        _invalid("Verification DAG final stage is missing")
    finals = [stage for stage in ordered if stage.final]
    if len(finals) != 1 or finals[0].stage_id != final_stage_id:
        _invalid("Verification DAG requires exactly one declared final stage")
    if any(
        stage.stage_id not in final.dependencies and stage is not final for stage in ordered[:-1]
    ):
        # Every earlier stage need not be a direct dependency, but must be an ancestor.
        ancestors: set[str] = set()

        def collect(stage_id: str) -> None:
            for dependency in by_id[stage_id].dependencies:
                if dependency not in ancestors:
                    ancestors.add(dependency)
                    collect(dependency)

        collect(final_stage_id)
        if any(stage.stage_id not in ancestors for stage in ordered if stage is not final):
            _invalid("Verification DAG final stage must depend on every required execution branch")
    semantic = {
        "final_stage_id": final_stage_id,
        "plan_hash": plan_hash,
        "plan_id": plan_id,
        "schema_version": VERIFICATION_DAG_SCHEMA_VERSION,
        "stages": [stage.definition_payload() for stage in ordered],
    }
    dag_hash = _digest(semantic)
    return VerificationDag(
        dag_id=f"dag-{dag_hash[:24]}",
        dag_hash=dag_hash,
        plan_id=plan_id,
        plan_hash=plan_hash,
        stages=ordered,
        final_stage_id=final_stage_id,
    )


def compile_plan_dag(plan: ExecutionPlan) -> VerificationDag:
    validate_execution_plan(plan)
    stages = tuple(
        VerificationDagStage(
            stage_id=stage.stage_id,
            version=1,
            dependencies=stage.dependencies,
            kind=stage.kind.value,
            target=stage.target,
            selector=stage.selector,
            environment_adapter="native",
            working_directory=stage.working_directory or ".",
            timeout_seconds=stage.timeout_seconds,
            network_policy=stage.network_policy,
            filesystem_policy=stage.mutability.value,
            process_policy="bounded_subprocess",
            credential_policy="none",
            mutability=stage.mutability.value,
            required_risk_level="unknown",
            artifact_paths=stage.artifact_paths,
            cache_policy=(
                CachePolicy.READ_ONLY
                if stage.boundary is PlanStageBoundary.ITERATION
                and stage.mutability is PlanStageMutability.READ_ONLY
                else CachePolicy.DISABLED
            ),
            failure_severity=(
                DagFailureSeverity.REQUIRED
                if stage.failure_policy is StageFailurePolicy.REQUIRED
                else DagFailureSeverity.OPTIONAL
            ),
            final=stage.boundary is PlanStageBoundary.FINAL,
        )
        for stage in plan.ordered_stages
    )
    final_stage_id = next(
        stage.stage_id for stage in plan.ordered_stages if stage.boundary is PlanStageBoundary.FINAL
    )
    return create_verification_dag(
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        stages=stages,
        final_stage_id=final_stage_id,
    )


def build_iteration_cache_key(
    *,
    workspace_identity: str,
    declared_input_hash: str,
    stage_definition_hash: str,
    target_identity: str,
    working_directory: str,
    environment_identity: str,
    toolchain_hash: str,
    lockfile_hash: str,
    config_generation: str,
    policy_hash: str,
    provider_hash: str,
    network_policy: str,
    dependency_receipt_hashes: tuple[str, ...],
) -> IterationCacheKey:
    for field, value in (
        ("workspace identity", workspace_identity),
        ("declared input hash", declared_input_hash),
        ("stage definition hash", stage_definition_hash),
        ("target identity", target_identity),
        ("environment identity", environment_identity),
        ("toolchain hash", toolchain_hash),
        ("lockfile hash", lockfile_hash),
        ("configuration generation", config_generation),
        ("policy hash", policy_hash),
        ("provider hash", provider_hash),
    ):
        _validate_sha(value, field)
    if not working_directory or len(working_directory) > 512:
        _invalid("Iteration cache working directory is invalid")
    if _SAFE_ID.fullmatch(network_policy) is None:
        _invalid("Iteration cache network policy is invalid")
    if len(dependency_receipt_hashes) > 64 or any(
        _SHA64.fullmatch(value) is None for value in dependency_receipt_hashes
    ):
        _invalid("Iteration cache dependency receipt hashes are invalid")
    semantic = {
        "config_generation": config_generation,
        "declared_input_hash": declared_input_hash,
        "dependency_receipt_hashes": list(dependency_receipt_hashes),
        "environment_identity": environment_identity,
        "lockfile_hash": lockfile_hash,
        "network_policy": network_policy,
        "policy_hash": policy_hash,
        "provider_hash": provider_hash,
        "schema_version": ITERATION_CACHE_SCHEMA_VERSION,
        "stage_definition_hash": stage_definition_hash,
        "target_identity": target_identity,
        "toolchain_hash": toolchain_hash,
        "working_directory": working_directory,
        "workspace_identity": workspace_identity,
    }
    digest = _digest(semantic)
    return IterationCacheKey(cache_key=digest, **semantic)  # type: ignore[arg-type]


def create_iteration_cache_entry(
    *,
    key: IterationCacheKey,
    source_receipt_id: str,
    artifact_digests: tuple[ArtifactDigest, ...],
    created_at: str,
) -> IterationCacheEntry:
    if _RECEIPT_ID.fullmatch(source_receipt_id) is None:
        _invalid("Iteration cache source receipt id is invalid")
    if len(artifact_digests) > 64 or len({item.path for item in artifact_digests}) != len(
        artifact_digests
    ):
        _invalid("Iteration cache artifact digests must be unique and bounded")
    if not created_at or len(created_at) > 80:
        _invalid("Iteration cache creation timestamp is invalid")
    semantic = {
        "artifact_digests": [item.payload() for item in artifact_digests],
        "created_at": created_at,
        "key": key.payload(),
        "source_receipt_id": source_receipt_id,
    }
    entry_hash = _digest(semantic)
    return IterationCacheEntry(
        entry_id=f"cache-{entry_hash[:24]}",
        key=key,
        source_receipt_id=source_receipt_id,
        artifact_digests=artifact_digests,
        created_at=created_at,
    )


def cache_entry_payload(entry: IterationCacheEntry) -> dict[str, object]:
    return {
        "artifact_digests": [item.payload() for item in entry.artifact_digests],
        "created_at": entry.created_at,
        "entry_id": entry.entry_id,
        "key": {"cache_key": entry.key.cache_key, **entry.key.payload()},
        "schema_version": entry.schema_version,
        "source_receipt_id": entry.source_receipt_id,
    }


def iteration_cache_entry_from_payload(payload: dict[str, Any]) -> IterationCacheEntry:
    raw_key = payload.get("key")
    raw_artifacts = payload.get("artifact_digests")
    if not isinstance(raw_key, dict) or not isinstance(raw_artifacts, list):
        _invalid("Iteration cache entry payload is incomplete")
    dependencies = raw_key.get("dependency_receipt_hashes")
    if not isinstance(dependencies, list):
        _invalid("Iteration cache dependency hashes payload is invalid")
    key = build_iteration_cache_key(
        workspace_identity=str(raw_key.get("workspace_identity", "")),
        declared_input_hash=str(raw_key.get("declared_input_hash", "")),
        stage_definition_hash=str(raw_key.get("stage_definition_hash", "")),
        target_identity=str(raw_key.get("target_identity", "")),
        working_directory=str(raw_key.get("working_directory", "")),
        environment_identity=str(raw_key.get("environment_identity", "")),
        toolchain_hash=str(raw_key.get("toolchain_hash", "")),
        lockfile_hash=str(raw_key.get("lockfile_hash", "")),
        config_generation=str(raw_key.get("config_generation", "")),
        policy_hash=str(raw_key.get("policy_hash", "")),
        provider_hash=str(raw_key.get("provider_hash", "")),
        network_policy=str(raw_key.get("network_policy", "")),
        dependency_receipt_hashes=tuple(str(item) for item in dependencies),
    )
    if raw_key.get("cache_key") != key.cache_key:
        _invalid("Iteration cache key digest mismatch")
    artifacts: list[ArtifactDigest] = []
    for raw in raw_artifacts:
        if not isinstance(raw, dict):
            _invalid("Iteration cache artifact payload is invalid")
        artifacts.append(
            ArtifactDigest(path=str(raw.get("path", "")), sha256=str(raw.get("sha256", "")))
        )
    entry = create_iteration_cache_entry(
        key=key,
        source_receipt_id=str(payload.get("source_receipt_id", "")),
        artifact_digests=tuple(artifacts),
        created_at=str(payload.get("created_at", "")),
    )
    schema = payload.get("schema_version")
    if schema != ITERATION_CACHE_SCHEMA_VERSION:
        _invalid("Iteration cache schema version is unsupported")
    if payload.get("entry_id") != entry.entry_id or _CACHE_ID.fullmatch(entry.entry_id) is None:
        _invalid("Iteration cache entry id digest mismatch")
    return entry
