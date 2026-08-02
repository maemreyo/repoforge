"""Cache envelope codec and binding validation for the ticket-graph snapshot.

Thin re-export shim around
:mod:`issue_graph_cache_codec` (snapshot encoding/decoding, strict
primitives, age calculation) and :mod:`issue_graph_cache_bindings`
(repository/source/authority bindings and payload checksum). Splitting
keeps each module under the 400-line file-length rule.
"""

from .issue_graph_cache_bindings import payload_bindings_valid, source_digest
from .issue_graph_cache_codec import (
    _ALLOWED_CACHE_SKEW_MS,
    observed_age_ms,
    snapshot_from_payload,
    snapshot_payload,
)

__all__ = [
    "_ALLOWED_CACHE_SKEW_MS",
    "observed_age_ms",
    "payload_bindings_valid",
    "snapshot_from_payload",
    "snapshot_payload",
    "source_digest",
]
