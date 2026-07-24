# Structured Runtime Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ambiguous runtime plaintext and fabricated timestamps with a versioned, secret-safe JSONL envelope that remains compatible with legacy lines and can be correlated to durable control-plane evidence.

**Architecture:** Add one domain-owned runtime event model/parser used by both the tunnel writer and `runtime_logs_read`. The writer persists only redacted JSONL v1 events; the reader parses v1, compatible legacy JSON and plaintext into one bounded public projection with explicit provenance and parse state. Correlation fields are optional and populated only when observed.

**Tech Stack:** Python 3.10+, dataclasses, JSONL, Pydantic v2 contracts, existing RepoForge redaction, exact-node pytest, Ruff, strict Mypy.

## Global Constraints

- Keep the public Forge V2 roster fixed at 28 tools.
- Preserve exact-state, policy, path, branch, approval and final verification invariants.
- Never fabricate timestamps, action names, correlation identities or observed layers.
- Redact secrets and host paths before persistence and again before egress.
- Keep log rotation, fsync, file permissions, bounded reads and snapshot-bound cursors.
- Use exact pytest nodes and quick static verification; do not run the full suite in this plan.

---

### Task 1: Versioned runtime event and compatibility parser

**Files:**
- Create: `src/repoforge/domain/runtime_events.py`
- Modify: `tests/test_runtime.py`

**Interfaces:**
- Produces: `RuntimeEventV1`, `ParsedRuntimeEvent`, `encode_runtime_event(event) -> str`, `parse_runtime_event(line) -> ParsedRuntimeEvent`.
- `ParsedRuntimeEvent.timestamp` is `str | None`; missing time is represented by `None`, never epoch zero.
- `parse_state` is one of `structured_v1`, `legacy_json`, `legacy_plaintext`, `malformed_json`.

- [ ] **Step 1: Write failing parser tests**

```python
def test_runtime_event_parser_never_fabricates_timestamp() -> None:
    parsed = parse_runtime_event("legacy plaintext")
    assert parsed.timestamp is None
    assert parsed.parse_state == "legacy_plaintext"
    assert parsed.timestamp_state == "unavailable"


def test_runtime_event_v1_round_trip_preserves_observed_fields() -> None:
    event = RuntimeEventV1(
        observed_at="2026-07-21T12:00:00+00:00",
        component="tunnel_client",
        stream="stdout",
        level="INFO",
        event_kind="process_output",
        message="ready",
        correlation_id="corr-1",
        operation_id="op-1",
        receipt_id=None,
        trace_id=None,
        workspace_hash=None,
        repository_hash=None,
    )
    assert parse_runtime_event(encode_runtime_event(event)).parse_state == "structured_v1"
```

- [ ] **Step 2: Run RED nodes**

Run:

```bash
pytest tests/test_runtime.py::test_runtime_event_parser_never_fabricates_timestamp -q
pytest tests/test_runtime.py::test_runtime_event_v1_round_trip_preserves_observed_fields -q
```

Expected: collection/import failure because `runtime_events` does not exist.

- [ ] **Step 3: Implement the domain model/parser**

```python
@dataclass(frozen=True, slots=True)
class RuntimeEventV1:
    observed_at: str
    component: str
    stream: str
    level: str
    event_kind: str
    message: str
    correlation_id: str | None = None
    operation_id: str | None = None
    receipt_id: str | None = None
    trace_id: str | None = None
    workspace_hash: str | None = None
    repository_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedRuntimeEvent:
    timestamp: str | None
    timestamp_state: Literal["observed", "unavailable", "invalid"]
    parse_state: Literal[
        "structured_v1", "legacy_json", "legacy_plaintext", "malformed_json"
    ]
    component: str | None
    stream: str | None
    level: str
    event_kind: str | None
    action: str | None
    message: str
    duration_ms: float | None
    correlation_id: str | None
    operation_id: str | None
    receipt_id: str | None
    trace_id: str | None
    workspace_hash: str | None
    repository_hash: str | None
```

`encode_runtime_event` writes a compact object with `schema_version=1`; `parse_runtime_event` accepts v1, legacy JSON keys (`timestamp`, `level`, `message`/`msg`, `action`, `duration_ms`) and plaintext. A string beginning with `{` that cannot be decoded is `malformed_json`, not plaintext certainty.

- [ ] **Step 4: Run GREEN nodes**

Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(runtime): add versioned runtime event parser (#238)"
```

---

### Task 2: Structured tunnel writer with redaction and rotation preservation

**Files:**
- Modify: `src/repoforge/adapters/runtime/tunnel_cli.py`
- Modify: `tests/test_runtime_adapters_and_serve.py`

**Interfaces:**
- Consumes: `RuntimeEventV1`, `encode_runtime_event`.
- Produces: `_append_runtime_event(log_path, event, secrets)`; `_pump_output` emits one JSONL event per child line.

- [ ] **Step 1: Write failing writer tests**

```python
def test_tunnel_writer_persists_secret_safe_runtime_jsonl(tmp_path: Path) -> None:
    client = TunnelCliClient("tunnel-client")
    path = tmp_path / "managed-runtime.log"
    client._append_runtime_event(
        path,
        RuntimeEventV1(
            observed_at="2026-07-21T12:00:00+00:00",
            component="tunnel_client",
            stream="stdout",
            level="INFO",
            event_kind="process_output",
            message="token=secret-value",
        ),
        secrets=("secret-value",),
    )
    payload = json.loads(path.read_text().strip())
    assert payload["schema_version"] == 1
    assert "secret-value" not in json.dumps(payload)
```

- [ ] **Step 2: Run RED node**

Expected: missing `_append_runtime_event`.

- [ ] **Step 3: Implement writer conversion**

- Capture `datetime.now(timezone.utc).isoformat()` when each complete child line is observed.
- Preserve health parsing from the original child line before redaction.
- Persist only the encoded redacted event plus newline.
- Emit a structured `oversized_line` event instead of plaintext omission markers.
- Keep existing lock, rotation, fsync and `0600/0700` permissions.

- [ ] **Step 4: Run writer, rotation and health nodes**

```bash
pytest tests/test_runtime_adapters_and_serve.py::test_tunnel_writer_persists_secret_safe_runtime_jsonl -q
pytest tests/test_runtime_adapters_and_serve.py::test_tunnel_health_uses_advertised_admin_endpoint_and_response_failures -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(runtime): persist structured secret-safe JSONL (#238)"
```

---

### Task 3: Honest bounded runtime-log projection

**Files:**
- Modify: `src/repoforge/application/config_admin/service.py`
- Modify: `src/repoforge/contracts/v2.py`
- Modify: `tests/test_config_admin.py`
- Modify: generated contract artifacts through `make schemas`.

**Interfaces:**
- Consumes: `parse_runtime_event`.
- Produces additional `RuntimeLogEntry` fields:
  - `timestamp: str | None`
  - `timestamp_state: observed | unavailable | invalid`
  - `parse_state: structured_v1 | legacy_json | legacy_plaintext | malformed_json | audit | failure_artifact`
  - `component`, `stream`, `event_kind`
  - optional correlation IDs and safe identity hashes.
- Produces output counters: `malformed_count`, `legacy_count`, `structured_count`.

- [ ] **Step 1: Write failing public behavior tests**

```python
def test_runtime_logs_read_reports_legacy_and_malformed_without_epoch(tmp_path: Path) -> None:
    admin = _admin(tmp_path)
    path = tmp_path / "state" / "managed-runtime.log"
    path.write_text('plain\n{"broken"\n', encoding="utf-8")
    result = admin.runtime_logs_read_v2(source="runtime", limit=10)
    assert all(entry["timestamp"] is None for entry in result["entries"])
    assert [entry["parse_state"] for entry in result["entries"]] == [
        "malformed_json", "legacy_plaintext"
    ]
    assert result["malformed_count"] == 1
    assert result["legacy_count"] == 1
```

- [ ] **Step 2: Run RED node**

Expected: output still contains `1970-01-01...` and contract requires timestamp string.

- [ ] **Step 3: Replace `_runtime_entry` with domain parser projection**

Filtering rules:
- Action filters compare only observed action values.
- Time filters exclude entries whose timestamp is unavailable/invalid.
- Malformed lines remain entries and increment counters; they never abort the page.
- Cursor binding remains filter-bound and snapshot-bound.
- Egress host-path redaction remains applied to message.

- [ ] **Step 4: Regenerate contracts and run GREEN nodes**

```bash
make schemas
pytest tests/test_config_admin.py::test_runtime_logs_read_reports_legacy_and_malformed_without_epoch -q
pytest tests/test_config_admin.py::test_runtime_logs_read_cursor_and_filters_are_snapshot_bound -q
python scripts/check_release_contracts.py
```

Expected: PASS and 28 tools.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(runtime): expose honest runtime log provenance (#238)"
```

---

### Task 4: Cross-layer correlation and production composition

**Files:**
- Modify: `src/repoforge/application/audit_context.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/adapters/runtime/tunnel_cli.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/test_runtime_adapters_and_serve.py`
- Modify: `tests/test_config_admin.py`

**Interfaces:**
- Consumes existing audit attribution/correlation context.
- Writer receives an immutable optional correlation projection containing only observed `correlation_id`, `operation_id`, `receipt_id`, `trace_id`, `workspace_hash`, `repository_hash`.
- Missing values remain `None`; raw workspace/repository/session IDs are never persisted.

- [ ] **Step 1: Write failing integration test**

```python
def test_runtime_event_correlation_matches_safe_audit_context(tmp_path: Path) -> None:
    # Build production composition with a fixed audit attribution context.
    # Emit one runtime event and one audit event for the same operation.
    # Assert the safe correlation ID and operation/receipt IDs match while raw
    # workspace/repository identities do not appear in either persisted file.
```

- [ ] **Step 2: Run RED node**

Expected: runtime events contain no correlation fields from application context.

- [ ] **Step 3: Add optional correlation provider**

- Add a small immutable `RuntimeCorrelation` value in `runtime_events.py`.
- Inject a callable into `TunnelCliClient`; default returns empty correlation.
- Bootstrap composes it from current audit attribution/operation context only where available.
- Never infer connector/client timing or trace IDs.

- [ ] **Step 4: Run integration and regression nodes**

```bash
pytest tests/test_runtime_adapters_and_serve.py::test_runtime_event_correlation_matches_safe_audit_context -q
pytest tests/test_runtime.py -q
pytest tests/test_config_admin.py::test_runtime_logs_read_reports_legacy_and_malformed_without_epoch -q
```

Expected: PASS.

- [ ] **Step 5: Run final focused gates and commit**

```bash
ruff format --check src tests scripts
ruff check src tests scripts
mypy --strict src/repoforge
python scripts/check_release_contracts.py
```

Expected: PASS, 28 tools.

```bash
git commit -am "feat(runtime): correlate structured control-plane events (#238)"
```

## Plan self-review

- Spec coverage: envelope, compatibility parser, zero synthetic timestamps, malformed-line evidence, redaction, rotation/fsync, correlation and bounded retrieval are covered.
- Deferred within #238: aggregate correlation-rate metrics are added only if an existing metrics field can accept them without a new subsystem; otherwise they remain follow-up release-gate work in #244.
- No placeholders: every implementation task has exact files, interfaces, tests and commands.
- Type consistency: the parser projection and public contract use the same optional timestamp and provenance enums.
