"""Coverage for the cursor-based foreman audit reads (#210)."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.adapters.audit.jsonl import JsonlAuditSink
from repoforge.adapters.audit.query import prune_audit_log, read_audit_events_since
from repoforge.domain.errors import ConfigError

cli = importlib.import_module("repoforge.interfaces.cli.main")


class _AdvancingClock:
    """Ticks forward one second per call so prune-by-timestamp tests can select a prefix."""

    def __init__(self) -> None:
        self._second = 0

    def now_iso(self) -> str:
        self._second += 1
        return f"2026-07-18T00:00:{self._second:02d}+00:00"


def _sink(tmp_path: Path, clock: _AdvancingClock | None = None) -> JsonlAuditSink:
    return JsonlAuditSink(tmp_path, clock=clock or _AdvancingClock())


def test_seq_is_monotonic_and_survives_a_fresh_sink_instance(tmp_path: Path) -> None:
    first = _sink(tmp_path)
    first.record("a", success=True, details={})
    first.record("b", success=True, details={})

    # A new instance backed by the same file (e.g. a fresh CLI invocation) must continue the
    # sequence rather than resetting to zero.
    second = _sink(tmp_path)
    second.record("c", success=True, details={})

    page = read_audit_events_since(tmp_path / "audit.jsonl", cursor=0)
    assert [event["action"] for event in page.events] == ["a", "b", "c"]
    assert [event["seq"] for event in page.events] == [1, 2, 3]


def test_cursor_returns_only_events_after_the_cursor(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    for name in ("a", "b", "c"):
        sink.record(name, success=True, details={})

    page = read_audit_events_since(tmp_path / "audit.jsonl", cursor=1)
    assert [event["action"] for event in page.events] == ["b", "c"]
    assert page.next_cursor == 3
    assert page.status == "ok"


def test_two_reads_around_a_prune_lose_nothing_and_duplicate_nothing(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    for name in ("a", "b", "c", "d"):
        sink.record(name, success=True, details={})
    path = tmp_path / "audit.jsonl"

    first_page = read_audit_events_since(path, cursor=0)
    assert [e["action"] for e in first_page.events] == ["a", "b", "c", "d"]

    # a/b are timestamped :01/:02, c/d are :03/:04 -- this cutoff removes exactly a/b.
    prune_audit_log(path, before="2026-07-18T00:00:03+00:00")
    sink2 = _sink(tmp_path)
    sink2.record("e", success=True, details={})

    second_page = read_audit_events_since(path, cursor=first_page.next_cursor)
    assert second_page.status == "ok"
    assert [e["action"] for e in second_page.events] == ["e"]
    # No duplicates and nothing lost across the two pages combined.
    all_actions = [e["action"] for e in first_page.events] + [
        e["action"] for e in second_page.events
    ]
    assert all_actions == ["a", "b", "c", "d", "e"]


def test_cursor_older_than_the_watermark_reports_cursor_gap(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    for name in ("a", "b", "c", "d", "e"):
        sink.record(name, success=True, details={})
    path = tmp_path / "audit.jsonl"

    # a/b/c are timestamped :01/:02/:03; this cutoff removes exactly those three, leaving
    # d (:04, seq 4) and e (:05, seq 5); watermark becomes seq 4.
    prune_audit_log(path, before="2026-07-18T00:00:04+00:00")

    page = read_audit_events_since(path, cursor=1)  # a cursor from before the prune
    assert page.status == "cursor_gap"
    assert page.watermark == 4
    assert page.events == ()


def test_cursor_at_or_after_the_watermark_boundary_is_not_a_gap(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    for name in ("a", "b", "c", "d"):
        sink.record(name, success=True, details={})
    path = tmp_path / "audit.jsonl"
    # a/b/c at :01/:02/:03 are pruned; only d (:04, seq 4) survives.
    prune_audit_log(path, before="2026-07-18T00:00:04+00:00")

    # watermark is seq 4 ("d"); a cursor of exactly watermark-1 is the correct handoff point.
    page = read_audit_events_since(path, cursor=3)
    assert page.status == "ok"
    assert [e["action"] for e in page.events] == ["d"]


def test_replaying_the_same_cursor_after_a_crash_reproduces_the_same_events(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    for name in ("a", "b"):
        sink.record(name, success=True, details={})
    path = tmp_path / "audit.jsonl"

    # Simulates: consumer read cursor=0, acted, crashed before persisting next_cursor. On
    # restart it replays from the same stale cursor -- delivery is at-least-once, and the
    # page returned is byte-identical, so idempotent handling on the consumer side absorbs it.
    first = read_audit_events_since(path, cursor=0)
    second = read_audit_events_since(path, cursor=0)
    assert first == second


def test_empty_log_returns_an_empty_page_not_an_error(tmp_path: Path) -> None:
    page = read_audit_events_since(tmp_path / "audit.jsonl", cursor=0)
    assert page.status == "ok"
    assert page.events == ()
    assert page.next_cursor == 0


def test_negative_cursor_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        read_audit_events_since(tmp_path / "audit.jsonl", cursor=-1)


def test_cli_cursor_flag_returns_a_cursor_page(
    forge_env: ForgeEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    forge_env.service.repo_list()
    forge_env.service.repo_list()
    capsys.readouterr()

    assert cli.main(["--config", str(forge_env.config_path), "audit", "--cursor", "0"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert len(payload["events"]) == 1
    assert payload["events"][0]["action"] == "repo_list"
    assert payload["next_cursor"] == 1

    assert (
        cli.main(
            [
                "--config",
                str(forge_env.config_path),
                "audit",
                "--cursor",
                str(payload["next_cursor"]),
            ]
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)
    assert second["events"] == []
    assert second["next_cursor"] == payload["next_cursor"]


def test_cursor_flag_does_not_create_an_argparse_prefix_ambiguity_with_stats_since(
    forge_env: ForgeEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression: `--cursor` must never share a prefix with `audit stats --since` (a distinct,
    # unrelated date-bound flag) -- argparse's abbreviation matching treats any option string
    # that is a strict prefix of exactly one other option as ambiguous, and previously named
    # `--since-cursor`/`--since-cursor-limit` collided with `--since` on Python's argparse in a
    # way that only failed on 3.10/3.11 in CI, not locally on 3.13.
    assert (
        cli.main(
            ["--config", str(forge_env.config_path), "audit", "stats", "--since", "2026-01-01"]
        )
        == 0
    )
