# PR B — Versioned incremental GitHub ticket-graph observation

Status: Implementation tracked by [issue #423](https://github.com/maemreyo/repoforge/issues/423); this file is its design source of truth.

Follow-on to PR A (batched full graph reader, #411). PR A ships the batched
GraphQL transport with per-capability isolation. PR B redesigns the
incremental observation layer that was stripped out of #411 after
architectural review, because the first attempt built it on a coherence model
that was not strong enough to merge.

This document records the design decisions the review demanded. It is the
source of truth for the PR B ticket; the ticket should reference this file.

## Why not patch the stripped implementation

The review rejected four invariants of the stripped delta cache, all
architectural rather than local:

1. **C1 — dirty-marker race.** `marker.changed_at > payload.observed_at` is a
   wall-clock comparison, not causal ordering. A payload written after a
   webhook can still have never observed that webhook. Only a monotonic
   generation with compare-and-swap is a correct protocol.
2. **C2 — integer issue identity.** `tuple[int, ...]` relationships collapse
   cross-repository sub-issues/dependencies (`repo-B#42` read as `repo-A#42`).
3. **C3 — unproven authorization binding.** `gh:<username>` / `"env-token"`
   does not prove the current credential context; private cache can be served
   across token rotations.
4. **C4 — fake provider revision.** `observed_revision` was a self-hash, not a
   GitHub revision; it proves neither freshness nor integrity.
5. **C5 — non-canonical delta merge.** Delta mutated `TicketNode` directly,
   keeping stale status after reopen/metadata removal instead of re-running
   the shared normalizer.

## Core model

### 1. Global issue identity

```python
@dataclass(frozen=True, order=True)
class IssueRef:
    host: str
    owner: str
    repository: str
    number: int
```

GraphQL selects `repository { nameWithOwner }` on every relationship node.
Relationships (`parent`, `blockers`, `blocks`, `children`) become `IssueRef`
or a local-number projection only when the ref resolves in-repo.

Fail-closed interim (until full cross-repo hydration lands): external refs
produce `CROSS_REPOSITORY_RELATION_UNSUPPORTED`; they are never coerced to a
local number and never let `next` select work past an unevaluated external
blocker. Aligns with #412 external dependency-context nodes.

### 2. Observation layers (per-capability cache)

Separate the single "graph snapshot" into independently cached layers:

- **Topology** — refs and edges only (no bodies). Freshness: short TTL.
- **Core metadata** — title, state, labels, type/status/priority, `updatedAt`.
- **Delivery/evolution** — parsed comment relations, partial-completion,
  design gates, comment revisions (never raw comment bodies long-term).
- **Project overlay** — separate provider and freshness; failure must not
  degrade topology (PR A already isolates this on read; PR B isolates it in
  the cache too).

Envelope:

```python
GraphObservation(
    topology=...,
    metadata=...,
    delivery=...,
    project_overlay=...,
    coverage=...,
    provenance=...,
)
```

### 3. Canonical normalization pipeline

```text
RawIssueObservation ──► shared normalizer ──► NormalizedIssueObservation
                                                      │
                                                      ▼
                                            graph assembler
                                            (full or affected closure)
                                            + diagnostics + coverage
```

Full reads and delta reads go through the SAME normalizer. A delta read:
fetch raw observation, replace the cached raw observation for that ref,
re-run the assembler for the affected closure, recompute diagnostics and
coverage. No direct `TicketNode` mutation. Project-derived fields that cannot
be determined from a delta probe force a project-specific probe or full
refresh. Reopen and metadata-removal are therefore correct by construction.

### 4. CAS cache protocol

```python
@dataclass(frozen=True)
class DirtyLease:
    generation: int
    changed_refs: tuple[IssueRef, ...]
    overflow: bool

class GraphCacheStore(Protocol):
    def read_with_dirty(...) -> tuple[CachedGraph | None, DirtyLease | None]: ...
    def put_if_generation(
        ...,
        payload: CachedGraph,
        expected_dirty_generation: int,
        acknowledged_refs: tuple[IssueRef, ...],
    ) -> PutOutcome: ...
```

Flow: read payload + dirty generation `G` atomically → probe exactly the refs
in `G` → write only if generation is still `G` → on generation bump, keep the
new marker (or merge the new delta) instead of dropping it. Timestamps never
prove observation. `overflow` forces full refresh and never drops old markers.

Webhook handling gains an explicit `ack`/`clear_dirty` so outside-graph
markers stop being re-checked on every read (reviewer M7).

### 5. Batched delta probes

Replace per-issue sequential probes with the existing alias batching:

```python
read_issue_deltas(refs: tuple[IssueRef, ...]) -> BatchDeltaObservation
```

Cost model: `dirty_count <= threshold` → one/two batched delta queries;
above threshold → full topology/core refresh. Removes the N+1 (reviewer H5)
and shrinks the C1 race window.

### 6. Authorization binding

Stop deriving ambient identity inside the gateway. Pass a typed authority
from the auth broker:

```python
@dataclass(frozen=True)
class GitHubObservationAuthority:
    provider_host: str
    actor_id: str
    credential_profile_id: str
    installation_id: str | None
    authorization_generation: str
```

Cache binding stores an opaque digest of the authority; private cache misses
when the authority changes. For un-brokered ambient tokens: either a keyed
local HMAC of `host + token` (never the token or a bare hash) or fail closed
and disable private caching for that mode.

### 7. Provider revision and integrity

Split the fake revision into two fields:

- `payload_checksum` — recomputed canonical JSON, compared with
  `hmac.compare_digest` (PR A already ships this).
- `provider_revision` — `updatedAt`/node-id vector for the root and hydrated
  issues, or a per-capability revision vector, used for conditional/delta
  probes before a cache is treated as fresh. Without a trustworthy provider
  revision, freshness is reported as `ttl_cache` / `webhook_assisted`, never
  as observed.

## What PR B must NOT do

- Do not reuse the stripped dirty-marker timestamp protocol.
- Do not mutate `TicketNode` in a delta path.
- Do not serve private cache across authority changes.
- Do not coerce external relationship refs to local numbers.

## Test requirements

Beyond PR A's coverage, PR B needs: concurrent-CAS interleaving, webhook
after-probe-before-put, reopen-from-Done, metadata-removal, project-derived
status on delta, truncated comments on delta, 50-dirty-issue process count,
deep-graph request bound, cross-repository identity collision, and missed
webhook/restart recovery.

## Relationship to other work

- #412 (multi-root / external dependency context): depends on `IssueRef`.
- #414 (durable state / indexed storage): target for per-capability cache.
- PR A ships the shared transport, normalizer input parsing, and
  per-capability degradation that PR B builds on.
