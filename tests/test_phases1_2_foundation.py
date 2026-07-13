from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from repoforge.adapters.configuration import ConfigGenerationStore
from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.runtime import InProcessOperationGate
from repoforge.domain.config_generation import (
    ApprovalEvent,
    CapabilityDeltaKind,
    ConfigMutation,
    classify_capability_delta,
    sha256_text,
)
from repoforge.domain.errors import ConfigError
from repoforge.testing import CleanupTracker, FailureInjector, SequenceIdGenerator


def _resolved(*repo_ids: str, max_files: int = 150, denied: tuple[str, ...] = (".git",)) -> str:
    lines = [
        "[server]",
        'workspace_root = "/tmp/workspaces"',
        'state_root = "/tmp/state"',
        'allowed_environment = ["HOME"]',
    ]
    for repo_id in repo_ids:
        lines.extend(
            [
                "",
                f"[repositories.{repo_id}]",
                f'path = "/tmp/{repo_id}"',
                'remote = "origin"',
                'default_base = "main"',
                'allowed_base_branches = ["main"]',
                'protected_branches = ["main"]',
                f"max_changed_files = {max_files}",
                "max_diff_lines = 12000",
                "max_total_changed_bytes = 1000000",
                "require_verification_before_commit = true",
                "no_maintainer_edit = false",
                "denied_paths = [" + ", ".join(json.dumps(item) for item in denied) + "]",
                "",
                f"[repositories.{repo_id}.profiles.full]",
                'description = "full"',
                "verification = true",
                'commands = [["python", "-m", "pytest"]]',
            ]
        )
    return "\n".join(lines) + "\n"


def _approval(proposal_id: str, at: str = "2026-07-13T00:00:00+00:00") -> ApprovalEvent:
    return ApprovalEvent("tester", at, proposal_id, sha256_text("approved"))


def _lock_holder(
    root: str,
    started: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    manager = FcntlLockManager(Path(root))
    with manager.lock("shared", timeout_seconds=2):
        started.set()
        release.wait(5)


def test_fcntl_lock_manager_rejects_competing_process(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    release = context.Event()
    process = context.Process(target=_lock_holder, args=(str(tmp_path), started, release))
    process.start()
    assert started.wait(3)
    with (
        pytest.raises(ConfigError, match="LOCK_TIMEOUT"),
        FcntlLockManager(tmp_path).lock("shared", timeout_seconds=0.1),
    ):
        pass
    release.set()
    process.join(5)
    assert process.exitcode == 0


def test_operation_gate_drains_writes_and_reopens() -> None:
    gate = InProcessOperationGate()
    with gate.operation("read", mutating=False):
        gate.begin_drain(reason="reload", correlation_id="abc")
        with (
            pytest.raises(ConfigError, match="RUNTIME_RELOADING"),
            gate.operation("new-read", mutating=False),
        ):
            pass
        with (
            pytest.raises(ConfigError, match="RUNTIME_RELOADING"),
            gate.operation("write", mutating=True),
        ):
            pass
    assert gate.wait_for_idle(0.1)
    gate.reopen()
    with gate.operation("write", mutating=True):
        assert gate.snapshot()["active_writes"] == 1


def test_semantic_delta_understands_permissions_and_budgets() -> None:
    base = _resolved("a", max_files=100, denied=(".git", ".env"))
    widened = _resolved("a", "b", max_files=200, denied=(".git",))
    delta = classify_capability_delta(base, widened)
    assert delta.kind is CapabilityDeltaKind.EXPANSION
    assert any(change.path == "repositories" for change in delta.changes)
    tightened = _resolved("a", max_files=50, denied=(".git", ".env", "*.pem"))
    assert classify_capability_delta(base, tightened).kind is CapabilityDeltaKind.RESTRICTION
    mixed = _resolved("a", "b", max_files=50, denied=(".git", ".env"))
    assert classify_capability_delta(base, mixed).kind is CapabilityDeltaKind.INCOMPATIBLE


def test_config_generations_are_immutable_noop_and_stale_safe(tmp_path: Path) -> None:
    source_path = tmp_path / "config.toml"
    source = 'version = 2\n[tunnel]\nid = "tunnel"\n[[repo]]\nid = "a"\npath = "/tmp/a"\n'
    source_path.write_text(source, encoding="utf-8")
    store = ConfigGenerationStore(
        source_path, tmp_path / "state", FcntlLockManager(tmp_path / "locks")
    )
    proposal_id = "p1"
    first = store.accept(
        ConfigMutation(
            source,
            _resolved("a"),
            (("a", "f" * 64),),
            "initial enrollment",
            "2026-07-13T00:00:00+00:00",
            0,
            sha256_text(source),
            proposal_id,
            _approval(proposal_id),
        )
    )
    assert first.generation == 1
    store.stage_activation(1, expected_active=None)
    assert store.activate(1, expected_active=None).active
    no_op = store.accept(
        ConfigMutation(
            source,
            _resolved("a"),
            (("a", "f" * 64),),
            "refresh",
            "2026-07-13T01:00:00+00:00",
            1,
            first.source_sha256,
        )
    )
    assert no_op.generation == 1
    with pytest.raises(ConfigError, match="STALE_CONFIG_GENERATION"):
        store.accept(
            ConfigMutation(
                source,
                _resolved("a"),
                (("a", "f" * 64),),
                "stale",
                "2026-07-13T02:00:00+00:00",
                0,
                first.source_sha256,
            )
        )
    assert len(store.history()) == 1
    assert (store.generations / "1" / "resolved.toml").is_file()


def test_restrictive_generation_and_rollback_expansion_require_approval(tmp_path: Path) -> None:
    source_path = tmp_path / "config.toml"
    source1 = 'version = 2\n[tunnel]\nid = "tunnel"\n[[repo]]\nid = "a"\npath = "/tmp/a"\n[[repo]]\nid = "b"\npath = "/tmp/b"\n'
    source_path.write_text(source1, encoding="utf-8")
    store = ConfigGenerationStore(
        source_path, tmp_path / "state", FcntlLockManager(tmp_path / "locks")
    )
    first = store.accept(
        ConfigMutation(
            source1,
            _resolved("a", "b"),
            (("a", "a" * 64), ("b", "b" * 64)),
            "initial",
            "2026-07-13T00:00:00+00:00",
            0,
            sha256_text(source1),
            "p1",
            _approval("p1"),
        )
    )
    store.stage_activation(first.generation, expected_active=None)
    store.activate(first.generation, expected_active=None)
    source2 = 'version = 2\n[tunnel]\nid = "tunnel"\n[[repo]]\nid = "a"\npath = "/tmp/a"\n'
    second = store.accept(
        ConfigMutation(
            source2,
            _resolved("a"),
            (("a", "a" * 64),),
            "remove b",
            "2026-07-13T01:00:00+00:00",
            1,
            first.source_sha256,
        )
    )
    assert second.delta is CapabilityDeltaKind.RESTRICTION
    store.stage_activation(second.generation, expected_active=1)
    store.activate(second.generation, expected_active=1)
    with pytest.raises(ConfigError, match="ROLLBACK_APPROVAL_REQUIRED"):
        store.rollback(1, expected_active=2)
    token = f"rollback:1:{first.resolved_sha256[:16]}"
    restored = store.rollback(1, expected_active=2, approval_token=token)
    assert restored.generation == 1 and not restored.active
    assert store.active() is not None and store.active().generation == 2
    store.stage_activation(restored.generation, expected_active=2)
    activated = store.activate(restored.generation, expected_active=2)
    assert activated.active and activated.generation == 1


def test_generation_corruption_is_detected(tmp_path: Path) -> None:
    source_path = tmp_path / "config.toml"
    source = 'version = 2\n[tunnel]\nid = "tunnel"\n[[repo]]\nid = "a"\npath = "/tmp/a"\n'
    source_path.write_text(source, encoding="utf-8")
    store = ConfigGenerationStore(
        source_path, tmp_path / "state", FcntlLockManager(tmp_path / "locks")
    )
    store.accept(
        ConfigMutation(
            source,
            _resolved("a"),
            (("a", "a" * 64),),
            "initial",
            "2026-07-13T00:00:00+00:00",
            0,
            sha256_text(source),
            "p",
            _approval("p"),
        )
    )
    (store.generations / "1" / "resolved.toml").write_text("tampered", encoding="utf-8")
    with pytest.raises(ConfigError, match="Resolved hash mismatch"):
        store.current()


def test_reusable_harness_primitives_are_deterministic() -> None:
    ids = SequenceIdGenerator(("abcdef", "123456"))
    assert ids.new_hex(4) == "abcd"
    assert ids.new_hex(4) == "1234"
    injector = FailureInjector()
    injector.fail_next("save", OSError("boom"))
    with pytest.raises(OSError, match="boom"):
        injector.hit("save")
    CleanupTracker().assert_clean()
