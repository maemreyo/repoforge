"""Startup compatibility with a state root written by an earlier release.

RepoForge keeps durable state across upgrades, but every other test begins with
an empty state root, so a record written by a previous release never exists in
CI. That gap shipped a release that could not start at all: raising
PR_CHECK_WATCH_SCHEMA_VERSION to 2 left already-stored v1 records undecodable,
`list_records` raised on the first one, and startup reaches it through
`resume_active()` -> `build_application()`. Ten CI checks were green, including
`production-gate` and `live-activation`, because both also start from nothing.

These tests boot against a populated state root instead. The rule they encode:
**no durable store may make a single unreadable record fatal to process
startup.** A scan reports what it cannot decode; a direct read of one exact
record stays strict.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment

from repoforge.application.service import CodingService
from repoforge.bootstrap import build_application
from repoforge.config import load_config

#: Durable stores that `build_application` sweeps at startup, with a filename the
#: store's own id validator accepts. The filename must be valid so that each case
#: proves the *record body* is survivable, rather than only proving that a stray
#: file with a foreign name is ignored.
STARTUP_SWEPT_STORES: tuple[tuple[str, str], ...] = (
    ("operations", "op-" + "0" * 24),
    ("operation-work-v1", "op-" + "1" * 24),
    ("pr-check-watches", "op-" + "2" * 24),
    ("effect-receipts", "receipt-" + "3" * 24),
    ("runtime-activations", "receipt-" + "4" * 24),
    ("operation-workers", "op-" + "5" * 24),
    ("operation-results", "op-" + "6" * 24),
)

#: A record shaped like something an older release wrote: a schema version this
#: release does not know, and none of the fields it since made required.
LEGACY_RECORD = {"schema_version": 1, "state": "running"}


def _seed(env: ForgeEnvironment, store_dir: str, record_id: str, payload: dict) -> Path:
    root = load_config(env.config_path).server.state_root / store_dir
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{record_id}.json"
    path.write_text(json.dumps({**payload, "operation_id": record_id}), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("store_dir", "record_id"),
    STARTUP_SWEPT_STORES,
    ids=[item[0] for item in STARTUP_SWEPT_STORES],
)
def test_startup_survives_one_unreadable_record(
    tmp_path: Path, store_dir: str, record_id: str
) -> None:
    """One record this release cannot decode must not stop the runtime from starting.

    Failing here means an upgrade will brick every process on any installation
    that already holds such a record -- MCP, the execution worker and the CLI
    alike, since all three go through `build_application`.
    """
    env = create_forge_environment(tmp_path)
    _seed(env, store_dir, record_id, LEGACY_RECORD)

    application = build_application(load_config(env.config_path))

    assert application.context is not None


def test_startup_survives_a_populated_previous_release_state_root(tmp_path: Path) -> None:
    """The realistic case: every swept store holds legacy records at once."""
    env = create_forge_environment(tmp_path)
    for index, (store_dir, record_id) in enumerate(STARTUP_SWEPT_STORES):
        _seed(env, store_dir, record_id, LEGACY_RECORD)
        # More than one per store, so a store that happens to stop after the first
        # bad file is not mistaken for one that tolerates them.
        _seed(env, store_dir, record_id[:-1] + f"{index}", LEGACY_RECORD)

    service = CodingService(load_config(env.config_path))

    # Control-plane reads that actually scan the seeded stores. Deliberately not
    # `doctor()["ok"]`: that aggregates host tooling checks -- git, gh, node --
    # and would make this assert whatever the runner happens to have installed
    # rather than whether legacy records are survivable.
    assert service.operation_list()["operations"] == []
    assert service.repo_list()["repositories"]

    # The records are skipped, not deleted. Removing durable state a caller never
    # asked to remove would be a worse failure than refusing to read it.
    for store_dir, record_id in STARTUP_SWEPT_STORES:
        root = load_config(env.config_path).server.state_root / store_dir
        assert (root / f"{record_id}.json").exists()


def test_the_gate_fails_when_a_swept_store_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove the gate has teeth: it must fail when a store regresses.

    A gate that only ever passes is indistinguishable from one that tests
    nothing. Reintroducing the exact #297 defect -- a scan that raises instead of
    reporting -- has to be caught here.
    """
    from repoforge.adapters.persistence.json_pr_check_watch_store import (
        JsonPrCheckWatchStore,
    )
    from repoforge.domain.errors import ErrorCode, RepoForgeError

    def fatal_scan(self: object, *, max_records: int) -> object:
        del self, max_records
        raise RepoForgeError(
            "Unsupported PR check watch schema version: 1",
            code=ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT,
        )

    # The environment is built first: creating it constructs a service, which
    # itself calls build_application, and the regression must be observed in the
    # assertion below rather than while the fixture is still being set up.
    env = create_forge_environment(tmp_path)
    _seed(env, "pr-check-watches", "op-" + "7" * 24, LEGACY_RECORD)
    monkeypatch.setattr(JsonPrCheckWatchStore, "list_records", fatal_scan)

    with pytest.raises(RepoForgeError) as regression:
        build_application(load_config(env.config_path))

    assert regression.value.code is ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT


def test_a_record_written_after_a_restart_survives_the_round_trip(tmp_path: Path) -> None:
    """Write, read back, and get a usable record -- the layer that took the runtime down.

    `restarts_total` and `last_restart_at` were added to the dataclass and to the
    supervisor's writer, but not to the decoder. Every read therefore defaulted
    `restarts_total` to 0 while `restart_count` came back as written, and an invariant
    requiring the first to be at least the second then rejected the record. The runtime
    refused to start, `rf` itself ran the same broken decoder, and no release could be
    rolled back to because they all carried it.

    Nothing caught it: the tests built `RuntimeRecord` directly, and the upgrade gate seeds
    records that cannot be DECODED -- this one decoded into an invalid object instead.
    """
    from repoforge.bootstrap import build_runtime_store
    from repoforge.domain.runtime import RuntimePhase, RuntimeRecord

    store = build_runtime_store(tmp_path / "runtime.json")
    written = RuntimeRecord(
        protocol_version=1,
        phase=RuntimePhase.DEGRADED,
        pid=None,
        process_identity=None,
        active_generation=None,
        accepted_generation=4,
        tunnel_profile="repoforge",
        tunnel_profile_fingerprint="b" * 64,
        tool_surface_hash="c" * 64,
        started_at=None,
        updated_at="2026-07-29T09:26:42+00:00",
        correlation_id="d" * 24,
        restart_count=1,
        restarts_total=3,
        last_restart_at="2026-07-29T09:26:21+00:00",
    )
    store.write(written)

    read_back = store.read()

    assert read_back is not None
    assert read_back.restart_count == 1
    assert read_back.restarts_total == 3
    assert read_back.last_restart_at == "2026-07-29T09:26:21+00:00"


def test_a_record_predating_the_restart_evidence_fields_still_decodes(tmp_path: Path) -> None:
    """An upgrade must not be able to make an existing installation unstartable.

    A record written before these fields existed has no `restarts_total`, so it decodes as
    0 next to whatever `restart_count` it carried. That is honest -- nothing was counting
    then -- and refusing it strands the runtime with no release to roll back to.
    """
    import json

    from repoforge.bootstrap import build_runtime_store

    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "phase": "degraded",
                "pid": None,
                "process_identity": None,
                "active_generation": None,
                "accepted_generation": 4,
                "tunnel_profile": "repoforge",
                "tunnel_profile_fingerprint": "b" * 64,
                "tool_surface_hash": "c" * 64,
                "started_at": None,
                "updated_at": "2026-07-29T09:26:42+00:00",
                "correlation_id": "d" * 24,
                "restart_count": 2,
            }
        ),
        encoding="utf-8",
    )

    record = build_runtime_store(path).read()

    assert record is not None
    assert record.restart_count == 2
    assert record.restarts_total == 0
