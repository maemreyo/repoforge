# Secret-Safe Egress Policy

RepoForge applies one local, typed policy before structured application results become model-, UI-, diagnostic-, trace-, recording-, or attestation-visible data.

## Decisions

`evaluate_egress` returns exactly one decision:

- `allow` — no secret evidence was detected; bounded content may be emitted.
- `redact_ranges` — useful context remains after deterministic ranges are replaced.
- `withhold_snippet` — the source is denied or contains material such as a private-key block that must not be partially emitted.
- `reject_result` — the input is binary, invalid for its declared encoding, or exceeds the reviewed input bound.

A result contains sanitized content or no content, normalized finding IDs/categories, safe ranges, counts, source digest, truncation metadata, and a policy reason. It never contains the detected secret value.

## Detection order

The engine evaluates bounded content for:

1. repository-denied source decisions supplied by the caller;
2. private-key blocks;
3. approved provider token shapes;
4. bearer authorization and credential-bearing URLs;
5. sensitive assignment/config keys;
6. caller-provided exact secret identities;
7. contextual high-entropy candidates using Shannon entropy and false-positive allowlists.

Overlapping detections are merged before redaction. Findings remain individually traceable through deterministic IDs while the rendered range is replaced only once.

## False-positive controls

The policy explicitly allows recognized:

- Git object and SHA-256 identities;
- UUIDs;
- lockfile integrity strings;
- stable RepoForge selectors and snapshot identities;
- public hostname/path URL bodies;
- caller-declared public fixtures.

High-entropy evidence requires a high Shannon-entropy threshold and character diversity; long branch names, repository paths, source selectors, and ordinary URLs are not secrets merely because they contain several character classes.

## Structured payload boundary

`CodingService._result` is the common application serialization boundary. It converts typed application results to data, unwraps the established `payload` envelope, then recursively applies `sanitize_egress_data` before returning an object to MCP or CLI callers.

The recursive policy:

- redacts values under known sensitive keys immediately;
- scans every other string and byte value;
- preserves safe scalar types, empty strings, list/object shape, hashes, selectors, and public URLs;
- bounds nesting and collection sizes;
- never mutates repository or workspace source.

An adapter that emits data outside `CodingService` must call `evaluate_egress` or `sanitize_egress_data` explicitly before serialization.

## Compatibility helpers

`domain.redaction.redact_text`, `redact_data`, and `sanitize_persisted_data` now delegate secret detection to the central engine while preserving established legacy markers such as `<redacted>`.

`sanitize_ci_text` uses central egress ranges before denied-path line withholding, preserving multiline private-key protection and existing CI evidence rendering such as `<redacted:private-key>`.

## Safe metadata only

Audit, metrics, diagnostics, findings, recordings, and errors may retain:

- finding ID and category;
- confidence and policy reason;
- redaction ranges and counts;
- source digest and snapshot identity;
- bounded truncation/withholding metadata.

They must not retain the secret, source body, patch, credential-bearing environment data, or raw provider output containing the secret.

## Integration checklist

Before adding a new outbound path:

1. Identify the content class and destination.
2. Supply repository path denial as a decision, not as trusted text.
3. Pass known in-process secret identities without persisting them.
4. Add positive, adversarial, Unicode/binary, bound, false-positive, and no-secret-in-findings tests.
5. Verify the payload through the public serialization boundary.
6. Run the focused tests and the authoritative production gate.
