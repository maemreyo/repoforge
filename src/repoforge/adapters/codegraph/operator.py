"""Secret-free operator views for reviewed CodeGraph enrollment."""

from __future__ import annotations

from dataclasses import dataclass

from ...config import AppConfig
from ...domain.provider_manifest import (
    ProviderAvailabilityStatus,
    ProviderExecutableIdentity,
    ProviderManifest,
)
from ...ports.provider_registry import ProviderRegistry
from .canary_corpus import embedded_canary_digest
from .receipts import promotion_identity, promotion_receipt_valid


@dataclass(frozen=True, slots=True)
class CodeGraphDoctorCheck:
    name: str
    ok: bool
    severity: str
    detail: str
    remediation: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "ok": self.ok,
            "severity": self.severity,
            "detail": self.detail,
        }
        if self.remediation is not None:
            payload["remediation"] = self.remediation
        return payload


@dataclass(frozen=True, slots=True)
class _ProviderStatus:
    provider_id: str
    provider_version: str
    manifest_digest: str
    options_digest: str
    executable_digest: str
    executable_status: str
    promotion_identity_digest: str
    promotion_receipt_valid: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "manifest_digest": self.manifest_digest,
            "options_digest": self.options_digest,
            "executable_digest": self.executable_digest,
            "executable_status": self.executable_status,
            "promotion_identity_digest": self.promotion_identity_digest,
            "promotion_receipt_valid": self.promotion_receipt_valid,
        }


def _unavailable_status(provider_id: str) -> _ProviderStatus:
    return _ProviderStatus(
        provider_id=provider_id,
        provider_version="",
        manifest_digest="",
        options_digest="",
        executable_digest="",
        executable_status=ProviderAvailabilityStatus.UNAVAILABLE.value,
        promotion_identity_digest="",
        promotion_receipt_valid=False,
    )


def _provider_status(
    config: AppConfig,
    registry: ProviderRegistry,
    provider_id: str,
) -> _ProviderStatus:
    manifest = registry.get_provider(provider_id)
    if (
        manifest is None
        or manifest.codegraph is None
        or not isinstance(manifest.runtime, ProviderExecutableIdentity)
    ):
        return _unavailable_status(provider_id)
    try:
        identity = promotion_identity(manifest, embedded_canary_digest())
    except ValueError:
        return _unavailable_status(provider_id)
    availability = registry.check_availability(provider_id)
    return _ProviderStatus(
        provider_id=provider_id,
        provider_version=manifest.version,
        manifest_digest=manifest.manifest_hash,
        options_digest=manifest.codegraph.options_digest,
        executable_digest=manifest.runtime.sha256,
        executable_status=availability.status.value,
        promotion_identity_digest=identity.digest,
        promotion_receipt_valid=promotion_receipt_valid(
            config.server.state_root,
            identity,
        ),
    )


def _provider_statuses(
    config: AppConfig,
    registry: ProviderRegistry,
) -> tuple[_ProviderStatus, ...]:
    provider_ids = sorted(
        {
            repo.code_intelligence_provider_id
            for repo in config.repositories.values()
            if repo.code_intelligence_provider_id
        }
    )
    return tuple(_provider_status(config, registry, provider_id) for provider_id in provider_ids)


def codegraph_operator_report(
    config: AppConfig,
    registry: ProviderRegistry,
) -> dict[str, object]:
    repositories = [
        {
            "repo_id": repo.repo_id,
            "code_intelligence_provider_id": repo.code_intelligence_provider_id,
        }
        for repo in sorted(config.repositories.values(), key=lambda item: item.repo_id)
    ]
    statuses = _provider_statuses(config, registry)
    return {
        "enabled": bool(statuses),
        "baseline_when_disabled": True,
        "providers": [status.as_dict() for status in statuses],
        "repositories": repositories,
        "managed_state_layout": "providers/codegraph/{promotion,canary-corpus,workspaces}",
        "guarantees": {
            "network_policy": "none",
            "daemon": False,
            "self_download": False,
            "mcp_tool_added": False,
        },
    }


def _manifest_by_id(config: AppConfig) -> dict[str, ProviderManifest]:
    return {provider.provider_id: provider for provider in config.providers}


def codegraph_doctor_checks(
    config: AppConfig,
    registry: ProviderRegistry,
) -> tuple[CodeGraphDoctorCheck, ...]:
    statuses = {status.provider_id: status for status in _provider_statuses(config, registry)}
    manifests = _manifest_by_id(config)
    checks: list[CodeGraphDoctorCheck] = []
    for repo in sorted(config.repositories.values(), key=lambda item: item.repo_id):
        provider_id = repo.code_intelligence_provider_id
        if not provider_id:
            checks.append(
                CodeGraphDoctorCheck(
                    f"codegraph_disabled:{repo.repo_id}",
                    True,
                    "info",
                    "CodeGraph is disabled; exact baseline provider construction is active.",
                )
            )
            continue
        status = statuses.get(provider_id, _unavailable_status(provider_id))
        configured = manifests.get(provider_id)
        version_ok = (
            configured is not None
            and status.provider_version == configured.version
            and status.manifest_digest == configured.manifest_hash
        )
        version = status.provider_version or "unavailable"
        executable_ok = status.executable_status == ProviderAvailabilityStatus.AVAILABLE.value
        checks.extend(
            (
                CodeGraphDoctorCheck(
                    f"codegraph_executable:{repo.repo_id}",
                    executable_ok,
                    "error" if not executable_ok else "info",
                    f"provider={provider_id}; executable_identity={status.executable_status}",
                    None
                    if executable_ok
                    else "Provision the pinned executable digest; RepoForge will not download it.",
                ),
                CodeGraphDoctorCheck(
                    f"codegraph_version:{repo.repo_id}",
                    version_ok,
                    "error" if not version_ok else "info",
                    f"provider={provider_id}; reviewed_version={version}",
                ),
                CodeGraphDoctorCheck(
                    f"codegraph_promotion_receipt:{repo.repo_id}",
                    status.promotion_receipt_valid,
                    "warning",
                    (
                        f"provider={provider_id}; receipt_identity="
                        f"{status.promotion_identity_digest}; valid="
                        f"{status.promotion_receipt_valid}"
                    ),
                    "Run the bounded semantic canary through normal provider use; never bypass promotion.",
                ),
            )
        )
    return tuple(checks)


__all__ = [
    "CodeGraphDoctorCheck",
    "codegraph_doctor_checks",
    "codegraph_operator_report",
]
