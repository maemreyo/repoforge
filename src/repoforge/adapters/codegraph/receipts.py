"""Canonical promotion identities and durable CodeGraph canary receipts."""

from __future__ import annotations

import hashlib
import json
import platform as host_platform
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ...domain.provider_manifest import ProviderExecutableIdentity, ProviderManifest
from ...ports.locking import LockManager
from ..filesystem.atomic import atomic_write_text

CODEGRAPH_ADAPTER_SCHEMA_VERSION = 1
_MAX_RECEIPT_BYTES = 64 * 1024
_HEX_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _HEX_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _name(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_NAME.fullmatch(value) is None:
        raise ValueError(f"{field_name} has an invalid format")
    return value


@dataclass(frozen=True, slots=True)
class PromotionIdentity:
    executable_digest: str
    provider_version: str
    platform: str
    architecture: str
    manifest_hash: str
    options_digest: str
    adapter_schema_version: int
    corpus_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "executable_digest", _digest(self.executable_digest, "executable_digest")
        )
        object.__setattr__(
            self, "provider_version", _name(self.provider_version, "provider_version")
        )
        object.__setattr__(self, "platform", _name(self.platform, "platform"))
        object.__setattr__(self, "architecture", _name(self.architecture, "architecture"))
        object.__setattr__(self, "manifest_hash", _digest(self.manifest_hash, "manifest_hash"))
        object.__setattr__(self, "options_digest", _digest(self.options_digest, "options_digest"))
        object.__setattr__(self, "corpus_digest", _digest(self.corpus_digest, "corpus_digest"))
        if (
            not isinstance(self.adapter_schema_version, int)
            or isinstance(self.adapter_schema_version, bool)
            or self.adapter_schema_version < 1
        ):
            raise ValueError("adapter_schema_version must be a positive integer")

    @property
    def digest(self) -> str:
        payload = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class PromotionGateOutcome:
    name: str
    passed: bool
    observed: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "gate name"))
        if not isinstance(self.passed, bool):
            raise ValueError("gate passed must be boolean")
        if (
            not isinstance(self.observed, int)
            or isinstance(self.observed, bool)
            or self.observed < 0
        ):
            raise ValueError("gate observed must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PromotionReceipt:
    identity: PromotionIdentity
    gates: tuple[PromotionGateOutcome, ...]
    metrics: tuple[tuple[str, int], ...]
    created_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, PromotionIdentity):
            raise ValueError("receipt identity must use PromotionIdentity")
        if not isinstance(self.gates, tuple) or any(
            not isinstance(gate, PromotionGateOutcome) for gate in self.gates
        ):
            raise ValueError("receipt gates must be an immutable typed tuple")
        if not self.gates or len(self.gates) > 32:
            raise ValueError("receipt must contain between 1 and 32 gates")
        normalized_gates = tuple(sorted(set(self.gates), key=lambda gate: gate.name))
        if len({gate.name for gate in normalized_gates}) != len(normalized_gates):
            raise ValueError("receipt gate names must be unique")
        normalized_metrics: list[tuple[str, int]] = []
        if not isinstance(self.metrics, tuple) or len(self.metrics) > 32:
            raise ValueError("receipt metrics must be a bounded immutable tuple")
        for raw in self.metrics:
            if not isinstance(raw, tuple) or len(raw) != 2:
                raise ValueError("receipt metric must be a name/value pair")
            name, value = raw
            if not isinstance(name, str):
                raise ValueError("receipt metric name must be text")
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("receipt metric value must be a non-negative integer")
            normalized_metrics.append((_name(name, "metric name"), value))
        if len({name for name, _ in normalized_metrics}) != len(normalized_metrics):
            raise ValueError("receipt metric names must be unique")
        if not isinstance(self.created_at, str) or not self.created_at or len(self.created_at) > 80:
            raise ValueError("receipt created_at must be bounded text")
        object.__setattr__(self, "gates", normalized_gates)
        object.__setattr__(self, "metrics", tuple(sorted(normalized_metrics)))

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _receipt_payload(receipt: PromotionReceipt) -> dict[str, object]:
    return {
        "schema_version": 1,
        "identity": asdict(receipt.identity),
        "identity_digest": receipt.identity.digest,
        "gates": [asdict(gate) for gate in receipt.gates],
        "metrics": [[name, value] for name, value in receipt.metrics],
        "created_at": receipt.created_at,
    }


def _parse_receipt(raw: object, expected: PromotionIdentity) -> PromotionReceipt:
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "identity",
        "identity_digest",
        "gates",
        "metrics",
        "created_at",
    }:
        raise ValueError("promotion receipt has an invalid schema")
    if raw["schema_version"] != 1 or raw["identity_digest"] != expected.digest:
        raise ValueError("promotion receipt identity does not match")
    identity_raw = raw["identity"]
    gates_raw = raw["gates"]
    metrics_raw = raw["metrics"]
    if (
        not isinstance(identity_raw, dict)
        or not isinstance(gates_raw, list)
        or not isinstance(metrics_raw, list)
    ):
        raise ValueError("promotion receipt contains invalid values")
    identity = PromotionIdentity(**identity_raw)
    if identity != expected:
        raise ValueError("promotion receipt identity changed")
    gates = tuple(PromotionGateOutcome(**gate) for gate in gates_raw if isinstance(gate, dict))
    if len(gates) != len(gates_raw):
        raise ValueError("promotion receipt gate is invalid")
    metrics: list[tuple[str, int]] = []
    for metric in metrics_raw:
        if not isinstance(metric, list) or len(metric) != 2:
            raise ValueError("promotion receipt metric is invalid")
        name, value = metric
        if not isinstance(name, str) or not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("promotion receipt metric is invalid")
        metrics.append((name, value))
    created_at = raw["created_at"]
    if not isinstance(created_at, str):
        raise ValueError("promotion receipt timestamp is invalid")
    receipt = PromotionReceipt(identity, gates, tuple(metrics), created_at)
    if not receipt.passed:
        raise ValueError("promotion receipt is not successful")
    return receipt


class PromotionReceiptStore:
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._root = state_root.expanduser().resolve() / "providers" / "codegraph" / "promotion"
        if self._root.is_symlink():
            raise ValueError("CodeGraph promotion directory must not be a symlink")
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._root.is_symlink() or not self._root.is_dir():
            raise ValueError("CodeGraph promotion directory must be a managed directory")
        self._locks = locks

    def _path(self, identity: PromotionIdentity) -> Path:
        return self._root / f"{identity.digest}.json"

    def load(self, identity: PromotionIdentity) -> PromotionReceipt | None:
        path = self._path(identity)
        lock_name = f"codegraph-promotion-{identity.digest}"
        with self._locks.lock(lock_name, timeout_seconds=30):
            try:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size > _MAX_RECEIPT_BYTES
                ):
                    return None
                text = path.read_text(encoding="utf-8")
                raw = json.loads(text, object_pairs_hook=_pairs)
                return _parse_receipt(raw, identity)
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                return None

    def save(self, receipt: PromotionReceipt) -> None:
        if not receipt.passed:
            raise ValueError("only successful promotion receipts may be persisted")
        path = self._path(receipt.identity)
        payload = (
            json.dumps(
                _receipt_payload(receipt),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        )
        if len(payload.encode("utf-8")) > _MAX_RECEIPT_BYTES:
            raise ValueError("promotion receipt exceeds its reviewed bound")
        lock_name = f"codegraph-promotion-{receipt.identity.digest}"
        with self._locks.lock(lock_name, timeout_seconds=30):
            if path.is_symlink():
                path.unlink()
            atomic_write_text(path, payload)


def promotion_identity(
    manifest: ProviderManifest,
    corpus_digest: str,
    *,
    platform_name: str | None = None,
    architecture: str | None = None,
) -> PromotionIdentity:
    if manifest.codegraph is None or not isinstance(manifest.runtime, ProviderExecutableIdentity):
        raise ValueError("promotion identity requires a reviewed executable CodeGraph enrollment")
    return PromotionIdentity(
        executable_digest=manifest.runtime.sha256,
        provider_version=manifest.version,
        platform=(platform_name or host_platform.system()).lower(),
        architecture=(architecture or host_platform.machine()).lower(),
        manifest_hash=manifest.manifest_hash,
        options_digest=manifest.codegraph.options_digest,
        adapter_schema_version=CODEGRAPH_ADAPTER_SCHEMA_VERSION,
        corpus_digest=corpus_digest,
    )


__all__ = [
    "CODEGRAPH_ADAPTER_SCHEMA_VERSION",
    "PromotionGateOutcome",
    "PromotionIdentity",
    "PromotionReceipt",
    "PromotionReceiptStore",
    "promotion_identity",
]
