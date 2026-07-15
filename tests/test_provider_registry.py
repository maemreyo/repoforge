from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from repoforge.adapters.provider.config_registry import ConfigProviderRegistry
from repoforge.domain.provider_manifest import (
    ProviderAvailabilityStatus,
    ProviderExecutableIdentity,
    ProviderImageIdentity,
    ProviderKind,
    ProviderManifest,
)


class StaticExecutableLocator:
    def __init__(self, paths: dict[str, str]) -> None:
        self.paths: dict[str, str] = paths

    def which(self, executable: str, *, path: str | None = None) -> str | None:
        del path
        return self.paths.get(executable)


def _manifest(
    provider_id: str,
    *,
    version: str = "1.0.0",
    kind: ProviderKind = ProviderKind.ANALYZER,
    capabilities: tuple[str, ...] = ("lint",),
    languages: tuple[str, ...] = ("python",),
    fallback: str = "",
) -> ProviderManifest:
    return ProviderManifest(
        provider_id=provider_id,
        kind=kind,
        version=version,
        runtime=ProviderExecutableIdentity("provider-bin", "a" * 64),
        supported_capabilities=capabilities,
        supported_languages=languages,
        fallback_provider_id=fallback,
    )


def test_registry_lists_providers_deterministically() -> None:
    registry = ConfigProviderRegistry(
        (_manifest("z-provider"), _manifest("a-provider")),
        StaticExecutableLocator({}),
    )

    assert tuple(item.provider_id for item in registry.list_providers()) == (
        "a-provider",
        "z-provider",
    )


def test_registry_rejects_duplicate_provider_ids() -> None:
    provider = _manifest("duplicate")

    with pytest.raises(ValueError, match="duplicate provider_id"):
        _ = ConfigProviderRegistry((provider, provider), StaticExecutableLocator({}))


def test_registry_rejects_missing_fallback() -> None:
    with pytest.raises(ValueError, match="unknown fallback"):
        _ = ConfigProviderRegistry(
            (_manifest("primary", fallback="missing"),),
            StaticExecutableLocator({}),
        )


@pytest.mark.parametrize(
    ("fallback", "message"),
    [
        (_manifest("fallback", version="2.0.0"), "major version"),
        (_manifest("fallback", capabilities=()), "capabilities"),
        (_manifest("fallback", languages=()), "languages"),
        (_manifest("fallback", kind=ProviderKind.POLICY), "kind"),
    ],
)
def test_registry_rejects_incompatible_fallbacks(
    fallback: ProviderManifest,
    message: str,
) -> None:
    primary = _manifest("primary", fallback="fallback")

    with pytest.raises(ValueError, match=message):
        _ = ConfigProviderRegistry((primary, fallback), StaticExecutableLocator({}))


def test_registry_rejects_fallback_cycles() -> None:
    first = _manifest("first", fallback="second")
    second = _manifest("second", fallback="first")

    with pytest.raises(ValueError, match="cycle"):
        _ = ConfigProviderRegistry((first, second), StaticExecutableLocator({}))


def test_registry_reports_verified_executable_available(tmp_path: Path) -> None:
    executable = tmp_path / "provider-bin"
    _ = executable.write_bytes(b"provider")
    digest = hashlib.sha256(b"provider").hexdigest()
    provider = ProviderManifest(
        provider_id="provider",
        kind=ProviderKind.ANALYZER,
        version="1.0.0",
        runtime=ProviderExecutableIdentity("provider-bin", digest),
    )
    registry = ConfigProviderRegistry(
        (provider,),
        StaticExecutableLocator({"provider-bin": str(executable)}),
    )

    availability = registry.check_availability("provider")

    assert availability.status is ProviderAvailabilityStatus.AVAILABLE
    assert availability.resolved_executable == str(executable.resolve())


def test_registry_reports_missing_executable_without_exposing_configured_value() -> None:
    secret_value = "provider-token=do-not-expose"
    provider = ProviderManifest(
        provider_id="provider",
        kind=ProviderKind.ANALYZER,
        version="1.0.0",
        runtime=ProviderExecutableIdentity(secret_value, "a" * 64),
    )
    registry = ConfigProviderRegistry((provider,), StaticExecutableLocator({}))

    availability = registry.check_availability("provider")

    assert availability.status is ProviderAvailabilityStatus.UNAVAILABLE
    assert secret_value not in availability.message
    assert "do-not-expose" not in availability.message


def test_registry_reports_digest_mismatch_without_running_provider(tmp_path: Path) -> None:
    executable = tmp_path / "provider-bin"
    _ = executable.write_bytes(b"unexpected")
    registry = ConfigProviderRegistry(
        (_manifest("provider"),),
        StaticExecutableLocator({"provider-bin": str(executable)}),
    )

    availability = registry.check_availability("provider")

    assert availability.status is ProviderAvailabilityStatus.UNAVAILABLE
    assert availability.message == "Configured executable digest does not match"


def test_registry_rejects_oversized_executable_without_reading_it(tmp_path: Path) -> None:
    executable = tmp_path / "provider-bin"
    with executable.open("wb") as handle:
        _ = handle.truncate(1_000_000_001)
    registry = ConfigProviderRegistry(
        (_manifest("provider"),),
        StaticExecutableLocator({"provider-bin": str(executable)}),
    )

    availability = registry.check_availability("provider")

    assert availability.status is ProviderAvailabilityStatus.UNAVAILABLE
    assert availability.message == "Configured executable cannot be read"


def test_registry_rejects_non_regular_executable_without_blocking(tmp_path: Path) -> None:
    executable = tmp_path / "provider-bin"
    os.mkfifo(executable)
    registry = ConfigProviderRegistry(
        (_manifest("provider"),),
        StaticExecutableLocator({"provider-bin": str(executable)}),
    )

    availability = registry.check_availability("provider")

    assert availability.status is ProviderAvailabilityStatus.UNAVAILABLE
    assert availability.message == "Configured executable cannot be read"


def test_registry_reports_image_identity_as_unverified() -> None:
    provider = ProviderManifest(
        provider_id="image-provider",
        kind=ProviderKind.ANALYZER,
        version="1.0.0",
        runtime=ProviderImageIdentity("registry.invalid/provider", "b" * 64),
    )
    registry = ConfigProviderRegistry((provider,), StaticExecutableLocator({}))

    availability = registry.check_availability("image-provider")

    assert availability.status is ProviderAvailabilityStatus.UNVERIFIED
