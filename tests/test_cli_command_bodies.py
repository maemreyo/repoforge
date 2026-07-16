from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from repoforge.application.configuration.source import SourceConfiguration, SourceRepository
from repoforge.domain.config_generation import (
    CapabilityChange,
    CapabilityDelta,
    CapabilityDeltaKind,
    ConfigGeneration,
)
from repoforge.domain.errors import ConfigError
from repoforge.domain.repository_proposal import (
    DetectionFinding,
    EnrollmentMode,
    ProposalConfidence,
    ProposedProfile,
    RepositoryPolicyProposal,
    RepositoryProposal,
    RequiredDecision,
)

cli = importlib.import_module("repoforge.interfaces.cli.main")


def _generation(number: int = 1, *, proposal_id: str | None = None) -> ConfigGeneration:
    return ConfigGeneration(
        number,
        "a" * 64,
        "b" * 64,
        (("demo", "c" * 64),),
        "2026-07-13T00:00:00+00:00",
        "test",
        proposal_id,
        None,
        CapabilityDeltaKind.EQUIVALENT,
        number - 1 or None,
        "corr",
        False,
    )


def _proposal(
    *,
    repo_id: str = "demo",
    proposal_id: str = "p" * 64,
    confidence: ProposalConfidence = ProposalConfidence.HIGH,
    decisions: tuple[RequiredDecision, ...] = (),
) -> RepositoryProposal:
    return RepositoryProposal(
        proposal_id,
        "f" * 64,
        repo_id,
        f"/repos/{repo_id}",
        confidence,
        (DetectionFinding("OK", "info", "detected"),),
        decisions,
        RepositoryPolicyProposal(
            EnrollmentMode.STANDARD,
            "origin",
            "main",
            ("main",),
            (),
            (".git",),
            (
                ProposedProfile(
                    "full",
                    "verify",
                    True,
                    (("python", "-m", "pytest"),),
                    ProposalConfidence.HIGH,
                    "pyproject.toml",
                ),
            ),
            True,
            100,
            1000,
            1_000_000,
        ),
    )


class FakeStore:
    def __init__(self, root: Path, *, current: ConfigGeneration | None = None) -> None:
        self.root = root
        self.source_path = root / "config.toml"
        self.active_resolved_path = root / "resolved.toml"
        self._current = current
        self.accepted: list[Any] = []
        self.source_text = "source-old"
        self.resolved_text = "resolved-old"

    def current(self) -> ConfigGeneration | None:
        return self._current

    def active(self) -> ConfigGeneration | None:
        return None

    def read_source_text(self) -> str:
        return self.source_text

    def read_resolved_text(self) -> str:
        return self.resolved_text

    def accept(self, mutation: Any) -> ConfigGeneration:
        self.accepted.append(mutation)
        number = (self._current.generation if self._current else 0) + 1
        result = _generation(number, proposal_id=getattr(mutation, "proposal_id", None))
        self._current = result
        return result

    def resolved_path(self, generation: int) -> Path:
        return self.root / "generations" / str(generation) / "resolved.toml"


class ProposalService:
    proposal = _proposal()
    facts: ClassVar[dict[str, object]] = {"repo_id": "demo", "root": "/repos/demo"}

    def __init__(self, probe: object) -> None:
        self.probe = probe

    def inspect(self, path: Path, *, repo_id: str | None = None) -> dict[str, object]:
        return {**self.facts, "repo_id": repo_id or self.facts["repo_id"], "root": str(path)}

    def propose(self, path: Path, **kwargs: object) -> RepositoryProposal:
        del path, kwargs
        return self.proposal

    def verify_approval(self, proposal: RepositoryProposal, token: str | None) -> str:
        if token != f"approve:{proposal.proposal_id}":
            raise ValueError("bad token")
        return "9" * 64


def _common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "RepositoryProposalService", ProposalService)
    monkeypatch.setattr(cli, "_probe", lambda: object())
    monkeypatch.setattr(cli, "system_clock", lambda: SimpleNamespace(now_iso=lambda: "now"))
    monkeypatch.setattr(cli, "id_generator", lambda: SimpleNamespace(new_hex=lambda n: "i" * n))
    monkeypatch.setattr(cli, "render_source", lambda source: "source-new")
    monkeypatch.setattr(cli, "parse_resolved", lambda text: {"repositories": {}})
    monkeypatch.setattr(cli, "apply_proposal", lambda document, proposal: document)
    monkeypatch.setattr(cli, "apply_ticket_graph", lambda document, repo_id, graph: document)
    monkeypatch.setattr(cli, "_render_candidate", lambda *args, **kwargs: "resolved-new")
    monkeypatch.setattr(cli, "_smoke_resolved", lambda *args: {"ok": True})
    monkeypatch.setattr(cli, "_state_root", lambda: Path("/state"))
    monkeypatch.setattr(cli, "_activate", lambda *args, **kwargs: {"activation": "ok"})
    monkeypatch.setattr(cli, "_activation_result", lambda *args: {"activation": "unchanged"})


def test_setup_preview_requirements_and_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _common(monkeypatch)
    store = FakeStore(tmp_path)
    monkeypatch.setattr(cli, "_store", lambda path: store)

    args = argparse.Namespace(
        config=str(tmp_path / "config.toml"),
        force=False,
        repos=["/repos/demo"],
        decision=[],
        policy_override=[],
        template="standard",
        approve=[],
        tunnel_id="tunnel",
        profile="repoforge",
        activate="auto",
        wait=True,
        rollback_on_failure=True,
    )
    assert cli._setup(args) == 3
    pending = json.loads(capsys.readouterr().out)
    assert pending["status"] == "pending_approval"
    assert pending["required_approval_tokens"] == [
        f"approve:{ProposalService.proposal.proposal_id}"
    ]

    args.approve = [f"approve:{ProposalService.proposal.proposal_id}"]
    assert cli._setup(args) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["status"] == "configured"
    assert store.accepted

    existing = tmp_path / "existing.toml"
    existing.write_text("old", encoding="utf-8")
    args.config = str(existing)
    with pytest.raises(ConfigError, match="already exists"):
        cli._setup(args)
    args.force = True
    forced_store = FakeStore(tmp_path / "forced")
    monkeypatch.setattr(cli, "_store", lambda path: forced_store)
    assert cli._setup(args) == 0
    assert list(tmp_path.glob("existing.toml.backup-*"))


def test_setup_reports_decisions_and_blocked_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _common(monkeypatch)
    decision = RequiredDecision("package_manager", "choose", ("pnpm", "npm"))
    ProposalService.proposal = _proposal(
        confidence=ProposalConfidence.BLOCKED, decisions=(decision,)
    )
    monkeypatch.setattr(cli, "_store", lambda path: FakeStore(tmp_path))
    args = argparse.Namespace(
        config=str(tmp_path / "config.toml"),
        force=False,
        repos=["/repos/demo"],
        decision=[],
        policy_override=[],
        template="standard",
        approve=[],
        tunnel_id="tunnel",
        profile="repoforge",
        activate="auto",
        wait=True,
        rollback_on_failure=True,
    )
    assert cli._setup(args) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "input_required"
    assert payload["blocked_repositories"] == ["demo"]
    ProposalService.proposal = _proposal()


def test_repo_inspect_and_propose_status_variants(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _common(monkeypatch)
    inspect_args = argparse.Namespace(path="/repo", repo_id="custom")
    assert cli._repo_inspect(inspect_args) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["facts"]["repo_id"] == "custom"
    assert inspected["verification_profile_candidates"] == []

    args = argparse.Namespace(
        path="/repo",
        repo_id="demo",
        decision=[],
        policy_override=[],
        template="standard",
        non_interactive=False,
    )
    assert cli._repo_propose(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pending_approval"

    ProposalService.proposal = _proposal(decisions=(RequiredDecision("x", "choose", ("a", "b")),))
    args.non_interactive = True
    assert cli._repo_propose(args) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "input_required"

    ProposalService.proposal = _proposal(confidence=ProposalConfidence.BLOCKED)
    assert cli._repo_propose(args) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    ProposalService.proposal = _proposal()


def test_repo_refresh_preview_accept_unchanged_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _common(monkeypatch)
    current = _generation(1, proposal_id="old")
    store = FakeStore(tmp_path, current=current)
    source = SourceConfiguration(
        "tunnel",
        "repoforge",
        (SourceRepository("demo", "/repos/demo", "old", "standard"),),
    )
    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    monkeypatch.setattr(cli, "_editable_source", lambda value: source)
    delta = CapabilityDelta(
        CapabilityDeltaKind.EXPANSION,
        "a" * 64,
        "b" * 64,
        (CapabilityChange("repositories.demo", None, {}, CapabilityDeltaKind.EXPANSION, "added"),),
    )
    monkeypatch.setattr(cli, "classify_capability_delta", lambda a, b: delta)

    args = argparse.Namespace(
        config=str(tmp_path / "config.toml"),
        repo_id=None,
        decision=[],
        policy_override=[],
        template=None,
        approve=[],
        accept=False,
        activate="auto",
        wait=True,
        rollback_on_failure=True,
    )
    assert cli._repo_refresh(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "preview"

    args.accept = True
    assert cli._repo_refresh(args) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "pending_approval"

    args.approve = [f"approve:{ProposalService.proposal.proposal_id}"]
    assert cli._repo_refresh(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"

    args.repo_id = "missing"
    with pytest.raises(ConfigError, match="Unknown repository"):
        cli._repo_refresh(args)
    store._current = None
    with pytest.raises(ConfigError, match="No accepted"):
        cli._repo_refresh(args)


def test_repo_enroll_and_remove_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _common(monkeypatch)
    store = FakeStore(tmp_path, current=_generation())
    source = SourceConfiguration(
        "tunnel", "repoforge", (SourceRepository("existing", "/repos/existing"),)
    )
    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    monkeypatch.setattr(cli, "_editable_source", lambda value: source)
    monkeypatch.setattr(cli, "add_source_repository", lambda value, item: value)
    monkeypatch.setattr(cli, "remove_source_repository", lambda value, repo_id: value)
    monkeypatch.setattr(cli, "parse_source", lambda text: source)
    monkeypatch.setattr(cli, "remove_repository", lambda document, repo_id: document)

    args = argparse.Namespace(
        config=str(tmp_path / "config.toml"),
        path="/repos/demo",
        repo_id="demo",
        decision=[],
        policy_override=[],
        template="standard",
        approve=f"approve:{ProposalService.proposal.proposal_id}",
        activate="auto",
        wait=True,
        rollback_on_failure=True,
    )
    assert cli._repo_enroll(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"

    ProposalService.proposal = _proposal(decisions=(RequiredDecision("x", "choose", ("a",)),))
    assert cli._repo_enroll(args) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "input_required"
    ProposalService.proposal = _proposal(confidence=ProposalConfidence.BLOCKED)
    with pytest.raises(ConfigError, match="blocked"):
        cli._repo_enroll(args)
    ProposalService.proposal = _proposal()

    remove_args = argparse.Namespace(config=str(tmp_path / "config.toml"), repo_id="demo")
    assert cli._repo_remove(remove_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accepted_restriction"
    store._current = None
    with pytest.raises(ConfigError, match="No accepted"):
        cli._repo_remove(remove_args)
