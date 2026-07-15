"""Provider-neutral, connection-scoped MCP client capability model."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

CLIENT_CAPABILITY_SCHEMA_VERSION = 1
_MAX_ID = 128
_MAX_VERSION = 64
_MAX_COMPATIBILITY_FLAGS = 32


class ClientFeature(str, Enum):
    """Optional protocol features that RepoForge may negotiate per connection."""

    APPS_UI = "apps_ui"
    ELICITATION_FORM = "elicitation_form"
    ELICITATION_URL = "elicitation_url"
    TASKS = "tasks"
    PROGRESS_NOTIFICATIONS = "progress_notifications"
    CANCELLATION_NOTIFICATIONS = "cancellation_notifications"
    TOOL_SEARCH = "tool_search"
    DEFERRED_DISCOVERY = "deferred_discovery"
    RESOURCE_SUBSCRIPTIONS = "resource_subscriptions"


@dataclass(frozen=True, slots=True)
class FeatureSupport:
    supported: bool
    version: str | None = None
    reason: str = "not_declared"

    def as_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "version": self.version,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ClientCapabilities:
    """Normalized capability snapshot captured from one MCP initialization."""

    protocol_version: str
    client_name: str
    client_version: str
    features: tuple[tuple[ClientFeature, FeatureSupport], ...]
    compatibility_flags: tuple[str, ...] = ()
    malformed_fields: tuple[str, ...] = ()
    legacy: bool = False
    schema_version: int = CLIENT_CAPABILITY_SCHEMA_VERSION

    def feature(self, feature: ClientFeature) -> FeatureSupport:
        for name, support in self.features:
            if name is feature:
                return support
        return FeatureSupport(False)

    def supports(self, feature: ClientFeature) -> bool:
        return self.feature(feature).supported

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "protocol_version": self.protocol_version,
            "client_name": self.client_name,
            "client_version": self.client_version,
            "features": {
                feature.value: self.feature(feature).as_dict() for feature in ClientFeature
            },
            "compatibility_flags": list(self.compatibility_flags),
            "malformed_fields": list(self.malformed_fields),
            "legacy": self.legacy,
        }


def _model_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(dumped, Mapping):
            return dumped
    return None


def _safe_text(value: object, *, default: str, limit: int) -> str:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        rendered = str(value).strip()
        if (
            rendered
            and len(rendered) <= limit
            and all(ord(character) >= 32 for character in rendered)
        ):
            return rendered
    return default


def _safe_version(value: object) -> str | None:
    if value is None:
        return None
    normalized = _safe_text(value, default="", limit=_MAX_VERSION)
    return normalized or None


def _declared_mapping(
    parent: Mapping[str, object],
    key: str,
    *,
    malformed: set[str],
    path: str,
) -> Mapping[str, object] | None:
    if key not in parent:
        return None
    value = _model_mapping(parent[key])
    if value is None:
        malformed.add(path)
        return None
    return value


def _extension(
    experimental: Mapping[str, object],
    aliases: tuple[str, ...],
    *,
    malformed: set[str],
) -> Mapping[str, object] | None:
    for alias in aliases:
        if alias not in experimental:
            continue
        value = _model_mapping(experimental[alias])
        if value is None:
            malformed.add(f"capabilities.experimental.{alias}")
            return None
        return value
    return None


def _supported(version: str | None = None, *, reason: str = "declared") -> FeatureSupport:
    return FeatureSupport(True, version, reason)


def _unsupported(reason: str = "not_declared") -> FeatureSupport:
    return FeatureSupport(False, None, reason)


def parse_client_capabilities(initialization: object | None) -> ClientCapabilities:
    """Normalize MCP initialize parameters without probing optional features.

    Missing, malformed, and legacy inputs fail closed. Unknown capability payloads are
    ignored rather than interpreted as authority.
    """

    malformed: set[str] = set()
    raw = _model_mapping(initialization)
    if raw is None:
        return ClientCapabilities(
            protocol_version="legacy",
            client_name="unknown",
            client_version="unknown",
            features=tuple(
                (feature, _unsupported("legacy_or_missing")) for feature in ClientFeature
            ),
            legacy=True,
        )

    protocol_version = _safe_text(
        raw.get("protocolVersion"),
        default="legacy",
        limit=_MAX_VERSION,
    )
    legacy = protocol_version == "legacy"

    client_info = _model_mapping(raw.get("clientInfo"))
    if client_info is None:
        if "clientInfo" in raw:
            malformed.add("clientInfo")
        client_info = {}
    client_name = _safe_text(client_info.get("name"), default="unknown", limit=_MAX_ID)
    client_version = _safe_text(
        client_info.get("version"),
        default="unknown",
        limit=_MAX_VERSION,
    )

    capabilities = _model_mapping(raw.get("capabilities"))
    if capabilities is None:
        if "capabilities" in raw:
            malformed.add("capabilities")
        capabilities = {}

    feature_support = {feature: _unsupported() for feature in ClientFeature}

    elicitation = _declared_mapping(
        capabilities,
        "elicitation",
        malformed=malformed,
        path="capabilities.elicitation",
    )
    if elicitation is not None:
        form = _declared_mapping(
            elicitation,
            "form",
            malformed=malformed,
            path="capabilities.elicitation.form",
        )
        url = _declared_mapping(
            elicitation,
            "url",
            malformed=malformed,
            path="capabilities.elicitation.url",
        )
        if form is not None:
            feature_support[ClientFeature.ELICITATION_FORM] = _supported(
                _safe_version(form.get("version"))
            )
        if url is not None:
            feature_support[ClientFeature.ELICITATION_URL] = _supported(
                _safe_version(url.get("version"))
            )

    tasks = _declared_mapping(
        capabilities,
        "tasks",
        malformed=malformed,
        path="capabilities.tasks",
    )
    if tasks is not None:
        feature_support[ClientFeature.TASKS] = _supported(_safe_version(tasks.get("version")))
        if "cancel" in tasks:
            cancel = _model_mapping(tasks.get("cancel"))
            if cancel is None:
                malformed.add("capabilities.tasks.cancel")
            else:
                feature_support[ClientFeature.CANCELLATION_NOTIFICATIONS] = _supported(
                    _safe_version(cancel.get("version")),
                    reason="tasks_cancel_declared",
                )

    experimental = _declared_mapping(
        capabilities,
        "experimental",
        malformed=malformed,
        path="capabilities.experimental",
    )
    experimental = experimental or {}

    apps = _extension(
        experimental,
        ("io.modelcontextprotocol/ui", "mcp-apps", "apps"),
        malformed=malformed,
    )
    if apps is not None:
        feature_support[ClientFeature.APPS_UI] = _supported(_safe_version(apps.get("version")))

    tool_search = _extension(
        experimental,
        ("io.modelcontextprotocol/tool-search", "toolSearch", "tool-search"),
        malformed=malformed,
    )
    if tool_search is not None:
        feature_support[ClientFeature.TOOL_SEARCH] = _supported(
            _safe_version(tool_search.get("version"))
        )
        if tool_search.get("deferredDiscovery") is True:
            feature_support[ClientFeature.DEFERRED_DISCOVERY] = _supported(
                _safe_version(tool_search.get("version")),
                reason="tool_search_deferred_declared",
            )

    repoforge_extension = _extension(
        experimental,
        ("repoforge", "io.repoforge/client"),
        malformed=malformed,
    )
    compatibility_flags: tuple[str, ...] = ()
    if repoforge_extension is not None:
        extension_flags = {
            ClientFeature.PROGRESS_NOTIFICATIONS: "progressNotifications",
            ClientFeature.CANCELLATION_NOTIFICATIONS: "cancellationNotifications",
            ClientFeature.RESOURCE_SUBSCRIPTIONS: "resourceSubscriptions",
            ClientFeature.DEFERRED_DISCOVERY: "deferredDiscovery",
        }
        extension_version = _safe_version(repoforge_extension.get("version"))
        for feature, key in extension_flags.items():
            if repoforge_extension.get(key) is True:
                feature_support[feature] = _supported(
                    extension_version,
                    reason="repoforge_extension_declared",
                )
        raw_flags = repoforge_extension.get("compatibilityFlags")
        if raw_flags is not None:
            if not isinstance(raw_flags, list):
                malformed.add("capabilities.experimental.repoforge.compatibilityFlags")
            else:
                normalized_flags = {
                    _safe_text(flag, default="", limit=_MAX_ID)
                    for flag in raw_flags[:_MAX_COMPATIBILITY_FLAGS]
                }
                normalized_flags.discard("")
                compatibility_flags = tuple(sorted(normalized_flags))

    features = tuple((feature, feature_support[feature]) for feature in ClientFeature)
    return ClientCapabilities(
        protocol_version=protocol_version,
        client_name=client_name,
        client_version=client_version,
        features=features,
        compatibility_flags=compatibility_flags,
        malformed_fields=tuple(sorted(malformed)),
        legacy=legacy,
    )
