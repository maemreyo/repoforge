"""Dedicated boundary for reviewed hygiene inspection and remediation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.execution_environment import ExecutionEvidence
from ..domain.hygiene import FormatterPolicy, HygieneFinding


@dataclass(frozen=True, slots=True)
class HygieneInspection:
    findings: tuple[HygieneFinding, ...]
    environment_identity: str
    excerpt: str
    output_truncated: bool
    execution_evidence: ExecutionEvidence | None = None


@dataclass(frozen=True, slots=True)
class HygieneFormatReceipt:
    environment_identity: str
    excerpt: str
    output_truncated: bool
    execution_evidence: ExecutionEvidence | None = None


class HygieneGateway(Protocol):
    def inspect_workspace(
        self,
        workspace: Path,
        policy: FormatterPolicy,
        paths: tuple[str, ...],
    ) -> HygieneInspection: ...

    def inspect_base(
        self,
        repository: Path,
        commit_sha: str,
        policy: FormatterPolicy,
        paths: tuple[str, ...],
        *,
        max_archive_bytes: int,
    ) -> HygieneInspection: ...

    def format_paths(
        self,
        workspace: Path,
        policy: FormatterPolicy,
        paths: tuple[str, ...],
    ) -> HygieneFormatReceipt: ...


@dataclass(frozen=True, slots=True)
class HygieneCacheKey:
    repo_id: str
    base_sha: str
    config_identity: str
    environment_identity: str
    formatter_contract_hash: str
    ttl_seconds: int


class HygieneBaselineCache(Protocol):
    def get(
        self,
        key: HygieneCacheKey,
        *,
        now_epoch: float,
    ) -> tuple[HygieneFinding, ...] | None: ...

    def put(
        self,
        key: HygieneCacheKey,
        findings: tuple[HygieneFinding, ...],
        *,
        now_epoch: float,
    ) -> None: ...
