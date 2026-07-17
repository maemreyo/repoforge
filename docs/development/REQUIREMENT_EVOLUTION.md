# Requirement Evolution and Partial Completion

RepoForge treats requirement changes as append-only delivery evidence. A ticket must not be silently rewritten to make old scope appear completed, and a closed issue is not automatically considered `Done` when its own metadata records remaining or unverified work.

## Canonical relation vocabulary

The live issue body or any of the first 20 bounded comments may declare these fields:

- `Supersedes: #41, #42` — the current ticket is the canonical replacement for the listed tickets.
- `Superseded by: #50` — the current ticket is replaced by the target ticket.
- `Split into: #51, #52` — the current ticket's remaining scope was divided into the listed tickets.
- `Merged into: #53` — the current ticket's remaining scope moved into one canonical ticket.
- `Invalidates: #54, #55` — the current decision invalidates assumptions used by the listed tickets.

Issue references are positive GitHub issue numbers. Relations are normalized deterministically by relation type and issue number, bounded to 128 unique edges, and rendered by `repo_issue_graph`, `repo_issue_next`, and `repo_issue_spec` as safe metadata.

### Reciprocal declarations

When both tickets can be edited, record reciprocal evidence:

```text
#40
Superseded by: #41

#41
Supersedes: #40
```

The readiness engine does not require reciprocal text before excluding the old work, but reciprocal declarations make the history easier to audit. Dependency and parent/sub-issue relationships remain GitHub-native and separate from requirement-evolution relations.

### Canonical replacement rules

- A ticket may have at most one canonical `superseded_by` or `merged_into` replacement.
- Self-targeting relations are invalid.
- Supersession, split, and merge replacement edges must not form cycles.
- A superseded ticket is never selectable, even when its stale declared status is `Ready`.
- A replacement ticket still passes the normal specification, parent, blocker, and WIP checks.
- `Invalidates` does not silently close the target; it derives `Blocked` with `INVALIDATED_ASSUMPTION` so the target can be reviewed and repaired.

Graph defects fail selection closed and appear as typed diagnostics such as `SELF_REQUIREMENT_RELATION`, `AMBIGUOUS_SUPERSESSION`, or `SUPERSESSION_CYCLE`.

## Partial completion record

Use these append-only fields when a PR or issue delivers only part of the declared scope:

```text
Verified deliverables:
- The domain contract is implemented and tested.

Remaining scope:
- Add the public adapter after the surface cutover.

New child issues: #71, #72

Unverified work:
- Remote-provider behavior was not exercised.

Rejected scope:
- Project V2 write authority.

Handoff notes:
- Reuse the shared durable-state repository and revision token.
```

The typed `PartialCompletion` record contains:

- verified deliverables;
- remaining scope;
- new child issues;
- unverified work;
- handoff notes;
- rejected scope as an additive explicit decision.

Each text collection is bounded to 64 items of at most 500 characters. Child issue IDs are bounded, sorted, and unique. This metadata must contain no credentials, source bodies, patches, logs, or raw provider responses.

### Meaning of each field

`Verified deliverables` contains only work supported by tests or other reviewed evidence. `Remaining scope` lists unfinished requirements that still belong to the delivery lineage. `New child issues` names the GitHub issues that own transferred work. `Unverified work` records implementation that exists but cannot be claimed complete. `Rejected scope` records an explicit non-delivery decision and is not treated as unfinished work. `Handoff notes` preserve bounded architectural and verification context.

A closed issue with remaining scope, child issues, or unverified work derives `Blocked` with `PARTIAL_COMPLETION_REMAINS`; it does not derive `Done`. Rejected scope alone does not block completion because it is an explicit decision rather than unfinished work.

## Issue authoring

The initiative and implementation-ticket forms require every evolution and partial-completion field. Enter `None` when a field does not apply. This forces an explicit authoring decision and prevents an absent field from being mistaken for reviewed evidence.

Use comments for append-only changes after creation. Do not erase an earlier relation or partial-completion record to make history look cleaner. Add a new dated comment that explains the correction and declares the current canonical relationship.

## PR closure rules

A pull request may use `Closes #N` only when all accepted scope in #N is completed and verified, or when #N is explicitly closed as a completed foundation whose remaining additive surface has been moved to new tickets.

For partial delivery:

1. record verified deliverables;
2. record every remaining and unverified item;
3. create or identify child/replacement tickets;
4. declare `split_into`, `merged_into`, or another applicable relation;
5. do not close the original issue unless the repository's reviewed decision explicitly treats the handoff as terminal;
6. never describe unverified work as `Done`.

For supersession:

1. declare the canonical replacement;
2. preserve the old issue's historical objective and acceptance criteria;
3. explain why the replacement owns the remaining scope;
4. close the old ticket as superseded only after the relation is reviewable;
5. ensure the replacement is independently executable and testable.

For rejection:

1. record the rejected scope and rationale in safe metadata;
2. use `Invalidates` when the decision makes another ticket's assumptions unsafe;
3. do not move rejected scope into `Remaining scope` unless the decision is later reversed through a new append-only comment.

## Tool behavior

`repo_issue_graph` renders each observed node with an additive `evolution` object. `repo_issue_next` uses the same evidence for derived status, selection, repairs, and diagnostics. `repo_issue_spec` renders the normalized evolution metadata beside the bounded live issue and comments.

GitHub issue and comment access remains read-only. If comment evidence is unavailable, malformed, oversized, or truncated, graph evidence is incomplete and readiness selection fails closed rather than assuming no supersession exists.

## Safe metadata and audit boundaries

Requirement-evolution evidence may include issue IDs, normalized relation types, bounded reasons, bounded decision summaries, statuses, counts, and timestamps. Audit trails contain identifiers and counts only; they do not copy issue bodies or comments. Secret-safe egress policy still applies before any public tool payload is serialized.
