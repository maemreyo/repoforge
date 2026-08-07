from __future__ import annotations

import json
from pathlib import Path

from codegraph_provider_support import manifest

from repoforge.adapters.codegraph.canary_corpus import embedded_canary_digest
from repoforge.adapters.codegraph.operator import (
    codegraph_doctor_checks,
    codegraph_operator_report,
)
from repoforge.adapters.codegraph.receipts import (
    PromotionGateOutcome,
    PromotionReceipt,
    PromotionReceiptStore,
    promotion_identity,
)
from repoforge.adapters.locking import FcntlLockManager
from repoforge.config import AppConfig, RepositoryConfig, ServerConfig
from repoforge.domain.provider_manifest import (
    ProviderAvailability,
    ProviderAvailabilityStatus,
    ProviderKind,
    ProviderManifest,
)

ROOT = Path(__file__).resolve().parents[1]
_SECRET_PATH = "/private/provider-secret/codegraph"
_SECRET_DETAIL = "provider-private-detail"


class Registry:
    def __init__(self, provider: ProviderManifest, status: ProviderAvailabilityStatus) -> None:
        self.provider = provider
        self.status = status

    def list_providers(self) -> tuple[ProviderManifest, ...]:
        return (self.provider,)

    def get_provider(self, provider_id: str) -> ProviderManifest | None:
        return self.provider if provider_id == self.provider.provider_id else None

    def get_providers_by_kind(self, kind: ProviderKind) -> tuple[ProviderManifest, ...]:
        return (self.provider,) if kind is self.provider.kind else ()

    def check_availability(self, provider_id: str) -> ProviderAvailability:
        return ProviderAvailability(
            provider_id,
            self.status,
            _SECRET_DETAIL,
            _SECRET_PATH,
        )


def _config(tmp_path: Path, *, enabled: bool = True) -> AppConfig:
    provider = manifest()
    repository = RepositoryConfig(
        repo_id="demo",
        path=tmp_path / "repo",
        code_intelligence_provider_id=provider.provider_id if enabled else "",
    )
    return AppConfig(
        source_path=tmp_path / "resolved.toml",
        server=ServerConfig(
            workspace_root=tmp_path / "workspaces",
            state_root=tmp_path / "state",
        ),
        repositories={"demo": repository},
        providers=(provider,),
    )


def _save_receipt(config: AppConfig) -> None:
    provider = config.providers[0]
    identity = promotion_identity(provider, embedded_canary_digest())
    store = PromotionReceiptStore(
        config.server.state_root,
        FcntlLockManager(config.server.state_root / "locks"),
    )
    store.save(
        PromotionReceipt(
            identity,
            (PromotionGateOutcome("required_edges", True, 2),),
            (("relationship_count", 2),),
            "2026-08-06T00:00:00+00:00",
        )
    )


def test_operator_report_is_secret_free_and_digest_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _save_receipt(config)
    report = codegraph_operator_report(
        config,
        Registry(config.providers[0], ProviderAvailabilityStatus.AVAILABLE),
    )
    encoded = json.dumps(report, sort_keys=True)
    provider = report["providers"][0]

    assert report["enabled"] is True
    assert provider["provider_id"] == "codegraph"
    assert provider["provider_version"] == "1.5.0"
    assert provider["manifest_digest"] == config.providers[0].manifest_hash
    assert provider["options_digest"] == config.providers[0].codegraph.options_digest  # type: ignore[union-attr]
    assert provider["executable_digest"] == "c" * 64
    assert provider["executable_status"] == "available"
    assert provider["promotion_receipt_valid"] is True
    assert _SECRET_PATH not in encoded
    assert _SECRET_DETAIL not in encoded
    assert "PATH" not in encoded


def test_disabled_report_preserves_exact_baseline_mode(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=False)

    report = codegraph_operator_report(
        config,
        Registry(config.providers[0], ProviderAvailabilityStatus.AVAILABLE),
    )

    assert report["enabled"] is False
    assert report["baseline_when_disabled"] is True
    assert report["providers"] == []
    assert report["repositories"] == [{"repo_id": "demo", "code_intelligence_provider_id": ""}]


def test_doctor_reports_executable_version_and_receipt_without_private_output(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    checks = codegraph_doctor_checks(
        config,
        Registry(config.providers[0], ProviderAvailabilityStatus.UNAVAILABLE),
    )
    encoded = json.dumps([check.as_dict() for check in checks], sort_keys=True)
    by_name = {check.name: check for check in checks}

    assert by_name["codegraph_executable:demo"].ok is False
    assert by_name["codegraph_version:demo"].ok is True
    assert by_name["codegraph_promotion_receipt:demo"].ok is False
    assert _SECRET_PATH not in encoded
    assert _SECRET_DETAIL not in encoded


def test_config_example_documents_disabled_default_and_reviewed_nested_options() -> None:
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")

    assert 'code_intelligence_provider_id = ""' in text
    assert "[[providers]]" in text
    assert "[providers.codegraph]" in text
    assert "canary_timeout_seconds" in text
    assert "CODEGRAPH_NO_DAEMON=1" in text
    assert "CODEGRAPH_NO_DOWNLOAD=1" in text
