"""Durable compatibility-bound failed-selector chains for targeted reruns."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .retry_guidance import FailureReuseBinding

FAILED_SELECTOR_METADATA_KEY = "failed_selector_history_v1"
LEGACY_FAILED_SELECTOR_SCHEMA_VERSION = 1
FAILED_SELECTOR_SCHEMA_VERSION = 2
MAX_FAILED_SELECTORS = 100
MAX_TRACKED_TARGETS = 16
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_FAILURE_CHAIN_ID = re.compile(r"^failure-chain-[a-f0-9]{24}$")
_BINDING_FIELDS = {
    "fingerprint",
    "target_identity",
    "command_source_identity",
    "config_identity",
    "environment_identity",
}


@dataclass(frozen=True, slots=True)
class FailedSelectorRecord:
    fingerprint: str
    selectors: tuple[str, ...]
    chain_id: str
    attempts: int
    binding: FailureReuseBinding | None = None
    schema_version: int = FAILED_SELECTOR_SCHEMA_VERSION


def _history(metadata: dict[str, Any]) -> dict[str, Any]:
    history = metadata.get(FAILED_SELECTOR_METADATA_KEY)
    if not isinstance(history, dict):
        history = {}
        metadata[FAILED_SELECTOR_METADATA_KEY] = history
    return history


def _common_record(
    raw: dict[str, Any],
) -> tuple[str, tuple[str, ...], str, int] | None:
    fingerprint = raw.get("fingerprint")
    selectors = raw.get("selectors")
    chain_id = raw.get("chain_id")
    attempts = raw.get("attempts")
    if (
        not isinstance(fingerprint, str)
        or _SHA256.fullmatch(fingerprint) is None
        or not isinstance(selectors, list)
        or not 1 <= len(selectors) <= MAX_FAILED_SELECTORS
        or not all(
            isinstance(item, str)
            and 1 <= len(item) <= 512
            and not any(ord(character) < 32 for character in item)
            for item in selectors
        )
        or len(set(selectors)) != len(selectors)
        or not isinstance(chain_id, str)
        or _FAILURE_CHAIN_ID.fullmatch(chain_id) is None
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 1
    ):
        return None
    return fingerprint, tuple(selectors), chain_id, attempts


def _binding(raw: object, digest: object) -> FailureReuseBinding | None:
    if not isinstance(raw, dict) or set(raw) != _BINDING_FIELDS:
        return None
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        return None
    try:
        binding = FailureReuseBinding(
            fingerprint=raw["fingerprint"],
            target_identity=raw["target_identity"],
            command_source_identity=raw["command_source_identity"],
            config_identity=raw["config_identity"],
            environment_identity=raw["environment_identity"],
        )
    except (TypeError, ValueError):
        return None
    return binding if binding.digest == digest else None


def read_failed_selectors(metadata: dict[str, Any], *, target: str) -> FailedSelectorRecord | None:
    history = metadata.get(FAILED_SELECTOR_METADATA_KEY)
    if not isinstance(history, dict):
        return None
    raw = history.get(target)
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    common = _common_record(raw)
    if common is None:
        return None
    fingerprint, selectors, chain_id, attempts = common
    if version == LEGACY_FAILED_SELECTOR_SCHEMA_VERSION:
        if set(raw) != {"version", "fingerprint", "selectors", "chain_id", "attempts"}:
            return None
        return FailedSelectorRecord(
            fingerprint,
            selectors,
            chain_id,
            attempts,
            binding=None,
            schema_version=LEGACY_FAILED_SELECTOR_SCHEMA_VERSION,
        )
    if version != FAILED_SELECTOR_SCHEMA_VERSION or set(raw) != {
        "version",
        "fingerprint",
        "selectors",
        "chain_id",
        "attempts",
        "binding",
        "binding_digest",
    }:
        return None
    binding = _binding(raw.get("binding"), raw.get("binding_digest"))
    if binding is None or binding.fingerprint != fingerprint:
        return None
    return FailedSelectorRecord(
        fingerprint,
        selectors,
        chain_id,
        attempts,
        binding=binding,
    )


def record_failed_selectors(
    metadata: dict[str, Any],
    *,
    target: str,
    fingerprint: str,
    selectors: tuple[str, ...],
    binding: FailureReuseBinding,
    chain_id: str | None = None,
) -> FailedSelectorRecord | None:
    if not target or _SHA256.fullmatch(fingerprint) is None or binding.fingerprint != fingerprint:
        return None
    normalized = tuple(dict.fromkeys(selectors))
    if not 1 <= len(normalized) <= MAX_FAILED_SELECTORS or any(
        not isinstance(item, str)
        or not 1 <= len(item) <= 512
        or any(ord(character) < 32 for character in item)
        for item in normalized
    ):
        return None
    existing = read_failed_selectors(metadata, target=target)
    selected_chain = chain_id
    if selected_chain is None and existing is not None and existing.binding == binding:
        selected_chain = existing.chain_id
    if selected_chain is None:
        digest = hashlib.sha256(
            json.dumps(
                {
                    "target": target,
                    "binding": binding.as_dict(),
                    "selectors": normalized,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        selected_chain = f"failure-chain-{digest[:24]}"
    if _FAILURE_CHAIN_ID.fullmatch(selected_chain) is None:
        return None
    attempts = (
        existing.attempts + 1
        if existing is not None
        and existing.chain_id == selected_chain
        and existing.binding == binding
        else 1
    )
    record = FailedSelectorRecord(
        fingerprint,
        normalized,
        selected_chain,
        attempts,
        binding=binding,
    )
    history = _history(metadata)
    history[target] = {
        "version": FAILED_SELECTOR_SCHEMA_VERSION,
        "fingerprint": record.fingerprint,
        "selectors": list(record.selectors),
        "chain_id": record.chain_id,
        "attempts": record.attempts,
        "binding": binding.as_dict(),
        "binding_digest": binding.digest,
    }
    while len(history) > MAX_TRACKED_TARGETS:
        history.pop(next(iter(history)))
    return record


def clear_failed_selectors(metadata: dict[str, Any], *, target: str) -> bool:
    history = metadata.get(FAILED_SELECTOR_METADATA_KEY)
    if not isinstance(history, dict) or target not in history:
        return False
    del history[target]
    return True
