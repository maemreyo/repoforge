# Sanitized Workflow Record and Replay Design

## Context

Issue #25 requires a deterministic workflow record for behavioral evaluation and incident reproduction without retaining prompts, source, patches, logs, full paths, credentials, private reasoning traces, or provider payloads. Replay must reconstruct only recorded tool-decision categories and must never invoke real repository or GitHub writes.

## Decision

Add an internal schema-versioned `WorkflowRecording` model, a private checksum-framed JSON persistence adapter, a bounded recorder service, and an isolated replay engine. No MCP or CLI surface is added.

## Domain model

A recording contains:

- stable recording and scenario IDs;
- exact server-instruction and tool-surface hashes;
- sorted capability flags;
- ordered workflow events;
- final outcome and bounded metrics;
- creation timestamp;
- explicit truncation state and reason.

Each event contains only:

- monotonic timestamp offset;
- sorted tool inventory IDs and one selected tool ID;
- normalized argument summaries consisting of safe field name, category, SHA-256 hash, and truncation flag;
- result category and stable error code;
- optional safe workspace/task/snapshot identities or hashes;
- sorted next-action IDs;
- state transition category;
- explicit result/argument truncation flags.

Raw values never belong to the persisted schema. Argument helpers categorize values in memory and hash only safe normalized representations. Sensitive/content/path-shaped inputs use fixed omitted markers rather than hashes of the original value.

## Bounds and validation

- At most 256 events per recording.
- At most 128 inventory IDs, 64 argument summaries, and 32 next-action IDs per event.
- Safe IDs are length-bounded and pattern-validated.
- Hashes are full 64-character lowercase SHA-256 values.
- Event offsets are monotonic and bounded.
- Encoded framed records are at most 256 KiB.
- Unknown fields, malformed enums, checksum mismatch, and future schemas fail closed.
- A truncated recording remains readable but is not eligible as complete evaluation evidence.

## Persistence

`JsonWorkflowRecordingStore` writes one deterministic framed JSON file per recording under a private `workflow-recordings/` directory:

```json
{
  "frame_version": 1,
  "payload_sha256": "...",
  "recording": { ... }
}
```

Writes are atomic, fsynced, and mode `0600`; the directory is mode `0700`. The store supports exact reads, bounded listing, explicit fixture export, and deterministic retention pruning by age, count, and total bytes.

Fixture export writes the same verified frame bytes to a caller-provided fixture root using a validated fixture name. It is an explicit internal action and never exports automatically.

## Recorder

`WorkflowRecorder` creates recording IDs through the existing ID generator, validates every event before persistence, truncates explicitly when event or encoded-size bounds are reached, and records only IDs/counts/error codes in audit.

The recorder does not inspect prompts, source files, patches, command output, or provider payloads. Callers must provide typed event decisions or use the safe argument categorizer.

## Replay isolation

`WorkflowReplayEngine` accepts only a `WorkflowReplayAdapter` declaring:

- `isolated = true`;
- `real_writes_enabled = false`.

The default recorded-category adapter returns deterministic replay results from the record itself and performs no filesystem, Git, GitHub, network, or subprocess access. Unsafe adapters are rejected before invocation with `WORKFLOW_REPLAY_UNSAFE`.

By default, truncated recordings fail replay with `WORKFLOW_RECORD_INCOMPLETE`. Diagnostic replay may opt in to an incomplete result, which is always marked `eligible_for_eval = false`.

## Error taxonomy

- `WORKFLOW_RECORD_INVALID`
- `WORKFLOW_RECORD_CORRUPT`
- `WORKFLOW_RECORD_SCHEMA_UNSUPPORTED`
- `WORKFLOW_RECORD_TOO_LARGE`
- `WORKFLOW_RECORD_NOT_FOUND`
- `WORKFLOW_RECORD_INCOMPLETE`
- `WORKFLOW_REPLAY_UNSAFE`

## Testing

Tests cover deterministic direct/failure records, safe argument categorization, forbidden payload rejection, checksum and schema corruption, permissions, event/size truncation, retention, fixture export, replay determinism, incomplete-evidence handling, and rejection of any non-isolated or real-write adapter.

## Non-goals

No prompt logging, source/patch/log persistence, full-path persistence, credentials, private reasoning trace capture, network payload capture, real action replay, generic tracing backend, OpenTelemetry export, production analytics export, or public MCP/CLI tool is introduced.
