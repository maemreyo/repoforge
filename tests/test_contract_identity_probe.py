"""The release contract-identity probe (#367).

A release built from a worktree with stale generated contract artifacts must never
reach ``begin_activation()``/``swap_current()``: its packaged identity
(``generated_contract_identity.py``) would diverge from the in-process registry and
its MCP serve child would die on every spawn. ``release_contract_probe`` is the
structured comparison the release smoke tester runs inside the candidate.
"""

from __future__ import annotations

import pytest

from repoforge.contracts import generated_contract_identity
from repoforge.contracts.registry import (
    contract_identity_digest,
    release_contract_probe,
    render_contract_identity_artifact,
    validate_generated_contract_identity,
)
from repoforge.domain.errors import ConfigError


def test_probe_reports_agreement_when_packaged_matches_the_registry() -> None:
    probe = release_contract_probe()

    assert probe["agreement"] is True
    assert probe["mismatched_fields"] == []
    assert probe["packaged"] == probe["computed"] == render_contract_identity_artifact()


def test_probe_names_the_offending_artifact_path() -> None:
    probe = release_contract_probe()

    assert probe["artifact_paths"]
    assert any("generated_contract_identity.py" in path for path in probe["artifact_paths"])


def test_probe_detects_a_stale_packaged_identity(monkeypatch) -> None:
    stale = dict(generated_contract_identity.CONTRACT_IDENTITY)
    stale["input_contract_digest"] = "0" * 64
    monkeypatch.setattr(generated_contract_identity, "CONTRACT_IDENTITY", stale)

    probe = release_contract_probe()

    assert probe["agreement"] is False
    assert probe["mismatched_fields"] == ["input_contract_digest"]


def test_probe_detects_a_stale_schema_bundle_digest(monkeypatch) -> None:
    """A stale `tool-schemas-v2.json` is the packaged schema-bundle digest drifting."""
    stale = dict(generated_contract_identity.CONTRACT_IDENTITY)
    stale["tool_schema_bundle_digest"] = "1" * 64
    monkeypatch.setattr(generated_contract_identity, "CONTRACT_IDENTITY", stale)

    probe = release_contract_probe()

    assert probe["agreement"] is False
    assert probe["mismatched_fields"] == ["tool_schema_bundle_digest"]


def test_serve_probe_validation_fails_closed_on_stale_packaged(monkeypatch) -> None:
    """The serve-time check (and the supervisor preflight) rejects the packaged drift."""
    stale = dict(generated_contract_identity.CONTRACT_IDENTITY)
    stale["output_contract_digest"] = "2" * 64
    monkeypatch.setattr(generated_contract_identity, "CONTRACT_IDENTITY", stale)

    with pytest.raises(ConfigError, match="CONTRACT_ARTIFACT_MISMATCH"):
        validate_generated_contract_identity()


def test_identity_digest_is_stable_and_field_sensitive() -> None:
    base = render_contract_identity_artifact()

    assert contract_identity_digest(base) == contract_identity_digest(base)

    changed = dict(base)
    changed["output_contract_digest"] = "1" * 64
    assert contract_identity_digest(changed) != contract_identity_digest(base)
