"""Coverage for the agent-facing friction found by auditing a real session transcript.

Each test here pins one place where the surface cost an agent a turn -- or a whole
re-run -- for a reason that was not the agent's mistake:

* command evidence truncated the tail, discarding the failure summary a caller runs
  a suite to obtain, forcing a second full run;
* the ad-hoc argv contract advertised bounds far wider than the domain enforces, so
  a request the schema accepts was rejected after the fact;
* an undeclared generated path failed the refresh transaction without naming the
  declaration that would fix it;
* the repository context listed profiles the model is not allowed to invoke without
  marking which ones those are;
* a command timeout named neither the budget that expired nor the config field that
  sets it, so the caller hand-rolled a workaround instead of asking for more time.

The recovery class covers the one finding that turned out to be a documentation gap rather
than a missing capability: a mutation whose response is lost to a dead connection is
still recoverable, and these tests pin that so it stays true.
"""

from __future__ import annotations

from dataclasses import replace as replace_dataclass
from typing import Any

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.application.workspace.mutate import CreateMutation
from repoforge.application.workspace.run_adhoc import _adhoc_timeout_remedy
from repoforge.contracts import v2 as v2_contracts
from repoforge.contracts.v2 import WorkspaceVerifyInput
from repoforge.domain.adhoc import (
    MAX_ADHOC_ARGV_ELEMENT_LENGTH,
    MAX_ADHOC_ARGV_ELEMENTS,
    MAX_ADHOC_RUNNERS,
    MAX_ADHOC_STDIN_LENGTH,
    validate_adhoc_argv,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.excerpts import bound_command_excerpt
from repoforge.domain.policy_patch import MAX_ADHOC_TIMEOUT_SECONDS
from repoforge.interfaces.mcp.server import create_server

_EXCERPT_LIMIT = 12_000


class TestBoundCommandExcerpt:
    def test_short_output_is_returned_unchanged(self) -> None:
        assert bound_command_excerpt("1 passed", _EXCERPT_LIMIT) == "1 passed"

    def test_exact_limit_is_returned_unchanged(self) -> None:
        value = "x" * _EXCERPT_LIMIT
        assert bound_command_excerpt(value, _EXCERPT_LIMIT) == value

    def test_failure_summary_at_the_tail_survives_truncation(self) -> None:
        """The regression that made an agent re-run a whole suite: pytest writes the
        short test summary last, so a head-only cut drops exactly the failing lines."""
        summary = "=== short test summary info ===\nFAILED tests/test_x.py::test_y"
        value = ("collection noise\n" * 4_000) + summary

        excerpt = bound_command_excerpt(value, _EXCERPT_LIMIT)

        assert excerpt.endswith(summary)
        assert "collection noise" in excerpt
        assert "characters omitted" in excerpt

    def test_result_never_exceeds_the_contract_bound(self) -> None:
        for length in (_EXCERPT_LIMIT + 1, 50_000, 5_000_000):
            excerpt = bound_command_excerpt("y" * length, _EXCERPT_LIMIT)
            assert len(excerpt) <= _EXCERPT_LIMIT, length

    def test_omitted_count_accounts_for_every_dropped_character(self) -> None:
        value = "z" * 40_000
        excerpt = bound_command_excerpt(value, _EXCERPT_LIMIT)
        marker = excerpt[excerpt.index("... <") : excerpt.index(" characters omitted")]
        omitted = int(marker.removeprefix("... <"))
        assert (
            len(excerpt) - len(marker) - len(" characters omitted> ...\n") - 1 + omitted == 40_000
        )

    def test_a_limit_too_small_for_both_ends_keeps_the_tail(self) -> None:
        assert bound_command_excerpt("abcdefghij", 4) == "ghij"

    def test_a_non_positive_limit_yields_no_excerpt(self) -> None:
        assert bound_command_excerpt("anything", 0) == ""


class TestAdhocArgvContractMatchesEnforcement:
    """The schema must not accept an argv the domain will reject after admission."""

    def test_schema_rejects_more_elements_than_the_domain_allows(self) -> None:
        argv = tuple(["python3"] + ["-c"] * MAX_ADHOC_ARGV_ELEMENTS)
        assert len(argv) > MAX_ADHOC_ARGV_ELEMENTS

        with pytest.raises(ValueError):
            WorkspaceVerifyInput(workspace_id="ws-1", mode="adhoc", argv=argv)

    def test_schema_rejects_a_longer_element_than_the_domain_allows(self) -> None:
        argv = ("python3", "-c", "x" * (MAX_ADHOC_ARGV_ELEMENT_LENGTH + 1))

        with pytest.raises(ValueError):
            WorkspaceVerifyInput(workspace_id="ws-1", mode="adhoc", argv=argv)

    def test_schema_accepts_argv_at_the_enforced_bounds(self) -> None:
        argv = ("python3", "x" * MAX_ADHOC_ARGV_ELEMENT_LENGTH)

        request = WorkspaceVerifyInput(workspace_id="ws-1", mode="adhoc", argv=argv)

        assert request.argv == argv

    def test_contract_bounds_equal_the_domain_constants(self) -> None:
        """The contracts package imports no domain module, so nothing but this test
        stops the advertised bounds from drifting away from the enforced ones again."""
        assert v2_contracts._MAX_ADHOC_ARGV_ELEMENTS == MAX_ADHOC_ARGV_ELEMENTS
        assert v2_contracts._MAX_ADHOC_ARGV_ELEMENT_LENGTH == MAX_ADHOC_ARGV_ELEMENT_LENGTH

    def test_selector_bounds_stay_wide(self) -> None:
        """Only argv narrows: a selector is a test path, not a command argument."""
        request = WorkspaceVerifyInput(
            workspace_id="ws-1",
            mode="diagnostic",
            diagnostic_id="pytest",
            selector="p" * 4_096,
        )

        assert request.selector is not None


class TestAdhocArgvErrorNamesTheViolation:
    def test_a_newline_is_reported_as_a_newline_not_as_oversized(self) -> None:
        """An agent pasting a multi-line script was told its element was "empty,
        oversized, or control-character" and had to guess which."""
        with pytest.raises(RepoForgeError) as caught:
            validate_adhoc_argv(("python3", "-c", "import os\nprint(os.getcwd())"), ("python3",))

        error = caught.value
        assert error.code is ErrorCode.ADHOC_ARGV_INVALID
        assert "newline" in str(error).lower()
        assert "argv[2]" in str(error)

    def test_an_oversized_element_is_reported_as_oversized(self) -> None:
        with pytest.raises(RepoForgeError) as caught:
            validate_adhoc_argv(
                ("python3", "x" * (MAX_ADHOC_ARGV_ELEMENT_LENGTH + 1)), ("python3",)
            )

        error = caught.value
        assert error.code is ErrorCode.ADHOC_ARGV_INVALID
        assert str(MAX_ADHOC_ARGV_ELEMENT_LENGTH) in str(error)
        assert "argv[1]" in str(error)

    def test_an_empty_element_is_reported_as_empty(self) -> None:
        with pytest.raises(RepoForgeError) as caught:
            validate_adhoc_argv(("python3", ""), ("python3",))

        assert "empty" in str(caught.value).lower()
        assert "argv[1]" in str(caught.value)

    def test_a_valid_argv_still_passes(self) -> None:
        argv = ("python3", "-c", "print(1)")
        assert validate_adhoc_argv(argv, ("python3",)) == argv


class TestFieldsActuallyReachTheToolSurface:
    """Every assertion here goes through a real MCP session, on purpose.

    Three fixes in this area were verified at the service layer and shipped green
    while being invisible to a caller: `repo_list` projects `RepositorySummary`,
    which never carried profile data; the v2 `repo_task_context` repository section
    is built from its own Facts and never reads `RepositoryContextResult`; and the
    poll-to-wait guidance was set on a background result whose field the verify
    output does not have. A service-level test cannot catch any of those.
    """

    @staticmethod
    def _reserve_full_profile(service: Any) -> None:
        from repoforge.config import ProfileConfig

        profiles = service.config.repositories["demo"].profiles
        profiles["full"] = replace_dataclass(profiles["full"], model_invocable=False)
        assert isinstance(profiles["full"], ProfileConfig)

    @pytest.mark.anyio
    async def test_repo_list_reports_which_profiles_the_model_may_invoke(
        self, forge_env: ForgeEnvironment
    ) -> None:
        self._reserve_full_profile(forge_env.service)
        server = create_server(service=forge_env.service)

        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("repo_list", {"detail": True})

        assert result.isError is False
        assert result.structuredContent is not None
        entry = next(
            item for item in result.structuredContent["repositories"] if item["repo_id"] == "demo"
        )
        assert entry["operator_only_profiles"] == ["full"]
        assert "quick" in entry["model_invocable_profiles"]
        assert "full" not in entry["model_invocable_profiles"]

    @pytest.mark.anyio
    async def test_repo_task_context_repository_section_names_reserved_profiles(
        self, forge_env: ForgeEnvironment
    ) -> None:
        self._reserve_full_profile(forge_env.service)
        server = create_server(service=forge_env.service)

        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool(
                "repo_task_context", {"repo_id": "demo", "sections": ["repository"]}
            )

        assert result.isError is False
        assert result.structuredContent is not None
        section = next(
            item for item in result.structuredContent["sections"] if item["name"] == "repository"
        )
        facts = {fact["key"]: fact["value"] for fact in section["facts"]}
        assert "operator_only_profiles" in facts
        assert "full" in facts["operator_only_profiles"]
        assert "quick" in facts["model_invocable_profiles"]

    @pytest.mark.anyio
    async def test_background_verify_tells_the_caller_to_wait_not_to_poll(
        self, forge_env: ForgeEnvironment
    ) -> None:
        """The anti-polling fix is worthless if the caller never sees it."""
        service = forge_env.service
        workspace_id = str(service.workspace_create("demo", "background guidance")["workspace_id"])
        server = create_server(service=service)

        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool(
                "workspace_verify",
                {
                    "workspace_id": workspace_id,
                    "mode": "profile",
                    "profile_name": "full",
                    "background": True,
                },
            )

        assert result.isError is False
        assert result.structuredContent is not None
        next_action = result.structuredContent["next_action"]
        assert next_action is not None
        assert "until='terminal'" in next_action
        assert "operation" in next_action
        assert "Poll operation status" not in next_action


class TestTimeoutRemedyNamesTheBudget:
    """A timeout that hides which budget expired invites a hand-rolled workaround.

    A real session hit the ad-hoc budget on a full coverage recording and responded by
    writing a temporary chunking helper into the repository, producing a generated
    artifact its own documented generator could no longer reproduce. The budget is an
    operator-adjustable config field, and saying so is the whole fix.
    """

    def test_the_remedy_names_the_budget_the_field_and_the_ceiling(self) -> None:
        remedy = _adhoc_timeout_remedy(600)

        assert "600s" in remedy
        assert "adhoc_timeout_seconds" in remedy
        assert "3600s" in remedy

    def test_the_remedy_offers_a_profile_and_refuses_hand_chunking(self) -> None:
        remedy = _adhoc_timeout_remedy(300)

        assert "profile" in remedy
        assert "reproducible" in remedy
        assert "chunk" in remedy


class TestLostResponseRecovery:
    """A transport failure between effect and response must stay answerable.

    A real session lost its connection at the moment a mutation was applied, and the
    agent concluded it could not know whether the write had landed. It could have: the
    effect is durable and addressable. These tests pin the two halves that make the
    answer reachable, because nothing else does.
    """

    @pytest.mark.anyio
    async def test_a_mutating_call_returns_its_durable_outcome_identity(
        self, forge_env: ForgeEnvironment
    ) -> None:
        """The response names the operation and receipt, so a caller can record the
        identity of its own write before anything else can go wrong."""
        server = create_server(service=forge_env.service)
        service = forge_env.service
        workspace_id = str(service.workspace_create("demo", "outcome identity")["workspace_id"])
        before = service.workspace_status(workspace_id)

        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool(
                "workspace_mutate",
                {
                    "workspace_id": workspace_id,
                    "operations": [
                        {"op": "create", "path": "recovered.txt", "content": "recovered\n"}
                    ],
                    "expected_workspace_fingerprint": str(before["workspace_fingerprint"]),
                    "expected_head_sha": str(before["head_sha"]),
                },
            )

        assert result.isError is False
        assert result.structuredContent is not None
        outcome = result.structuredContent["outcome"]
        assert outcome["operation_id"].startswith("op-")
        assert outcome["receipt_id"].startswith("receipt-")
        assert outcome["state"] in {"applied_unvalidated", "applied_validated"}

    def test_a_synchronous_mutation_is_recoverable_by_workspace_scope(
        self, forge_env: ForgeEnvironment
    ) -> None:
        """Even with the response lost, the mutation is listable by workspace and its
        durable result readable -- which is what "did my write land?" reduces to."""
        service = forge_env.service
        workspace_id = str(service.workspace_create("demo", "lost response")["workspace_id"])
        before = service.workspace_status(workspace_id)

        service.workspace_mutate(
            workspace_id,
            [CreateMutation(path="landed.txt", content="landed\n")],
            expected_workspace_fingerprint=str(before["workspace_fingerprint"]),
        )

        listed = service.operation_list(scope=f"workspace:{workspace_id}")
        mutations = [item for item in listed["operations"] if item["kind"] == "workspace_mutate"]
        assert len(mutations) == 1
        record = mutations[0]
        assert record["state"] == "succeeded"
        assert record["receipt_id"] is not None
        assert record["result_reference"] is not None

        durable = service.operation_status(str(record["operation_id"]))
        result: dict[str, Any] = durable["result"]
        assert result["changed"] is True
        paths = [operation["path"] for operation in result["operations"]]
        assert paths == ["landed.txt"]


class TestReasoningTravelsWithTheSchema:
    """A `#:` comment is invisible to a caller, and that has already cost real time.

    Twice now an agent has read `tool_surface_changed_since_activation=true` -- normal
    after a release upgrade -- as proof of a diverged installation, because the sentence
    saying so lived in a Sphinx-style source comment that never reaches the emitted
    schema. Issue #314 was the first time. Pydantic only publishes `Field(description=)`,
    so a field whose semantics are non-obvious has to carry one.
    """

    @staticmethod
    def _fields_documented_only_by_source_comments() -> list[tuple[str, str]]:
        import re
        from pathlib import Path

        found: list[tuple[str, str]] = []
        root = Path(__file__).resolve().parent.parent / "src" / "repoforge" / "contracts"
        for module in ("v2.py", "common.py"):
            lines = (root / module).read_text(encoding="utf-8").split("\n")
            commented = False
            for line in lines:
                if re.match(r"^\s+#:", line):
                    commented = True
                    continue
                if commented:
                    match = re.match(r"^\s+([a-z_][a-z0-9_]*)\s*:", line)
                    if match:
                        found.append((module, match.group(1)))
                    commented = False
        return found

    def test_no_contract_field_is_documented_only_by_a_source_comment(self) -> None:
        assert self._fields_documented_only_by_source_comments() == []

    def test_the_activation_evidence_facts_say_they_are_not_faults(self) -> None:
        """The exact three fields that produced the false divergence report."""
        from repoforge.contracts.v2 import RuntimeActivationEvidenceView

        schema = RuntimeActivationEvidenceView.model_json_schema()["properties"]

        verdict = schema["agreement"]["description"]
        assert "configuration generation only" in verdict

        for name in (
            "process_restarted_since_activation",
            "tool_surface_changed_since_activation",
        ):
            description = schema[name]["description"]
            assert "fact, not a fault" in description, name
            assert "agreement" in description, name


class TestAdhocPolicyContractMatchesEnforcement:
    """A repo_policy proposal must not advertise what the config loader will refuse."""

    def test_stdin_bound_equals_the_domain_constant(self) -> None:
        assert v2_contracts._MAX_ADHOC_STDIN_LENGTH == MAX_ADHOC_STDIN_LENGTH

    def test_runner_bounds_equal_the_domain_constants(self) -> None:
        assert v2_contracts._MAX_ADHOC_RUNNERS == MAX_ADHOC_RUNNERS
        assert v2_contracts._MAX_ADHOC_TIMEOUT_SECONDS == MAX_ADHOC_TIMEOUT_SECONDS

    def test_schema_rejects_a_runner_path_the_domain_rejects(self) -> None:
        with pytest.raises(ValueError):
            v2_contracts.ExecutionPolicyDeclaration(adhoc_runners=("/usr/bin/python3",))

    def test_schema_accepts_a_bare_runner_basename(self) -> None:
        declaration = v2_contracts.ExecutionPolicyDeclaration(adhoc_runners=("uv", "bash"))

        assert declaration.adhoc_runners == ("uv", "bash")

    def test_stdin_text_is_refused_outside_adhoc_mode(self) -> None:
        with pytest.raises(ValueError, match="only valid for mode=adhoc"):
            WorkspaceVerifyInput(
                workspace_id="ws-1",
                mode="profile",
                profile_name="full",
                stdin_text="anything",
            )
