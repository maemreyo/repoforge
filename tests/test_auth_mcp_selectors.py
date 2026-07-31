"""Identity selectors on the MCP surface: effectful inputs only, defaults unchanged.

A selector is an input to *choosing* an identity, so it belongs only where a call can actually
act as one. Read and watch branches must keep rejecting it, existing callers must keep working
without it, and the public tool roster must not grow.
"""

from __future__ import annotations

import json
from typing import Protocol, cast

import pytest
from pydantic import ValidationError

from repoforge.contracts.common import AuthActorClass
from repoforge.contracts.registry import render_v2_schema_bundle
from repoforge.contracts.v2 import (
    IssueMode,
    RefreshAction,
    RepoIssueInput,
    WorkspaceCommitInput,
    WorkspaceCreateInput,
    WorkspacePrAction,
    WorkspacePrInput,
    WorkspacePushInput,
    WorkspaceRefreshInput,
)
from repoforge.domain.auth_profile import AuthProfileSelector, RequestedActorClass

_SHA = "a" * 40
_SHA256 = "b" * 64


def _commit(**overrides: object) -> WorkspaceCommitInput:
    return WorkspaceCommitInput(workspace_id="ws-1", message="msg", **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Defaults preserve every existing caller
# ---------------------------------------------------------------------------


def test_every_effectful_input_defaults_to_the_deterministic_selector() -> None:
    inputs = (
        WorkspaceCreateInput(repo_id="demo", task_slug="task"),
        _commit(),
        WorkspacePushInput(workspace_id="ws-1"),
        WorkspaceRefreshInput(
            workspace_id="ws-1",
            action=RefreshAction.PREVIEW,
            expected_head_sha=_SHA,
            expected_fingerprint=_SHA256,
        ),
        WorkspacePrInput(
            workspace_id="ws-1",
            action=WorkspacePrAction.CREATE_DRAFT,
            title="t",
            body="b",
            idempotency_key="idem-key-1",
        ),
        RepoIssueInput(
            repo_id="demo",
            mode=IssueMode.COMMENT,
            issue_number=1,
            body="b",
            evidence_ref="ref",
            idempotency_key="idem-key-1",
        ),
    )
    for value in inputs:
        assert value.auth_profile == "auto", type(value).__name__
        assert value.actor_class is AuthActorClass.HUMAN, type(value).__name__


def test_an_explicit_selector_is_accepted_on_effectful_inputs() -> None:
    committed = _commit(auth_profile="personal", actor_class=AuthActorClass.AGENT)

    assert committed.auth_profile == "personal"
    assert committed.actor_class is AuthActorClass.AGENT


def test_the_public_selector_maps_onto_the_domain_selector_without_loss() -> None:
    for actor_class, expected in (
        (AuthActorClass.HUMAN, RequestedActorClass.HUMAN),
        (AuthActorClass.AGENT, RequestedActorClass.AGENT),
    ):
        committed = _commit(auth_profile="personal", actor_class=actor_class)

        selector = AuthProfileSelector(
            auth_profile=committed.auth_profile,
            actor_class=RequestedActorClass(committed.actor_class.value),
        )

        assert selector.actor_class is expected
        assert selector.auth_profile == "personal"
        assert selector.automatic is False


def test_the_domain_selector_still_fails_closed_on_a_secret_shaped_profile_id() -> None:
    # The contract pattern admits underscores, so the domain remains the fail-closed layer.
    accepted = _commit(auth_profile="ghp_looks_like_a_token")

    with pytest.raises(ValueError, match="non-secret"):
        AuthProfileSelector(auth_profile=accepted.auth_profile)


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def test_an_invalid_profile_id_or_actor_class_is_rejected() -> None:
    for overrides in (
        {"auth_profile": ""},
        {"auth_profile": "-leading-hyphen"},
        {"auth_profile": "has space"},
        {"auth_profile": "x" * 200},
        {"actor_class": "superuser"},
        {"actor_class": ""},
    ):
        with pytest.raises(ValidationError):
            _commit(**overrides)


def test_read_and_watch_branches_reject_a_selector() -> None:
    from repoforge.contracts.v2 import WorkspaceListInput, WorkspaceReadInput, WorkspaceStatusInput

    for factory in (
        lambda **kw: WorkspaceListInput(**kw),
        lambda **kw: WorkspaceStatusInput(workspace_id="ws-1", **kw),
        lambda **kw: WorkspaceReadInput(workspace_id="ws-1", **kw),
    ):
        with pytest.raises(ValidationError):
            factory(auth_profile="personal")
        with pytest.raises(ValidationError):
            factory(actor_class="human")


def test_a_watch_only_pr_action_rejects_a_selector() -> None:
    # `watch` performs no write, so selecting an identity for it is a caller mistake.
    with pytest.raises(ValidationError, match="auth_profile"):
        WorkspacePrInput(
            workspace_id="ws-1",
            action=WorkspacePrAction.WATCH,
            auth_profile="personal",
        )


def test_every_read_only_issue_mode_rejects_a_selector() -> None:
    for mode in (IssueMode.READ, IssueMode.SPEC, IssueMode.GRAPH, IssueMode.NEXT):
        with pytest.raises(ValidationError, match="auth_profile"):
            RepoIssueInput(
                repo_id="demo",
                mode=mode,
                issue_number=1,
                auth_profile="personal",
            )


def test_every_refresh_action_accepts_a_selector_because_each_reaches_the_remote() -> None:
    # Even `preview` fetches the base through the pinned transport, so there is no refresh
    # branch that runs without an identity.
    for action in RefreshAction:
        refreshed = WorkspaceRefreshInput(
            workspace_id="ws-1",
            action=action,
            expected_head_sha=_SHA,
            expected_fingerprint=_SHA256,
            auth_profile="personal",
        )
        assert refreshed.auth_profile == "personal", action


# ---------------------------------------------------------------------------
# The generated surface
# ---------------------------------------------------------------------------


def test_the_public_tool_roster_does_not_grow() -> None:
    bundle = render_v2_schema_bundle()

    assert bundle["tool_count"] == 28
    assert len(bundle["tools"]) == 28
    assert bundle["contract_version"] == 2


def test_selectors_appear_only_on_effectful_input_schemas() -> None:
    tools = render_v2_schema_bundle()["tools"]
    with_selector = {
        name
        for name, schemas in tools.items()
        if "auth_profile" in json.dumps(schemas["input"], sort_keys=True)
    }

    # Exactly the tools that can act as an identity, and nothing else.
    assert with_selector == {
        "repo_issue",
        "workspace_commit",
        "workspace_create",
        "workspace_pr",
        "workspace_push",
        "workspace_refresh",
    }
    # A selector chooses an identity for a call, so it is never a *result* field. It may
    # still appear inside a typed recovery action's `arguments`, because a suggested retry has
    # to carry the same identity choice as the call that failed.
    for name, schemas in tools.items():
        output = schemas["output"]
        assert "auth_profile" not in json.dumps(output.get("properties", {}), sort_keys=True), name
        carrying = {
            definition
            for definition, schema in output.get("$defs", {}).items()
            if "auth_profile" in json.dumps(schema, sort_keys=True)
        }
        assert all(definition.endswith("Input") for definition in carrying), (name, carrying)


def test_no_generated_schema_carries_a_credential_reference() -> None:
    rendered = json.dumps(render_v2_schema_bundle(), sort_keys=True)

    for canary in ("credential_ref", "https_token_environment", "ssh_identity_file", "gho_"):
        assert canary not in rendered, canary


# ---------------------------------------------------------------------------
# The selector must actually reach the application
# ---------------------------------------------------------------------------


def test_every_effectful_tool_forwards_the_selector_to_its_service_method() -> None:
    """The v2 dispatch maps validated input fields straight onto service kwargs.

    Adding a field to an input without accepting it on the service method makes every call to
    that tool raise `TypeError` at runtime, which no schema test would catch.
    """

    import inspect

    from repoforge.application.service import CodingService
    from repoforge.interfaces.mcp import server

    cases = {
        "workspace_create": WorkspaceCreateInput(repo_id="demo", task_slug="task"),
        "workspace_commit": _commit(),
        "workspace_push": WorkspacePushInput(workspace_id="ws-1"),
        "workspace_refresh": WorkspaceRefreshInput(
            workspace_id="ws-1",
            action=RefreshAction.PREVIEW,
            expected_head_sha=_SHA,
            expected_fingerprint=_SHA256,
        ),
        "workspace_pr": WorkspacePrInput(
            workspace_id="ws-1",
            action=WorkspacePrAction.CREATE_DRAFT,
            title="t",
            body="b",
            idempotency_key="idem-key-1",
        ),
        "repo_issue": RepoIssueInput(
            repo_id="demo",
            mode=IssueMode.COMMENT,
            issue_number=1,
            body="b",
            evidence_ref="ref",
            idempotency_key="idem-key-1",
        ),
    }
    for tool_name, model in cases.items():
        method = getattr(CodingService, server._SERVICE_METHODS[tool_name])
        accepted = set(inspect.signature(method).parameters)
        supplied = set(
            server._dispatch_kwargs(
                tool_name,
                model,
                runtime_identity=None,
            )
        )
        assert {"auth_profile", "actor_class"} <= supplied, tool_name
        assert supplied <= accepted, (tool_name, sorted(supplied - accepted))


def test_a_secret_shaped_selector_is_refused_before_any_effect_is_admitted() -> None:
    from repoforge.application.service import _auth_selector
    from repoforge.domain.errors import ErrorCode as DomainErrorCode
    from repoforge.domain.errors import RepoForgeError

    with pytest.raises(RepoForgeError) as failure:
        _auth_selector("ghp_looks_like_a_token", "human")

    assert failure.value.code is DomainErrorCode.CREDENTIAL_SCOPE_MISMATCH
    assert "No identity was acquired" in " ".join(failure.value.unchanged_state)
    assert "ghp_looks_like_a_token" not in str(failure.value)


def test_the_default_selector_is_the_deterministic_one_at_the_application_boundary() -> None:
    from repoforge.application.service import _auth_selector

    selector = _auth_selector("auto", "human")

    assert selector.automatic is True
    assert selector.actor_class is RequestedActorClass.HUMAN


def test_every_effectful_service_method_carries_the_exact_selector_into_its_command() -> None:
    from repoforge.application.service import CodingService

    class Recorder:
        def __init__(self) -> None:
            self.command: object | None = None

        def execute(self, command: object) -> dict[str, bool]:
            self.command = command
            return {"ok": True}

    service = object.__new__(CodingService)
    recorders = {
        "_repo_issue_v2": Recorder(),
        "_create_v2": Recorder(),
        "_refresh_v2": Recorder(),
        "_commit": Recorder(),
        "_push": Recorder(),
        "_pr": Recorder(),
    }
    for name, recorder in recorders.items():
        setattr(service, name, recorder)

    service.repo_issue_v2(
        "demo",
        mode="comment",
        issue_number=7,
        body="verified",
        evidence_ref="commit:abc",
        idempotency_key="issue-selector-0001",
        auth_profile="personal",
        actor_class="agent",
    )
    service.workspace_create_v2(
        "demo",
        "selector create",
        auth_profile="personal",
        actor_class="agent",
    )
    service.workspace_refresh_v2(
        "ws-1",
        action="preview",
        expected_head_sha=_SHA,
        expected_fingerprint=_SHA256,
        auth_profile="personal",
        actor_class="agent",
    )
    service.workspace_commit(
        "ws-1",
        "selector commit",
        auth_profile="personal",
        actor_class="agent",
    )
    service.workspace_push(
        "ws-1",
        auth_profile="personal",
        actor_class="agent",
    )
    service.workspace_pr(
        "ws-1",
        action="create_draft",
        title="Selector PR",
        body="Verified selector propagation.",
        idempotency_key="pr-selector-0001",
        auth_profile="personal",
        actor_class="agent",
    )

    class SelectedCommand(Protocol):
        selector: AuthProfileSelector

    expected = AuthProfileSelector("personal", RequestedActorClass.AGENT)
    for name, recorder in recorders.items():
        assert recorder.command is not None, name
        command = cast(SelectedCommand, recorder.command)
        assert command.selector == expected, name


def test_every_nested_effect_command_and_publication_request_has_a_selector() -> None:
    from pathlib import Path

    from repoforge.application.repository.family_v2 import RepositoryIssueV2Command
    from repoforge.application.repository.issue_mutation_v2 import (
        RepositoryIssueMutationCommand,
    )
    from repoforge.application.workspace.commit import WorkspaceCommitCommand
    from repoforge.application.workspace.create import WorkspaceCreateCommand
    from repoforge.application.workspace.create_draft_pr import (
        WorkspaceCreateDraftPrCommand,
    )
    from repoforge.application.workspace.family_v2 import WorkspaceCreateV2Command
    from repoforge.application.workspace.pr import WorkspacePrCommand
    from repoforge.application.workspace.push import WorkspacePushCommand
    from repoforge.application.workspace.refresh_v2 import WorkspaceRefreshV2Command
    from repoforge.application.workspace.update_draft_pr import (
        WorkspaceUpdateDraftPrCommand,
    )
    from repoforge.ports.workspace_publication import (
        WorkspaceDraftPrPublication,
        WorkspacePushPublication,
    )

    selector = AuthProfileSelector("personal", RequestedActorClass.AGENT)
    commands = (
        RepositoryIssueV2Command("demo", "comment", selector=selector),
        RepositoryIssueMutationCommand("demo", "comment", selector=selector),
        WorkspaceCreateV2Command("demo", "task", selector=selector),
        WorkspaceCreateCommand("demo", "task", selector=selector),
        WorkspaceRefreshV2Command("ws-1", "preview", _SHA, _SHA256, selector=selector),
        WorkspaceCommitCommand("ws-1", "message", selector=selector),
        WorkspacePushCommand("ws-1", selector=selector),
        WorkspacePrCommand("ws-1", "create_draft", selector=selector),
        WorkspaceCreateDraftPrCommand("ws-1", "title", "body", selector=selector),
        WorkspaceUpdateDraftPrCommand("ws-1", selector=selector),
        WorkspacePushPublication(
            workspace_id="ws-1",
            repo_id="demo",
            cwd=Path("/tmp/workspace"),
            remote="origin",
            source_ref="refs/heads/feature",
            destination_ref="refs/heads/feature",
            head_sha=_SHA,
            tree_sha=_SHA,
            remote_head_before=None,
            idempotency_key=None,
            selector=selector,
        ),
        WorkspaceDraftPrPublication(
            workspace_id="ws-1",
            repo_id="demo",
            cwd=Path("/tmp/workspace"),
            remote="origin",
            base_ref="refs/heads/main",
            head_ref="refs/heads/feature",
            head_sha=_SHA,
            tree_sha=_SHA,
            title="title",
            body="body",
            idempotency_key=None,
            selector=selector,
        ),
    )

    assert all(command.selector == selector for command in commands)
