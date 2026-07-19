"""Versioned confidence calibration derived from the committed seeded-bug corpus."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from ...domain.code_intelligence import CodeLanguage

_CALIBRATION_RESOURCE = "calibration-v1.json"


def _document() -> dict[str, Any]:
    payload = json.loads(
        files("repoforge.adapters.code_intelligence")
        .joinpath(_CALIBRATION_RESOURCE)
        .read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Unsupported code-intelligence calibration document")
    providers = payload.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("Code-intelligence calibration providers are missing")
    return payload


def calibrated_confidence(provider_id: str, languages: frozenset[CodeLanguage]) -> tuple[int, str]:
    """Return corpus-derived routed-test recall for one provider/language set."""

    document = _document()
    providers = document["providers"]
    assert isinstance(providers, dict)
    provider = providers.get(provider_id)
    if not isinstance(provider, dict):
        return 0, f"No {_CALIBRATION_RESOURCE} entry exists for provider {provider_id}."
    values: list[int] = []
    for language in sorted(languages, key=lambda item: item.value):
        entry = provider.get(language.value)
        if not isinstance(entry, dict):
            continue
        recall = entry.get("routed_test_recall")
        if isinstance(recall, int) and not isinstance(recall, bool) and 0 <= recall <= 100:
            values.append(recall)
    value = min(values) if values else 0
    corpus = document.get("corpus", "unknown")
    language_names = ", ".join(sorted(language.value for language in languages)) or "none"
    return (
        value,
        f"Confidence uses {corpus} calibration for {provider_id} across {language_names}.",
    )


__all__ = ["calibrated_confidence"]
