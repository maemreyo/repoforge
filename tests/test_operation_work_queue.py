"""Durable operation work-envelope and queue behavior."""

from __future__ import annotations


def test_new_work_item_is_queued_and_exact_state_bound() -> None:
    """Catch work admission that loses exact-state identity or starts as claimed."""
    from repoforge.domain.operation_work import (
        OperationWorkRequest,
        OperationWorkState,
        new_work_item,
    )

    item = new_work_item(
        operation_id="op-" + "a" * 24,
        request=OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="full",
            expected_head_sha="b" * 40,
            expected_fingerprint="c" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )

    assert item.state is OperationWorkState.QUEUED
    assert item.attempt == 0
    assert item.owner_id is None
    assert item.lease_expires_at is None
    assert item.request.expected_head_sha == "b" * 40
    assert item.request.expected_fingerprint == "c" * 64
    assert item.request.config_generation == 12


def test_claim_increments_attempt_and_started_work_cannot_requeue() -> None:
    """Catch duplicate execution caused by requeueing an already-started child."""
    from dataclasses import replace

    import pytest

    from repoforge.domain.errors import RepoForgeError
    from repoforge.domain.operation_work import (
        OperationWorkRequest,
        OperationWorkState,
        claim_work_item,
        new_work_item,
        requeue_unstarted_work,
    )

    item = new_work_item(
        operation_id="op-" + "d" * 24,
        request=OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="full",
            expected_head_sha="e" * 40,
            expected_fingerprint="f" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    claimed = claim_work_item(
        item,
        owner_id="worker-1",
        lease_expires_at="2026-07-27T00:01:30+00:00",
        now="2026-07-27T00:00:01+00:00",
    )

    assert claimed.state is OperationWorkState.CLAIMED
    assert claimed.attempt == 1
    assert claimed.owner_id == "worker-1"
    with pytest.raises(RepoForgeError, match="started work cannot be requeued"):
        requeue_unstarted_work(
            replace(claimed, child_started=True),
            now="2026-07-27T00:02:00+00:00",
        )


def test_work_item_payload_round_trips_exact_schema() -> None:
    """Catch persistence that drops execution identity or accepts schema drift."""
    from repoforge.domain.operation_work import (
        OperationWorkRequest,
        new_work_item,
        work_item_from_payload,
        work_item_payload,
    )

    item = new_work_item(
        operation_id="op-" + "1" * 24,
        request=OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="full",
            expected_head_sha="2" * 40,
            expected_fingerprint="3" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )

    payload = work_item_payload(item)

    assert payload == {
        "operation_id": "op-" + "1" * 24,
        "request": {
            "kind": "profile",
            "workspace_id": "workspace-1",
            "profile_name": "full",
            "expected_head_sha": "2" * 40,
            "expected_fingerprint": "3" * 64,
            "config_generation": 12,
        },
        "state": "queued",
        "attempt": 0,
        "owner_id": None,
        "lease_expires_at": None,
        "child_started": False,
        "created_at": "2026-07-27T00:00:00+00:00",
        "updated_at": "2026-07-27T00:00:00+00:00",
        "schema_version": 1,
    }
    assert work_item_from_payload(payload) == item


def test_work_item_payload_rejects_schema_drift() -> None:
    """Catch readers that silently accept future or unknown persisted fields."""
    import pytest

    from repoforge.domain.errors import RepoForgeError
    from repoforge.domain.operation_work import (
        OperationWorkRequest,
        new_work_item,
        work_item_from_payload,
        work_item_payload,
    )

    item = new_work_item(
        operation_id="op-" + "4" * 24,
        request=OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="full",
            expected_head_sha="5" * 40,
            expected_fingerprint="6" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    future = work_item_payload(item)
    future["schema_version"] = 2
    unknown = work_item_payload(item)
    unknown["unexpected"] = True

    with pytest.raises(RepoForgeError, match="schema"):
        work_item_from_payload(future)
    with pytest.raises(RepoForgeError, match="schema"):
        work_item_from_payload(unknown)


def test_new_work_item_rejects_non_exact_fingerprint() -> None:
    """Catch durable admission that permits work against an ambiguous tree identity."""
    import pytest

    from repoforge.domain.errors import RepoForgeError
    from repoforge.domain.operation_work import OperationWorkRequest, new_work_item

    request = OperationWorkRequest.profile(
        workspace_id="workspace-1",
        profile_name="full",
        expected_head_sha="7" * 40,
        expected_fingerprint="8" * 63,
        config_generation=12,
    )

    with pytest.raises(RepoForgeError, match="expected_fingerprint"):
        new_work_item(
            operation_id="op-" + "9" * 24,
            request=request,
            now="2026-07-27T00:00:00+00:00",
        )


def test_json_work_queue_survives_restart(tmp_path) -> None:
    """Catch queue persistence that exists only in process memory."""
    from repoforge.adapters.locking import FcntlLockManager
    from repoforge.adapters.persistence.json_operation_work_queue import (
        JsonOperationWorkQueue,
    )
    from repoforge.domain.operation_work import OperationWorkRequest, new_work_item

    state_root = tmp_path / "state"
    locks = FcntlLockManager(state_root / "locks")
    queue = JsonOperationWorkQueue(state_root, locks)
    item = new_work_item(
        operation_id="op-" + "a" * 24,
        request=OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="full",
            expected_head_sha="b" * 40,
            expected_fingerprint="c" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )

    queue.create(item)

    restarted = JsonOperationWorkQueue(state_root, locks)
    assert restarted.read(item.operation_id) == item
    assert (restarted.root / f"{item.operation_id}.json").stat().st_mode & 0o777 == 0o600


def test_json_work_queue_save_rejects_stale_updated_at(tmp_path) -> None:
    """Catch a stale worker overwriting a newer durable claim."""
    import pytest

    from repoforge.adapters.locking import FcntlLockManager
    from repoforge.adapters.persistence.json_operation_work_queue import (
        JsonOperationWorkQueue,
    )
    from repoforge.domain.errors import RepoForgeError
    from repoforge.domain.operation_work import (
        OperationWorkRequest,
        claim_work_item,
        new_work_item,
    )

    state_root = tmp_path / "state"
    queue = JsonOperationWorkQueue(
        state_root,
        FcntlLockManager(state_root / "locks"),
    )
    item = new_work_item(
        operation_id="op-" + "d" * 24,
        request=OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="full",
            expected_head_sha="e" * 40,
            expected_fingerprint="f" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    queue.create(item)
    claimed = claim_work_item(
        item,
        owner_id="worker-1",
        lease_expires_at="2026-07-27T00:01:30+00:00",
        now="2026-07-27T00:00:01+00:00",
    )
    queue.save(claimed, expected_updated_at=item.updated_at)

    with pytest.raises(RepoForgeError, match="changed"):
        queue.save(item, expected_updated_at=item.updated_at)


def test_two_queue_instances_have_one_claim_winner(tmp_path) -> None:
    """Catch duplicate execution when concurrent workers race for one item."""
    from concurrent.futures import ThreadPoolExecutor

    from repoforge.adapters.locking import FcntlLockManager
    from repoforge.adapters.persistence.json_operation_work_queue import (
        JsonOperationWorkQueue,
    )
    from repoforge.domain.operation_work import OperationWorkRequest, new_work_item

    state_root = tmp_path / "state"
    locks = FcntlLockManager(state_root / "locks")
    first = JsonOperationWorkQueue(state_root, locks)
    second = JsonOperationWorkQueue(state_root, locks)
    item = new_work_item(
        operation_id="op-" + "1" * 24,
        request=OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="full",
            expected_head_sha="2" * 40,
            expected_fingerprint="3" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    first.create(item)

    def claim(queue_and_owner):
        queue, owner = queue_and_owner
        return queue.claim_next(
            owner_id=owner,
            now="2026-07-27T00:00:01+00:00",
            lease_expires_at="2026-07-27T00:01:31+00:00",
            compatible_kinds=frozenset({"profile"}),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ((first, "worker-1"), (second, "worker-2"))))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].attempt == 1
    assert winners[0].owner_id in {"worker-1", "worker-2"}


def test_json_work_queue_lists_bounded_records(tmp_path) -> None:
    """Catch recovery scans that cannot discover persisted queued work."""
    from repoforge.adapters.locking import FcntlLockManager
    from repoforge.adapters.persistence.json_operation_work_queue import (
        JsonOperationWorkQueue,
    )
    from repoforge.domain.operation_work import OperationWorkRequest, new_work_item

    state_root = tmp_path / "state"
    queue = JsonOperationWorkQueue(
        state_root,
        FcntlLockManager(state_root / "locks"),
    )
    item = new_work_item(
        operation_id="op-" + "4" * 24,
        request=OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="full",
            expected_head_sha="5" * 40,
            expected_fingerprint="6" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    queue.create(item)

    page = queue.list_records(max_records=10)

    assert page.records == (item,)
    assert page.scan_truncated is False


def test_json_work_queue_delete_removes_persisted_item(tmp_path) -> None:
    """Catch cancellation cleanup that leaves queued work claimable."""
    from repoforge.adapters.locking import FcntlLockManager
    from repoforge.adapters.persistence.json_operation_work_queue import (
        JsonOperationWorkQueue,
    )
    from repoforge.domain.operation_work import OperationWorkRequest, new_work_item

    state_root = tmp_path / "state"
    queue = JsonOperationWorkQueue(
        state_root,
        FcntlLockManager(state_root / "locks"),
    )
    item = new_work_item(
        operation_id="op-" + "7" * 24,
        request=OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="full",
            expected_head_sha="8" * 40,
            expected_fingerprint="9" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    queue.create(item)

    queue.delete(item.operation_id)

    assert queue.read(item.operation_id) is None


def test_adhoc_work_request_round_trips_exact_argv_and_policy() -> None:
    """Catch ad-hoc work that loses reviewed argv, cwd, or mutability during persistence."""
    from repoforge.domain.operation_work import (
        OperationWorkRequest,
        new_work_item,
        work_item_from_payload,
        work_item_payload,
    )

    item = new_work_item(
        operation_id="op-" + "a" * 24,
        request=OperationWorkRequest.adhoc(
            workspace_id="workspace-1",
            argv=("python3", "-c", "print('durable')"),
            working_directory="src",
            mutability="read_only",
            expected_head_sha="b" * 40,
            expected_fingerprint="c" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )

    payload = work_item_payload(item)

    assert payload["request"] == {
        "kind": "adhoc",
        "workspace_id": "workspace-1",
        "argv": ["python3", "-c", "print('durable')"],
        "script": None,
        "shell": None,
        "argv_sequence": None,
        "working_directory": "src",
        "mutability": "read_only",
        "stdin_text": None,
        "expected_head_sha": "b" * 40,
        "expected_fingerprint": "c" * 64,
        "config_generation": 12,
    }
    assert work_item_from_payload(payload) == item


def test_adhoc_work_request_round_trips_script_and_argv_sequence_forms() -> None:
    """#377/#443: the script and argv_sequence forms persist and decode exactly, same as
    the original argv form."""
    from repoforge.domain.operation_work import (
        OperationWorkRequest,
        new_work_item,
        work_item_from_payload,
        work_item_payload,
    )

    script_item = new_work_item(
        operation_id="op-" + "a" * 24,
        request=OperationWorkRequest.adhoc(
            workspace_id="workspace-1",
            script="echo hi",
            shell="sh",
            working_directory="src",
            mutability="read_only",
            expected_head_sha="b" * 40,
            expected_fingerprint="c" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    script_payload = work_item_payload(script_item)
    assert script_payload["request"]["script"] == "echo hi"
    assert script_payload["request"]["shell"] == "sh"
    assert script_payload["request"]["argv"] == []
    assert work_item_from_payload(script_payload) == script_item

    sequence_item = new_work_item(
        operation_id="op-" + "b" * 24,
        request=OperationWorkRequest.adhoc(
            workspace_id="workspace-1",
            argv_sequence=(("ruff", "check"), ("mypy", ".")),
            working_directory="src",
            mutability="read_only",
            expected_head_sha="b" * 40,
            expected_fingerprint="c" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    sequence_payload = work_item_payload(sequence_item)
    assert sequence_payload["request"]["argv_sequence"] == [["ruff", "check"], ["mypy", "."]]
    assert work_item_from_payload(sequence_payload) == sequence_item


def test_adhoc_work_queued_before_script_and_sequence_existed_still_decodes() -> None:
    """Same backward-compatibility guarantee as stdin_text's own precedent below: an
    in-flight item queued before #377/#443 added script/shell/argv_sequence must still
    decode after the release that adds them takes over."""
    from repoforge.domain.operation_work import (
        OperationWorkRequest,
        new_work_item,
        work_item_from_payload,
        work_item_payload,
    )

    item = new_work_item(
        operation_id="op-" + "a" * 24,
        request=OperationWorkRequest.adhoc(
            workspace_id="workspace-1",
            argv=("python3", "-c", "print('durable')"),
            working_directory="src",
            mutability="read_only",
            expected_head_sha="b" * 40,
            expected_fingerprint="c" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    legacy = work_item_payload(item)
    del legacy["request"]["script"]  # type: ignore[union-attr]
    del legacy["request"]["shell"]  # type: ignore[union-attr]
    del legacy["request"]["argv_sequence"]  # type: ignore[union-attr]

    assert work_item_from_payload(legacy) == item


def test_adhoc_work_queued_before_stdin_existed_still_decodes() -> None:
    """An activation must not strand work the previous release already queued.

    The decoder matches the request field set exactly, so adding `stdin_text` would
    have made every in-flight ad-hoc item undecodable the moment a new release took
    over. It is accepted as absent, and only as absent -- unknown fields still fail.
    """
    import pytest

    from repoforge.domain.errors import RepoForgeError
    from repoforge.domain.operation_work import (
        OperationWorkRequest,
        new_work_item,
        work_item_from_payload,
        work_item_payload,
    )

    item = new_work_item(
        operation_id="op-" + "a" * 24,
        request=OperationWorkRequest.adhoc(
            workspace_id="workspace-1",
            argv=("python3", "-c", "print('durable')"),
            working_directory="src",
            mutability="read_only",
            expected_head_sha="b" * 40,
            expected_fingerprint="c" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    legacy = work_item_payload(item)
    del legacy["request"]["stdin_text"]  # type: ignore[union-attr]

    assert work_item_from_payload(legacy) == item

    unknown = work_item_payload(item)
    unknown["request"]["unexpected"] = "value"  # type: ignore[index]
    with pytest.raises(RepoForgeError):
        work_item_from_payload(unknown)


def test_queue_claim_is_fenced_to_exact_config_generation(tmp_path) -> None:
    """An old worker must not execute work admitted for a newer config generation."""
    from repoforge.adapters.locking import FcntlLockManager
    from repoforge.adapters.persistence.json_operation_work_queue import JsonOperationWorkQueue
    from repoforge.domain.operation_work import OperationWorkRequest, new_work_item

    state_root = tmp_path / "state"
    queue = JsonOperationWorkQueue(state_root, FcntlLockManager(state_root / "locks"))
    item = new_work_item(
        operation_id="op-" + "b" * 24,
        request=OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="full",
            expected_head_sha="c" * 40,
            expected_fingerprint="d" * 64,
            config_generation=13,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    queue.create(item)

    claimed = queue.claim_next(
        owner_id="worker-generation-12",
        now="2026-07-27T00:00:01+00:00",
        lease_expires_at="2026-07-27T00:01:31+00:00",
        compatible_kinds=frozenset({"profile"}),
        config_generation=12,
    )

    assert claimed is None
    assert queue.read(item.operation_id) == item


def test_stale_owner_cannot_mark_a_new_attempt_as_started() -> None:
    """An expired worker must fail before spawn after another worker takes over."""
    import pytest

    from repoforge.domain.errors import RepoForgeError
    from repoforge.domain.operation_work import (
        OperationWorkRequest,
        claim_work_item,
        mark_work_child_started,
        new_work_item,
        requeue_unstarted_work,
    )

    queued = new_work_item(
        operation_id="op-" + "c" * 24,
        request=OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="d" * 40,
            expected_fingerprint="e" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    attempt_one = claim_work_item(
        queued,
        owner_id="worker-a",
        lease_expires_at="2026-07-27T00:01:31+00:00",
        now="2026-07-27T00:00:01+00:00",
    )
    recovered = requeue_unstarted_work(attempt_one, now="2026-07-27T00:02:00+00:00")
    attempt_two = claim_work_item(
        recovered,
        owner_id="worker-b",
        lease_expires_at="2026-07-27T00:03:31+00:00",
        now="2026-07-27T00:02:01+00:00",
    )

    with pytest.raises(RepoForgeError, match="ownership changed"):
        mark_work_child_started(
            attempt_two,
            owner_id="worker-a",
            attempt=attempt_one.attempt,
            now="2026-07-27T00:02:02+00:00",
        )
