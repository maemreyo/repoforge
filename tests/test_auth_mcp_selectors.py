"""Identity selectors on the MCP surface: effectful inputs only, defaults unchanged.

A selector is an input to *choosing* an identity, so it belongs only where a call can actually
act as one. Read and watch branches must keep rejecting it, existing callers must keep working
without it, and the public tool roster must not grow.
"""

from __future__ import annotations

import json

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
