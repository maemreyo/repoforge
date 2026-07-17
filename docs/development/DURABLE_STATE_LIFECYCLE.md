# Durable-state lifecycle operations

RepoForge durable state uses typed schema/revision envelopes and private atomic JSON records. The lifecycle layer adds schema migration, reference-aware retention, integrity inspection, portable backup, and operator-controlled restore without adding MCP tools or resources.

## Safety boundary

Lifecycle operations are application and adapter contracts for operator-owned workflows. They are not unrestricted filesystem operations.

- Collection names and record identifiers are validated and bounded.
- Records remain private (`0700` directories and `0600` files).
- Preview results contain identities, versions, checksums, byte counts, dispositions, conflicts, and reason codes only.
- Integrity findings, journals, and manifests never contain source bodies, patches, credentials, raw logs, or arbitrary environment data.
- Every mutation is bound to a deterministic preview digest and current record checksums.
- Unknown or future schema versions fail closed unless an explicit adjacent migration path is registered.
- No cleanup runs automatically. An operator or reviewed application workflow must request preview and apply separately.

## Schema migrations

Register one `StateMigrationStep` for every adjacent version edge. A forward transform is mandatory. A reverse transform is optional, but reverse planning fails closed unless every traversed step explicitly provides one.

Transforms must:

- accept and return `dict[str, object]` JSON payloads;
- be deterministic for identical input;
- remain within the encoded record-size bound;
- avoid I/O and external state;
- preserve meaning across the declared version edge.

`preview_migration` scans bounded shared envelopes and returns exact source/target checksums. `apply_migration` validates those checksums, writes a private backup and journal before the first replacement, then atomically replaces each record. Ordinary failures restore exact original bytes. A process interruption leaves an `applying` journal; `recover_incomplete_migrations` restores from the checksum-verified backup after restart.

No-op migrations do not create backup data.

## Retention, references, and quotas

Retention policy is consumer-neutral. The caller supplies:

- an ISO-8601 timestamp for every current record;
- explicit protected records and safe reason codes;
- typed source-to-target references;
- retention age, record quota, byte quota, and batch size.

Referenced targets and explicit protections are never selected. Missing targets become bounded orphan findings. Selection is deterministic: expired records first, then oldest unprotected records required to satisfy count and byte quotas.

`apply_cleanup` rechecks source checksums and moves selected records into a private plan-specific trash directory. It does not physically purge them. Repeating the same plan is idempotent, and a process interruption can resume because already-moved records are verified in trash before the remaining candidates are processed.

Active tasks, running operations, accepted plans, current receipts, evidence required by audit policy, and other live state must be supplied as protections by their owning application service.

## Integrity inspection

`inspect_integrity` reports only safe metadata. Findings include:

- corrupt or identity-inconsistent envelopes;
- unsupported schema versions;
- missing or corrupt reference targets;
- record-count and byte-quota violations;
- bounded-scan truncation.

Findings are deterministically ordered and capped. Payload values are never included in the report. An empty error set means the inspected collection is healthy for the supplied supported-version and quota policy; it is not a substitute for application-specific semantic validation.

## Portable backup

`preview_backup` creates a deterministic manifest from record identity, schema version, revision, checksum, size, collection, destination identity, and total bytes. Corrupt sources or exceeded quotas fail closed.

`apply_backup` requires a destination directory whose final name matches the reviewed destination identity. It copies checksum-verified private record bytes and writes the manifest last, so an interrupted copy is never mistaken for a completed backup. A restart may resume only when every partial record already present matches the reviewed preview; unrelated entries or mismatched bytes fail closed. Reapplying an unchanged manifest is idempotent. `repair=True` is an explicit operator action that rewrites a damaged destination from the still-current source preview.

A backup directory contains:

```text
<destination-id>/
  manifest.json
  records/
    <record-id>.json
```

The manifest contains no record payloads. Readers require the exact format-version-1 field set and native JSON types, reject unknown fields and duplicate record IDs, verify the deterministic backup identity and manifest checksum, and decode every record to confirm identity, schema version, revision, size, and checksum metadata.

## Restore

`preview_restore` verifies the manifest checksum, every record checksum, record and byte quotas, supported schema versions, caller-supplied references, and current destination conflicts. The reviewed destination identity must equal the final directory name of the current state root. The preview binds the backup identity, destination identity, supported-version set, references, overwrite decision, conflicts, and record metadata into one restore digest.

Without `overwrite=True`, different existing records block apply. Unsupported record schemas and references from restored records to missing or corrupt targets block preview. With overwrite enabled, destination bytes are copied into a private restore-specific backup before the first write. The restore journal records only created/replaced record IDs and safe operation metadata.

Ordinary failures restore replaced records and remove records created by the failed operation. If the process stops during apply, `recover_incomplete_restores` uses the private destination backup and journal metadata to roll the destination back after restart. Reapplying a committed restore returns the original structured report.

## Compatibility and non-goals

- Existing `JsonStateRepository` serialization remains unchanged.
- TaskCapsule, approval, and OperationTask public contracts are unchanged.
- No MCP or CLI surface is added by this lifecycle foundation.
- Remote databases, scheduled automatic cleanup, secret remediation, content export through model-facing responses, and history-rewriting restore behavior are out of scope.
- Backup files contain private durable-state bytes and must remain under operator-controlled storage and filesystem permissions.
