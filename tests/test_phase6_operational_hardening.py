from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from repoforge.adapters.audit import JsonlAuditSink
from repoforge.adapters.observability import JsonMetricsSink
from repoforge.adapters.persistence import JsonIdempotencyStore
from repoforge.adapters.runtime.tunnel_cli import TunnelCliClient
from repoforge.application.context import ApplicationContext
from repoforge.application.diagnostics.bundle import build_diagnostics_bundle
from repoforge.application.repository.doctor import Doctor, DoctorCommand
from repoforge.config import AppConfig, RepositoryConfig, ServerConfig, load_config
from repoforge.domain.errors import ConfigError, ErrorCode
from repoforge.domain.operations import (
    IdempotencyRecord,
    IdempotencyState,
    automatic_retry_allowed,
    hash_idempotency_key,
    request_fingerprint,
)
from repoforge.domain.redaction import redact_data
from repoforge.domain.runtime import TunnelProfile
from repoforge.interfaces.mcp.server import _ServiceErrorBoundary
from repoforge.ports.command import CommandResult
from repoforge.testing import FixedClock, InMemoryLockManager, SequenceIdGenerator


class NullCommand:
    def environment(self, extra=None):
        return dict(extra or {})

    def run(self, argv, *, cwd, **kwargs):
        del kwargs
        return CommandResult(tuple(argv), str(cwd), 0, "ok", "")

    def run_bytes(self, argv, *, cwd, timeout=None, max_bytes=1024):
        del argv, cwd, timeout, max_bytes
        return b""


class NullGit:
    executor = None


class NullGithub:
    pass


class LocalFiles:
    def exists(self, path):
        return path.exists()

    def is_dir(self, path):
        return path.is_dir()

    def is_file(self, path):
        return path.is_file()

    def is_symlink(self, path):
        return path.is_symlink()

    def size(self, path):
        return path.stat().st_size

    def read_bytes(self, path):
        return path.read_bytes()

    def read_text(self, path):
        return path.read_text(encoding="utf-8")

    def write_bytes_atomic(self, path, data, *, preserve_mode=True):
        del preserve_mode
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def unlink(self, path, *, missing_ok=False):
        path.unlink(missing_ok=missing_ok)

    def mkdir(self, path, *, parents=True, exist_ok=True):
        path.mkdir(parents=parents, exist_ok=exist_ok)


class MemoryStore:
    def save(self, record):
        del record

    def load(self, workspace_id):
        raise KeyError(workspace_id)

    def delete(self, workspace_id):
        del workspace_id

    def list(self):
        return []


class OpenGate:
    def operation(self, operation_id, *, mutating):
        del operation_id, mutating
        return nullcontext()

    def begin_drain(self, *, reason, correlation_id):
        del reason, correlation_id

    def fail_closed(self, *, reason, correlation_id):
        del reason, correlation_id

    def reopen(self):
        pass

    def wait_for_idle(self, timeout_seconds):
        del timeout_seconds
        return True

    def snapshot(self):
        return {"state": "open", "active_reads": 0, "active_writes": 0}


class NullExecutable:
    def which(self, executable, *, path=None):
        del executable, path
        return None


def _context(tmp_path: Path) -> ApplicationContext:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    state_root = tmp_path / "state"
    server = ServerConfig(
        tmp_path / "workspaces",
        state_root,
        audit_max_bytes=450,
        audit_backup_count=2,
        runtime_log_max_bytes=450,
        runtime_log_backup_count=2,
        idempotency_stale_seconds=60,
    )
    config = AppConfig(
        tmp_path / "config.toml",
        server,
        {"demo": RepositoryConfig("demo", repo_path, fetch_before_workspace=False)},
    )
    locks = InMemoryLockManager()
    clock = FixedClock("2026-07-13T00:00:00+00:00")
    return ApplicationContext(
        config,
        NullCommand(),
        NullGit(),
        NullGithub(),
        LocalFiles(),
        MemoryStore(),
        locks,
        OpenGate(),
        JsonlAuditSink(
            state_root,
            clock,
            max_bytes=server.audit_max_bytes,
            backup_count=server.audit_backup_count,
        ),
        clock,
        SequenceIdGenerator(("correlation-000000000000",)),
        NullExecutable(),
        JsonMetricsSink(state_root, locks),
        JsonIdempotencyStore(state_root),
    )


def test_recursive_redaction_and_audit_retention_are_private(tmp_path: Path) -> None:
    redacted = redact_data(
        {
            "authorization": "Bearer abc",
            "nested": {"message": "token=secret-value", "safe": 7},
            "items": ["https://user:password@example.test/path"],
        }
    )
    assert redacted["authorization"] == "<redacted>"
    assert "secret-value" not in json.dumps(redacted)
    assert "password" not in json.dumps(redacted)

    sink = JsonlAuditSink(tmp_path, FixedClock(), max_bytes=300, backup_count=2)
    for index in range(10):
        sink.record(
            "workspace_push",
            success=False,
            details={"index": index, "api_key": "top-secret", "message": "x" * 80},
        )

    assert sink.path.is_file()
    assert sink.path.with_suffix(".jsonl.1").is_file()
    assert not sink.path.with_suffix(".jsonl.3").exists()
    assert sink.path.stat().st_mode & 0o777 == 0o600
    assert sink.path.parent.stat().st_mode & 0o777 == 0o700
    for path in sink.path.parent.glob("audit.jsonl*"):
        assert "top-secret" not in path.read_text(encoding="utf-8")

    bounded = JsonlAuditSink(tmp_path / "bounded", FixedClock(), max_event_bytes=2_048)
    bounded.record("huge", success=False, details={"message": "z" * 100_000})
    assert bounded.path.stat().st_size <= 2_048
    bounded_payload = json.loads(bounded.path.read_text(encoding="utf-8"))
    assert bounded_payload["details"]["event_truncated"] is True
    assert len(bounded_payload["details"]["event_sha256"]) == 64
    assert "preview" not in bounded_payload["details"]
    assert "z" * 100 not in bounded.path.read_text(encoding="utf-8")


def test_audit_persistence_failure_is_stable_and_does_not_mask_primary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = JsonlAuditSink(tmp_path / "audit-failure", FixedClock())
    real_open = os.open

    def fail_audit_open(path: object, flags: int, mode: int = 0o777) -> int:
        if str(path).endswith("audit.jsonl"):
            raise OSError("disk full")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_audit_open)
    with pytest.raises(ConfigError) as persisted:
        sink.record("workspace_push", success=True, details={"safe": True})
    assert persisted.value.code is ErrorCode.STATE_PERSISTENCE_FAILED
    assert persisted.value.retryable is True

    class FailingAudit:
        path = tmp_path / "failing-audit.jsonl"

        def record(self, action: str, *, success: bool, details: dict[str, Any]) -> None:
            del action, success, details
            raise ConfigError(
                "STATE_PERSISTENCE_FAILED: audit unavailable",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            )

    context_root = tmp_path / "context"
    context_root.mkdir()
    ctx = replace(_context(context_root), audit=FailingAudit())
    with pytest.raises(ConfigError) as primary:
        ctx.audited(
            "workspace_push",
            {"repo_id": "demo"},
            lambda: (_ for _ in ()).throw(ConfigError("COMMAND_TIMEOUT: upstream")),
        )
    assert primary.value.code is ErrorCode.COMMAND_TIMEOUT


def test_metrics_sink_aggregates_duration_and_failure_category(tmp_path: Path) -> None:
    locks = InMemoryLockManager()
    metrics = JsonMetricsSink(tmp_path, locks)
    metrics.record("workspace_push", success=True, duration_ms=12.5, error_code=None)
    metrics.record("workspace_push", success=False, duration_ms=7.5, error_code="COMMAND_TIMEOUT")

    snapshot = metrics.snapshot()
    item = snapshot["operations"]["workspace_push"]
    assert item == {
        "count": 2,
        "successes": 1,
        "failures": 1,
        "duration_ms_total": 20.0,
        "duration_ms_max": 12.5,
        "failure_categories": {"COMMAND_TIMEOUT": 1},
    }
    assert metrics.path.stat().st_mode & 0o777 == 0o600


def test_idempotency_store_rejects_corrupt_identity_and_wraps_persistence_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonIdempotencyStore(tmp_path)
    key_hash = hash_idempotency_key("identity-key-0001")
    path = store._path("workspace_push", key_hash)
    path.write_text(
        json.dumps(
            {
                "action": "workspace_create",
                "key_hash": key_hash,
                "request_fingerprint": "a" * 64,
                "state": "completed",
                "updated_at": "2026-07-13T00:00:00+00:00",
                "updated_at_epoch": 1.0,
                "correlation_id": "correlation",
                "result": {"ok": True},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as corrupt:
        store.load("workspace_push", key_hash)
    assert corrupt.value.code is ErrorCode.STATE_PERSISTENCE_FAILED
    assert corrupt.value.retryable is False

    record = IdempotencyRecord(
        "workspace_push",
        key_hash,
        "a" * 64,
        IdempotencyState.COMPLETED,
        "2026-07-13T00:00:00+00:00",
        1.0,
        "correlation",
        {"ok": True},
    )

    def fail_replace(source: object, destination: object) -> None:
        del source, destination
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(ConfigError) as failed:
        store.save(record)
    assert failed.value.code is ErrorCode.STATE_PERSISTENCE_FAILED
    assert failed.value.retryable is True


def test_idempotency_replays_completed_result_and_rejects_conflict(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    calls = 0

    def operation() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"workspace_id": "demo-1", "created": True}

    first = ctx.idempotent(
        "workspace_create",
        "request-12345678",
        {"repo_id": "demo", "task_slug": "task"},
        operation,
    )
    second = ctx.idempotent(
        "workspace_create",
        "request-12345678",
        {"repo_id": "demo", "task_slug": "task"},
        operation,
    )

    assert first == second
    assert calls == 1
    raw = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "state" / "idempotency").glob("*.json")
    )
    assert "request-12345678" not in raw

    with pytest.raises(ConfigError) as error:
        ctx.idempotent(
            "workspace_create",
            "request-12345678",
            {"repo_id": "demo", "task_slug": "different"},
            operation,
        )
    assert error.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert error.value.retryable is False


def test_idempotency_in_progress_is_retryable_and_stale_claim_recovers(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    assert ctx.idempotency is not None
    ctx.idempotency.save(
        IdempotencyRecord(
            "workspace_push",
            hash_idempotency_key("push-key-12345678"),
            request_fingerprint({"workspace_id": "demo"}),
            IdempotencyState.IN_PROGRESS,
            "1970-01-01T00:00:00+00:00",
            0.0,
            "test-correlation",
        )
    )
    # The fixed clock is in 2026, so the synthetic claim is stale and can be recovered.
    result = ctx.idempotent(
        "workspace_push",
        "push-key-12345678",
        {"workspace_id": "demo"},
        lambda: {"pushed": True},
    )
    assert result == {"pushed": True}

    ctx.idempotency.save(
        IdempotencyRecord(
            "workspace_push",
            hash_idempotency_key("live-key-12345678"),
            request_fingerprint({"workspace_id": "demo"}),
            IdempotencyState.IN_PROGRESS,
            ctx.clock.now_iso(),
            ctx.now_epoch(),
            "live-correlation",
        )
    )
    with pytest.raises(ConfigError) as error:
        ctx.idempotent(
            "workspace_push",
            "live-key-12345678",
            {"workspace_id": "demo"},
            lambda: {"pushed": True},
        )
    assert error.value.code is ErrorCode.IDEMPOTENCY_IN_PROGRESS
    assert error.value.retryable is True


def test_automatic_retry_policy_is_limited_to_keyed_proven_operations() -> None:
    assert automatic_retry_allowed(
        "workspace_push", ErrorCode.COMMAND_TIMEOUT, has_idempotency_key=True
    )
    assert automatic_retry_allowed(
        "workspace_create_draft_pr", ErrorCode.RUNTIME_UNAVAILABLE, has_idempotency_key=True
    )
    assert automatic_retry_allowed(
        "workspace_update_draft_pr",
        ErrorCode.STATE_PERSISTENCE_FAILED,
        has_idempotency_key=True,
    )
    assert not automatic_retry_allowed(
        "workspace_push", ErrorCode.COMMAND_TIMEOUT, has_idempotency_key=False
    )
    assert not automatic_retry_allowed(
        "workspace_write_file", ErrorCode.COMMAND_TIMEOUT, has_idempotency_key=True
    )
    assert not automatic_retry_allowed(
        "workspace_push", ErrorCode.SECURITY_POLICY_VIOLATION, has_idempotency_key=True
    )


def test_audited_operation_records_metrics_and_structured_unchanged_state(tmp_path: Path) -> None:
    ctx = _context(tmp_path)

    with pytest.raises(ConfigError) as error:
        ctx.audited(
            "workspace_push",
            {"workspace_id": "missing", "repo_id": "demo"},
            lambda: (_ for _ in ()).throw(ConfigError("COMMAND_TIMEOUT: upstream unavailable")),
        )

    assert error.value.correlation_id
    assert error.value.unchanged_state
    assert ctx.metrics is not None
    metric = ctx.metrics.snapshot()["operations"]["workspace_push"]
    assert metric["failures"] == 1
    assert metric["failure_categories"] == {"COMMAND_TIMEOUT": 1}


def test_doctor_redacts_secrets_before_direct_cli_or_service_rendering(tmp_path: Path) -> None:
    class SecretCommand(NullCommand):
        def environment(self, extra=None):
            del extra
            return {"PATH": "/bin", "CONTROL_PLANE_API_KEY": "doctor-secret-value"}

        def run(self, argv, *, cwd, **kwargs):
            del kwargs
            return CommandResult(tuple(argv), str(cwd), 0, "token=doctor-secret-value", "")

    class FoundExecutable:
        def which(self, executable, *, path=None):
            del path
            return f"/bin/{executable}"

    ctx = replace(
        _context(tmp_path),
        commands=SecretCommand(),
        executables=FoundExecutable(),
    )
    result = Doctor(ctx).execute(DoctorCommand())
    encoded = json.dumps(result.checks)
    assert "doctor-secret-value" not in encoded
    assert "<redacted>" in encoded


def test_diagnostics_bundle_contains_safe_capability_and_metric_metadata() -> None:
    payload = build_diagnostics_bundle(
        created_at="2026-07-13T00:00:00+00:00",
        config_path=Path("/tmp/config.toml"),
        accepted={"generation": 3, "source_sha256": "a" * 64},
        active={"generation": 2, "resolved_sha256": "b" * 64},
        runtime={"status": "healthy", "token": "do-not-emit"},
        capabilities={
            "ok": False,
            "summary": {"passed": 2, "errors": 1},
            "checks": [
                {
                    "name": "gh_auth",
                    "ok": False,
                    "detail": "authorization=secret-value",
                    "remediation": "Run gh auth login",
                }
            ],
        },
        metrics={"operations": {"workspace_push": {"count": 2}}},
    )
    encoded = json.dumps(payload)
    assert "secret-value" not in encoded
    assert "do-not-emit" not in encoded
    assert payload["capabilities"]["checks"][0]["detail"] == "authorization=<redacted>"
    assert payload["metrics"]["operations"]["workspace_push"]["count"] == 2
    assert "configuration bodies" in payload["exclusions"]


def test_runtime_log_retention_keeps_only_configured_backups(tmp_path: Path) -> None:
    log = tmp_path / "runtime.log"
    client = TunnelCliClient("tunnel-client", log_max_bytes=10, log_backup_count=2)
    log.write_bytes(b"first-generation")
    client._rotate_log(log)
    assert log.with_suffix(".log.1").read_bytes() == b"first-generation"
    log.write_bytes(b"second-generation")
    client._rotate_log(log)
    assert log.with_suffix(".log.1").read_bytes() == b"second-generation"
    assert log.with_suffix(".log.2").read_bytes() == b"first-generation"
    log.write_bytes(b"third-generation")
    client._rotate_log(log)
    assert log.with_suffix(".log.1").read_bytes() == b"third-generation"
    assert log.with_suffix(".log.2").read_bytes() == b"second-generation"
    assert not log.with_suffix(".log.3").exists()


def test_live_tunnel_log_is_redacted_and_bounded_while_child_runs(tmp_path: Path) -> None:
    executable = tmp_path / "fake-tunnel-client"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "for index in range(80):\n"
        "    print(f'event={index} token={os.environ[\"CONTROL_PLANE_API_KEY\"]}', flush=True)\n"
        'print("q" * 100000, end="", flush=True)\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    log = tmp_path / "managed-runtime.log"
    client = TunnelCliClient(str(executable), log_max_bytes=100_000, log_backup_count=2)
    profile = TunnelProfile(
        "a" * 64, "profile", str(executable), "1.0", ("python", "-m", "repoforge")
    )
    child = client.start(
        profile,
        env={**os.environ, "CONTROL_PLANE_API_KEY": "super-secret-value"},
        log_path=log,
    )
    deadline = time.monotonic() + 5
    while client.is_alive(child) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not client.is_alive(child)

    paths = sorted(log.parent.glob("managed-runtime.log*"))
    assert 1 <= len(paths) <= 3
    assert all(path.stat().st_size <= 100_000 for path in paths)
    combined = "".join(path.read_text(encoding="utf-8") for path in paths)
    assert "super-secret-value" not in combined
    assert "q" * 100 not in combined
    assert "<redacted>" in combined
    assert "runtime log line omitted" in combined
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)


def test_mcp_error_boundary_returns_stable_redacted_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Service:
        def workspace_push(
            self, workspace_id: str, idempotency_key: str | None = None
        ) -> dict[str, Any]:
            del workspace_id, idempotency_key
            raise ConfigError(
                "COMMAND_TIMEOUT: authorization=top-secret bare-process-secret",
                unchanged_state=("Local branch and workspace files remain unchanged.",),
                safe_next_action="Retry with the same idempotency key.",
            )

    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "bare-process-secret")
    payload = _ServiceErrorBoundary(Service()).call(
        "workspace_push", "demo", idempotency_key="retry-key-0001"
    )
    assert payload["status"] == "failed"
    assert payload["error_code"] == "COMMAND_TIMEOUT"
    assert isinstance(payload["correlation_id"], str) and payload["correlation_id"]
    assert "top-secret" not in payload["what_happened"]
    assert "bare-process-secret" not in payload["what_happened"]
    assert payload["unchanged_state"] == ["Local branch and workspace files remain unchanged."]
    assert payload["automatic_retry_allowed"] is True


def test_phase6_server_limits_are_configurable_and_validated(tmp_path: Path) -> None:
    repo = tmp_path / "repo-config"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''[server]
workspace_root = "{tmp_path / "workspaces-config"}"
state_root = "{tmp_path / "state-config"}"
audit_max_bytes = 1234
audit_backup_count = 4
runtime_log_max_bytes = 2345
runtime_log_backup_count = 5
idempotency_stale_seconds = 321
idempotency_lock_timeout_seconds = 7

[repositories.demo]
path = "{repo}"
''',
        encoding="utf-8",
    )
    server = load_config(config_path).server
    assert (
        server.audit_max_bytes,
        server.audit_backup_count,
        server.runtime_log_max_bytes,
        server.runtime_log_backup_count,
        server.idempotency_stale_seconds,
        server.idempotency_lock_timeout_seconds,
    ) == (1234, 4, 2345, 5, 321, 7)

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "audit_backup_count = 4", "audit_backup_count = 0"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="audit_backup_count"):
        load_config(config_path)


def test_cross_process_idempotency_executes_external_effect_once(tmp_path: Path) -> None:
    worker = tmp_path / "worker.py"
    state_root = tmp_path / "shared-state"
    counter = tmp_path / "counter.txt"
    repo = tmp_path / "shared-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    worker_source = f"""from pathlib import Path
import time
from repoforge.bootstrap import build_application
from repoforge.config import AppConfig, RepositoryConfig, ServerConfig

root = Path({str(tmp_path)!r})
state = Path({str(state_root)!r})
repo = Path({str(repo)!r})
counter = Path({str(counter)!r})
config = AppConfig(
    root / "config.toml",
    ServerConfig(
        root / "workspaces",
        state,
        idempotency_stale_seconds=60,
        idempotency_lock_timeout_seconds=5,
    ),
    {{"demo": RepositoryConfig("demo", repo, fetch_before_workspace=False)}},
)
ctx = build_application(config).context

def effect():
    with counter.open("a", encoding="utf-8") as handle:
        handle.write("effect\\n")
        handle.flush()
    time.sleep(0.8)
    return {{"ok": True}}

result = ctx.idempotent(
    "workspace_push",
    "cross-process-key-0001",
    {{"workspace_id": "demo"}},
    effect,
    details={{"repo_id": "demo"}},
)
assert result == {{"ok": True}}
"""
    worker.write_text(worker_source, encoding="utf-8")
    first = subprocess.Popen([sys.executable, str(worker)])
    second: subprocess.Popen[bytes] | None = None
    try:
        time.sleep(0.15)
        second = subprocess.Popen([sys.executable, str(worker)])
        assert first.wait(timeout=10) == 0
        assert second.wait(timeout=10) == 0
    finally:
        if first.poll() is None:
            first.kill()
        if second is not None and second.poll() is None:
            second.kill()
    assert counter.read_text(encoding="utf-8").splitlines() == ["effect"]


def test_keyed_result_is_safely_identical_on_first_call_and_replay(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    request = {"workspace_id": "demo", "body": "private request body"}

    first = ctx.idempotent(
        "workspace_update_draft_pr",
        "safe-result-key-0001",
        request,
        lambda: {"payload": {"title": "Safe", "body": "private PR body", "token": "secret"}},
    )
    second = ctx.idempotent(
        "workspace_update_draft_pr",
        "safe-result-key-0001",
        request,
        lambda: {"unreachable": True},
    )

    assert first == second
    encoded = json.dumps(first)
    assert "private PR body" not in encoded
    assert first["payload"]["body_omitted"] is True
    assert first["payload"]["token"] == "<redacted>"


def test_idempotency_receipt_omits_pr_body_and_raw_key(tmp_path: Path) -> None:
    store = JsonIdempotencyStore(tmp_path)
    raw_key = "sensitive-request-key-0001"
    key_hash = hash_idempotency_key(raw_key)
    store.save(
        IdempotencyRecord(
            "workspace_update_draft_pr",
            key_hash,
            request_fingerprint({"workspace_id": "demo", "body": "private body"}),
            IdempotencyState.COMPLETED,
            "2026-07-13T00:00:00+00:00",
            1.0,
            "correlation",
            {"payload": {"title": "Safe", "body": "private PR body", "token": "secret"}},
        )
    )
    raw = next(store.root.glob("*.json")).read_text(encoding="utf-8")
    assert raw_key not in raw
    assert "private PR body" not in raw
    assert '"body_omitted": true' in raw
    assert '"token": "<redacted>"' in raw
