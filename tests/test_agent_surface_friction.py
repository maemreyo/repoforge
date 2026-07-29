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
  marking which ones those are.

The last class covers the one finding that turned out to be a documentation gap rather
than a missing capability: a mutation whose response is lost to a dead connection is
still recoverable, and these tests pin that so it stays true.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.application.workspace.mutate import CreateMutation
from repoforge.contracts import v2 as v2_contracts
from repoforge.contracts.v2 import WorkspaceVerifyInput
from repoforge.domain.adhoc import (
    MAX_ADHOC_ARGV_ELEMENT_LENGTH,
    MAX_ADHOC_ARGV_ELEMENTS,
    validate_adhoc_argv,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.excerpts import bound_command_excerpt
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
