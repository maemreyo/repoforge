# GitHub Webhook Cache Invalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Optionally invalidate RepoForge's GitHub graph cache immediately when issue, dependency, or Project item state changes, while retaining TTL/live reads as the correctness path.

**Architecture:** Add a standalone, explicitly enabled HTTP ingress rather than mixing unauthenticated routes into the stdio MCP server. A small Starlette adapter verifies GitHub HMAC signatures and bounded payloads, deduplicates delivery IDs in private state, resolves the configured repository, and calls only the cache invalidation port. The existing tunnel may expose this second local endpoint only when the operator opts in.

**Tech Stack:** Starlette, Uvicorn, HMAC-SHA256, existing atomic JSON persistence and lock manager, pytest HTTP client.

## Global Constraints

- Webhooks improve freshness only; missed webhooks cannot affect correctness.
- No webhook payload triggers GitHub writes, repository writes, command execution, or configuration expansion.
- Secrets come from an environment variable and never from committed config or audit logs.
- Reject unsigned, oversized, stale, unknown-repository, and unsupported-event requests.

---

### Task 1: Add typed webhook configuration and delivery deduplication

**Files:**
- Modify: `src/repoforge/config.py`
- Create: `src/repoforge/ports/webhook_deliveries.py`
- Create: `src/repoforge/adapters/persistence/json_webhook_deliveries.py`
- Modify: `src/repoforge/adapters/persistence/__init__.py`
- Test: `tests/test_github_webhook_ingress.py`

**Interfaces:**
- Produces server fields `github_webhook_enabled: bool = false`, `github_webhook_bind: str = "127.0.0.1"`, `github_webhook_port: int = 8766`, `github_webhook_secret_env: str = "REPOFORGE_GITHUB_WEBHOOK_SECRET"`, and `github_webhook_max_body_bytes: int = 1_000_000`.
- Produces `WebhookDeliveryStore.claim(delivery_id: str, received_at: float) -> bool` with bounded retention.

- [ ] **Step 1: Write failing config and dedup tests**

Cover disabled defaults, loopback-only bind validation, invalid ports/env names, first claim, duplicate claim, retention eviction, corruption fallback, and private file permissions.

- [ ] **Step 2: Implement typed config and atomic delivery store**

Use the existing lock/atomic-write patterns; store only delivery ID hashes and timestamps, never payloads.

- [ ] **Step 3: Run narrow tests**

Expected: PASS.

### Task 2: Implement signature verification and event normalization

**Files:**
- Create: `src/repoforge/application/webhooks/github.py`
- Test: `tests/test_github_webhook_ingress.py`

**Interfaces:**
- Produces:

```python
def verify_github_signature(body: bytes, header: str, secret: bytes) -> bool: ...
def affected_repository(event: str, payload: dict[str, Any]) -> str | None: ...
```

- [ ] **Step 1: Add failing HMAC and payload tests**

Cover valid SHA-256, malformed/missing signature, wrong secret, constant-time comparison behavior, malformed JSON, unsupported events, and repository extraction.

- [ ] **Step 2: Implement bounded pure helpers**

Accept only `issues`, `issue_dependencies`, and `projects_v2_item`; return no ticket content to audit metadata.

- [ ] **Step 3: Run the helper tests**

Expected: PASS.

### Task 3: Add the opt-in HTTP ingress

**Files:**
- Create: `src/repoforge/interfaces/http/github_webhooks.py`
- Create: `src/repoforge/interfaces/http/__init__.py`
- Modify: `src/repoforge/interfaces/cli/main.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_github_webhook_ingress.py`

**Interfaces:**
- Adds CLI command `rf webhook serve`.
- Exposes `POST /github/webhooks` only on the configured loopback bind.

- [ ] **Step 1: Write failing HTTP behavior tests**

Assert `202` for a new supported delivery, `202` idempotently for a duplicate, `401` for bad signature, `413` for oversized body, `400` for malformed JSON, `422` for unknown repository, and `204` for a signed unsupported event.

- [ ] **Step 2: Implement the Starlette endpoint**

Read the body once within the byte limit, verify signature before parsing, claim delivery ID, resolve repository by GitHub slug, and call `github_read_cache.invalidate(..., kind="graph")` only.

- [ ] **Step 3: Add CLI startup checks**

Fail closed if disabled, secret env is absent, bind is non-loopback, or the port is unavailable. Never print the secret.

- [ ] **Step 4: Run ingress tests**

Expected: PASS with no outbound GitHub or subprocess calls.

### Task 4: Document setup and verify security boundaries

**Files:**
- Create: `docs/operations/GITHUB_WEBHOOKS.md`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `CHANGELOG.md`
- Modify: `config.example.toml`
- Test: `tests/test_security.py`

**Interfaces:**
- Produces operator steps for subscribing to `issues`, `issue_dependencies`, and `projects_v2_item`, configuring the secret, and exposing the optional endpoint.

- [ ] **Step 1: Add negative security tests**

Assert payload bodies/signatures/secrets never appear in audit/runtime logs, webhook handling cannot invoke commands, and disabled configuration opens no listener.

- [ ] **Step 2: Document GitHub App permissions and tunnel routing**

Require read-level Issues and Projects permissions. Explain that TTL/fresh reads remain authoritative when webhook delivery is unavailable.

- [ ] **Step 3: Run webhook, security, config, and cache suites**

Expected: PASS.

- [ ] **Step 4: Run the repository `full` profile and commit**

Commit message:

```text
feat(webhooks): invalidate GitHub graph cache safely
```
