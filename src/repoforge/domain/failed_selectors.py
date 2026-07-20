"""Durable exact-fingerprint failed-selector chains for targeted verification reruns."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

FAILED_SELECTOR_METADATA_KEY = "failed_selector_history_v1"
FAILED_SELECTOR_SCHEMA_VERSION = 1
MAX_FAILED_SELECTORS = 100
MAX_TRACKED_TARGETS = 16
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_FAILURE_CHAIN_ID = re.compile(r"^failure-chain-[a-f0-9]{24}$")


@dataclass(frozen=True, slots=True)
class FailedSelectorRecord:
    fingerprint: str
    selectors: tuple[str, ...]
    chain_id: str
    attempts: int


def _history(metadata: dict[str, Any]) -> dict[str, Any]:
    history = metadata.get(FAILED_SELECTOR_METADATA_KEY)
    if not isinstance(history, dict):
        history = {}
        metadata[FAILED_SELECTOR_METADATA_KEY] = history
    return history


def read_failed_selectors(metadata: dict[str, Any], *, target: str) -> FailedSelectorRecord | None:
    history = metadata.get(FAILED_SELECTOR_METADATA_KEY)
    if not isinstance(history, dict):
        return None
    raw = history.get(target)
    if not isinstance(raw, dict) or set(raw) != {
        "version",
        "fingerprint",
        "selectors",
        "chain_id",
        "attempts",
    }:
        return None
    fingerprint = raw.get("fingerprint")
    selectors = raw.get("selectors")
    chain_id = raw.get("chain_id")
    attempts = raw.get("attempts")
    if (
        raw.get("version") != FAILED_SELECTOR_SCHEMA_VERSION
        or not isinstance(fingerprint, str)
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
    return FailedSelectorRecord(fingerprint, tuple(selectors), chain_id, attempts)


def record_failed_selectors(
    metadata: dict[str, Any],
    *,
    target: str,
    fingerprint: str,
    selectors: tuple[str, ...],
    chain_id: str | None = None,
) -> FailedSelectorRecord | None:
    if not target or _SHA256.fullmatch(fingerprint) is None:
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
    if selected_chain is None and existing is not None and existing.fingerprint == fingerprint:
        selected_chain = existing.chain_id
    if selected_chain is None:
        digest = hashlib.sha256(
            json.dumps(
                {"target": target, "fingerprint": fingerprint, "selectors": normalized},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        selected_chain = f"failure-chain-{digest[:24]}"
    if _FAILURE_CHAIN_ID.fullmatch(selected_chain) is None:
        return None
    attempts = (
        existing.attempts + 1 if existing is not None and existing.chain_id == selected_chain else 1
    )
    record = FailedSelectorRecord(fingerprint, normalized, selected_chain, attempts)
    history = _history(metadata)
    history[target] = {
        "version": FAILED_SELECTOR_SCHEMA_VERSION,
        "fingerprint": record.fingerprint,
        "selectors": list(record.selectors),
        "chain_id": record.chain_id,
        "attempts": record.attempts,
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
