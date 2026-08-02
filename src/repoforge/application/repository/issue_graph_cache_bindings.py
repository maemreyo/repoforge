"""Cache envelope bindings and checksum for the ticket-graph snapshot.

The bindings pin the resolved repository slug, the reviewed source
configuration digest, the reader/query contract version, the API version,
the authority digest, and a payload checksum. The checksum is recomputed
over the payload body (excluding the bindings block) on every read so a
corrupted or hand-edited cache entry fails closed instead of being
served (F-006).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from ...config import GitHubTicketGraphConfig
from ...domain.tickets import GITHUB_API_VERSION, TICKET_GRAPH_READER_VERSION


def source_digest(source: GitHubTicketGraphConfig) -> str:
    """Digest of the reviewed source configuration the snapshot was read under.

    Binds the cache entry to the repository/project fields and field names the
    reader used, so a configuration change is a cache miss instead of stale
    evidence served as current.
    """
    canonical = json.dumps(
        {
            "repository": source.repository,
            "root_issue": source.root_issue,
            "project_owner": source.project_owner,
            "project_number": source.project_number,
            "status_field": source.status_field,
            "priority_field": source.priority_field,
            "initiative_field": source.initiative_field,
            "type_field": source.type_field,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_bindings(
    *,
    source: GitHubTicketGraphConfig,
    authority_digest: str | None,
    repository_slug: str | None,
    payload_body: dict[str, Any],
) -> dict[str, Any]:
    body = {key: value for key, value in payload_body.items() if key != "bindings"}
    payload_checksum = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "cache_schema_version": 3,
        "reader_contract_version": TICKET_GRAPH_READER_VERSION,
        "api_version": GITHUB_API_VERSION,
        "repository_slug": repository_slug,
        "source_digest": source_digest(source),
        "authority_digest": authority_digest,
        "payload_checksum": payload_checksum,
    }


def payload_bindings_valid(
    payload: object,
    source: GitHubTicketGraphConfig,
    expected_slug: str | None,
    authority_digest: str | None,
) -> bool:
    """Whether a cached payload's bindings match the current reader and config."""
    if not isinstance(payload, dict):
        return False
    bindings = payload.get("bindings")
    if not isinstance(bindings, dict):
        return False
    if bindings.get("cache_schema_version") != 3:
        return False
    if bindings.get("reader_contract_version") != TICKET_GRAPH_READER_VERSION:
        return False
    if bindings.get("api_version") != GITHUB_API_VERSION:
        return False
    if expected_slug is None or bindings.get("repository_slug") != expected_slug:
        return False
    if bindings.get("source_digest") != source_digest(source):
        return False
    if authority_digest is None or bindings.get("authority_digest") != authority_digest:
        return False
    stored = bindings.get("payload_checksum")
    if not isinstance(stored, str) or len(stored) != 64:
        return False
    body = {key: value for key, value in payload.items() if key != "bindings"}
    computed = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return hmac.compare_digest(computed, stored)


__all__ = ["build_bindings", "payload_bindings_valid", "source_digest"]
