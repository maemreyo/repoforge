# Phase 7 Atomic Hot Reload

Phase 7 implements optional in-process configuration activation while retaining the Phase 4
supervisor-managed restart path as the compatibility and recovery fallback. The implementation is
based on `dev@ada5d6fca145f66a690cbe851268a65cd127cc76` and also closes the five regressions captured
while Phase 6 was verified on Darwin.

## Safety invariants

- Every MCP request is pinned to exactly one immutable generation-scoped application container.
- A candidate is fully constructed and self-checked before it can become visible.
- The durable active-generation pointer and the in-process router swap are serialized against new
  request acquisition.
- Existing requests retain their old container until completion; new requests see only the complete
  new container.
- Candidate construction or durable activation failure leaves the old container active.
- Retired containers are fail-closed and disposed only after their active request count reaches zero.
- An incompatible generation returns `HOT_RELOAD_RESTART_REQUIRED`; the existing supervisor restart
  remains the fallback.
- Repository-only generation changes never reinitialize the tunnel profile or replace the tunnel
  process.

## Generation-scoped container

`GenerationServiceContainer` owns one complete application graph:

- accepted configuration generation;
- `CodingService` compatibility facade backed by that generation;
- a generation-local operation gate;
- the immutable repository-ID set used by candidate self-checks;
- a bounded disposal callback.

No configuration dictionaries or service instances are mutated in place. Candidate construction
loads the immutable resolved snapshot for the requested generation and builds a fresh application
composition through `bootstrap.py`.

## Atomic request routing

`AtomicServiceRouter.acquire()` selects the active container while holding one condition lock and
increments the request count for that generation. The selected container remains pinned for the
entire MCP tool invocation.

Hot reload performs the following ordered transaction:

1. Serialize reload attempts.
2. Verify the caller's expected active generation.
3. Build a complete candidate container.
4. Run `repo_list` against the candidate and compare the observed repository set with its immutable
   metadata.
5. Hold the router condition so new requests cannot enter.
6. Commit the durable active-generation pointer with its optimistic guard.
7. Swap the active container reference.
8. Release new request acquisition.
9. Dispose the retired container immediately when idle, or after its last pinned request completes.

This ordering prevents a request from observing a disk generation that does not match its service
container and prevents partial mixtures of old and new adapters.

## Runtime control and activation

The local owner-only runtime protocol adds the allowlisted `RELOAD` command. Reload accepts only:

- `generation`: positive integer;
- `expected_active`: zero/absent or a positive integer.

Duplicate fields, unsupported fields, booleans, unhashable values, and invalid drain timeouts are
rejected. The protocol never accepts executable names, command arguments, file content, or arbitrary
shell input.

`GenerationActivator` attempts hot reload for compatible metadata, equivalent, expansion, and
restriction generations while a healthy/degraded MCP runtime is present. It stages the target, asks
the MCP host to reload, and treats the durable active pointer as the reconciliation source if the IPC
response is lost. Incompatible or unsupported reloads continue through bounded drain, restart,
health check, and rollback.

After a successful reload, the supervisor runtime record is advanced without replacing the tunnel
child. If that child later crashes, `RuntimeSupervisor` adopts the newer generation only when both the
durable active pointer and identity-validated runtime record agree, then restarts the child against
that generation. It does not require a second activation commit and does not reinitialize an
unchanged tunnel profile.

## Removed repository workspaces

A new workspace stores an integrity-protected, read-boundary-only repository policy snapshot. The
snapshot deliberately excludes command profiles and publishing capability.

When a repository is absent from the active generation:

- new repository requests fail immediately because it is no longer in the active container;
- existing workspaces are reported as `orphaned_read_only`;
- only reads constrained by the signed snapshot remain available;
- mutation, verification, commit, push, and pull-request operations fail closed;
- executable profiles and publishing are unavailable;
- explicit workspace cleanup remains possible;
- missing, legacy, malformed, or tampered snapshots fall back to metadata-only access with all paths
  denied.

The SHA-256 snapshot protects against local registry edits silently broadening the orphan read
boundary.

## Darwin and Phase 6 regression closure

The same change set fixes the five verification failures preceding Phase 7:

- MCP structured failures raise a real tool error while preserving the stable JSON envelope.
- Unix peer credential discovery falls through from incompatible `SO_PEERCRED` layouts to Python or
  native BSD/Darwin `getpeereid`.
- Normal, hashed, and long-`TMPDIR` Unix socket paths authenticate the same local owner.
- Tunnel-child finalization retains process/log tracking until the bounded log pump reaches EOF and
  persists its final omission marker.

## Verification contract

Phase 7 is complete only when a clean checkout passes:

```bash
uv sync --extra dev --frozen
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy --strict src/repoforge
uv run pytest --cov=repoforge --cov-branch --cov-report=term-missing
uv build
```

The regression suite covers atomic routing under concurrent reads, request pinning, candidate and
commit failure, config-store integration, removed-repository policy, lost IPC response
reconciliation, crash-after-hot-reload supervisor fallback, MCP error semantics, Darwin peer
credentials, portable socket paths, and live bounded log drainage.
