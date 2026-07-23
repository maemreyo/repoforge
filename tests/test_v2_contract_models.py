from __future__ import annotations

import pytest


def _contracts():
    from repoforge.contracts import common, registry

    return common, registry


EXPECTED_V2_TOOLS = (
    "repo_task_context",
    "repo_read",
    "repo_search",
    "repo_tree",
    "repo_history",
    "repo_issue",
    "repo_pr_read",
    "repo_list",
    "repo_policy",
    "workspace_create",
    "workspace_remove",
    "workspace_list",
    "workspace_refresh",
    "workspace_status",
    "workspace_format_changed",
    "workspace_read",
    "workspace_search",
    "workspace_tree",
    "workspace_diff",
    "workspace_mutate",
    "workspace_verify",
    "workspace_commit",
    "workspace_push",
    "workspace_pr",
    "workspace_pr_evidence",
    "operation",
    "config_inspect",
    "runtime_logs_read",
)


def _walk_objects(schema: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            found.append(schema)
        for value in schema.values():
            found.extend(_walk_objects(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_walk_objects(value))
    return found


def test_v2_roster_is_static_and_exactly_twenty_eight_tools() -> None:
    _, registry = _contracts()
    assert registry.V2_TOOL_NAMES == EXPECTED_V2_TOOLS
    assert tuple(registry.V2_TOOL_SPECS) == EXPECTED_V2_TOOLS
    assert len(set(registry.V2_TOOL_NAMES)) == 28


def test_every_tool_has_strict_pydantic_input_and_output_models() -> None:
    _, registry = _contracts()
    for tool_name in EXPECTED_V2_TOOLS:
        spec = registry.V2_TOOL_SPECS[tool_name]

        assert hasattr(spec.input_model, "model_json_schema")
        assert hasattr(spec.output_model, "model_json_schema")
        assert spec.input_model.model_config.get("extra") == "forbid"
        assert spec.output_model.model_config.get("extra") == "forbid"

        for model in (spec.input_model, spec.output_model):
            schema = model.model_json_schema()
            objects = _walk_objects(schema)
            assert objects, (tool_name, model.__name__)
            assert all(item.get("additionalProperties") is False for item in objects), (
                tool_name,
                model.__name__,
                objects,
            )


def test_runtime_log_limit_bound_is_published_and_enforced() -> None:
    _, registry = _contracts()
    model = registry.V2_TOOL_SPECS["runtime_logs_read"].input_model
    limit_schema = model.model_json_schema()["properties"]["limit"]

    assert limit_schema["minimum"] == 1
    assert limit_schema["maximum"] == 200
    assert model(limit=200).limit == 200
    try:
        model(limit=201)
    except ValueError:
        pass
    else:
        raise AssertionError("runtime_logs_read limit=201 must be rejected")


def test_operation_contract_publishes_all_terminal_states_and_action_validation() -> None:
    from pydantic import ValidationError

    common, registry = _contracts()
    assert {item.value for item in common.OperationState} >= {
        "pending",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
        "orphaned",
    }
    model = registry.V2_TOOL_SPECS["operation"].input_model
    with pytest.raises(ValidationError):
        model(action="get")
    with pytest.raises(ValidationError):
        model(action="list", operation_id="op-000000000000000000000001")
    with pytest.raises(ValidationError):
        model(action="cancel", operation_id="op-000000000000000000000001", scope="task:x")


def test_operation_wait_contract_bounds_timeout_and_cursor_fields() -> None:
    from pydantic import ValidationError

    _, registry = _contracts()
    spec = registry.V2_TOOL_SPECS["operation"]
    model = spec.input_model
    operation_id = "op-000000000000000000000001"

    validated = model(
        action="wait",
        operation_id=operation_id,
        since_updated_at="2026-07-19T00:00:00+00:00",
        timeout_seconds=30,
    )
    assert validated.action.value == "wait"
    assert validated.timeout_seconds == 30

    with pytest.raises(ValidationError):
        model(action="wait", operation_id=operation_id, timeout_seconds=61)
    with pytest.raises(ValidationError):
        model(action="get", operation_id=operation_id, timeout_seconds=30)
    with pytest.raises(ValidationError):
        model(action="list", since_updated_at="2026-07-19T00:00:00+00:00")

    output_fields = set(spec.output_model.model_fields)
    assert {"changed_since", "timed_out"} <= output_fields
    operation_schema = spec.output_model.model_json_schema()
    rendered = str(operation_schema)
    assert "suggested_poll_after_s" in rendered
    assert "eta_seconds" in rendered
    assert "progress_message" in rendered
    assert "progress_unit" in rendered


def test_runtime_log_time_range_requires_timezone_and_order() -> None:
    from pydantic import ValidationError

    _, registry = _contracts()
    model = registry.V2_TOOL_SPECS["runtime_logs_read"].input_model
    with pytest.raises(ValidationError):
        model(start_time="2026-07-16T00:00:00")
    with pytest.raises(ValidationError):
        model(
            start_time="2026-07-16T00:02:00+00:00",
            end_time="2026-07-16T00:01:00+00:00",
        )


def test_discriminated_modes_are_real_enums_not_free_form_strings() -> None:
    _, registry = _contracts()
    expectations = {
        "repo_history": {"commit", "log", "compare"},
        "repo_issue": {
            "read",
            "spec",
            "graph",
            "next",
            "comment",
            "close",
            "reopen",
            "link",
            "create",
            "manage",
        },
        "repo_policy": {"preview", "apply"},
        "workspace_refresh": {"preview", "apply", "recreate_from_latest_base"},
        "workspace_verify": {"plan", "auto", "diagnostic", "profile", "adhoc"},
        "workspace_pr": {"create_draft", "update", "comment", "watch", "reconcile"},
        "operation": {"get", "wait", "list", "cancel", "failure_evidence"},
    }

    for tool_name, expected in expectations.items():
        field = registry.V2_TOOL_SPECS[tool_name].input_model.model_fields[
            "mode"
            if tool_name not in {"repo_policy", "workspace_refresh", "workspace_pr", "operation"}
            else "action"
        ]
        enum_type = field.annotation
        assert isinstance(enum_type, type) and hasattr(enum_type, "__members__"), tool_name
        assert {member.value for member in enum_type} == expected


def test_all_outputs_share_one_typed_error_contract() -> None:
    common, registry = _contracts()
    for tool_name, spec in registry.V2_TOOL_SPECS.items():
        validated = spec.validate_output(
            {
                "status": "failed",
                "summary": "Request failed",
                "error": {
                    "code": "NOT_FOUND",
                    "message": "Resource not found",
                    "why": "The requested resource does not exist.",
                    "safe_next_action": "Refresh state and choose an existing resource.",
                },
            }
        )
        assert isinstance(validated, common.ToolFailure), tool_name


def test_registry_runtime_validation_rejects_unknown_fields() -> None:
    _, registry = _contracts()
    spec = registry.V2_TOOL_SPECS["repo_list"]

    try:
        spec.validate_input({"detail": False, "unexpected": "nope"})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown input fields must be rejected")


def test_retrieval_contracts_publish_budget_and_truncation_metadata() -> None:
    _, registry = _contracts()
    expected_output_fields = {
        "repo_search": {"omitted_count", "source_truncated"},
        "repo_tree": {"omitted_count", "source_truncated"},
        "workspace_search": {"omitted_count", "source_truncated"},
        "workspace_tree": {"omitted_count", "source_truncated"},
        "workspace_diff": {"staged", "omitted_count", "source_truncated"},
    }
    for tool_name, fields in expected_output_fields.items():
        model_fields = registry.V2_TOOL_SPECS[tool_name].output_model.model_fields
        assert fields <= set(model_fields), (tool_name, set(model_fields))

    diff_input = registry.V2_TOOL_SPECS["workspace_diff"].input_model
    assert diff_input.model_fields["max_files"].default == 100
    schema = diff_input.model_json_schema()["properties"]["max_files"]
    assert schema["minimum"] == 1
    assert schema["maximum"] == 1000


def test_repo_policy_contract_carries_typed_generated_paths() -> None:
    _, registry = _contracts()
    spec = registry.V2_TOOL_SPECS["repo_policy"]

    validated = spec.validate_input(
        {
            "repo_id": "demo",
            "action": "preview",
            "mutations": [],
            "generated_paths": [
                {
                    "glob": "docs/contracts/*.json",
                    "regeneration_command": ["python", "render.py"],
                    "description": "Generated contracts",
                }
            ],
        }
    )

    assert validated.generated_paths[0].glob == "docs/contracts/*.json"
    schema = spec.input_model.model_json_schema()
    generated = schema["properties"]["generated_paths"]
    assert generated["maxItems"] == 64


def test_repo_policy_contract_rejects_unknown_issue_template_fields() -> None:
    _, registry = _contracts()
    spec = registry.V2_TOOL_SPECS["repo_policy"]

    with pytest.raises(ValueError, match=r"exactly.*body.*evidence_ref"):
        spec.validate_input(
            {
                "repo_id": "demo",
                "action": "preview",
                "issue_writes": {
                    "create_body_template": "{body}\n{evidence_ref}\n{unknown}",
                },
            }
        )


def test_workspace_refresh_contract_has_typed_conflicts_and_resolutions() -> None:
    _, registry = _contracts()
    spec = registry.V2_TOOL_SPECS["workspace_refresh"]

    validated = spec.validate_input(
        {
            "workspace_id": "demo-workspace",
            "action": "apply",
            "expected_head_sha": "a" * 40,
            "expected_fingerprint": "b" * 64,
            "plan_token": "refresh-v2:" + "c" * 40 + ":" + "d" * 64 + ":" + "e" * 64,
            "resolutions": [
                {
                    "path": "hello.txt",
                    "content": "reviewed resolution\n",
                }
            ],
        }
    )

    assert validated.resolutions[0].path == "hello.txt"
    input_schema = spec.input_model.model_json_schema()
    assert input_schema["properties"]["resolutions"]["maxItems"] == 100
    output_fields = set(spec.output_model.model_fields)
    assert {
        "prediction_scope",
        "apply_blockers",
        "conflicts",
        "warnings",
        "changed_paths",
        "verify_selector",
        "invalidated_receipts",
        "transaction_id",
    } <= output_fields


def test_repo_issue_contract_exposes_governed_write_modes() -> None:
    from pydantic import ValidationError

    _, registry = _contracts()
    spec = registry.V2_TOOL_SPECS["repo_issue"]
    comment = spec.validate_input(
        {
            "repo_id": "demo",
            "mode": "comment",
            "issue_number": 7,
            "body": "Verification evidence is attached.",
            "evidence_ref": "commit:abc123",
            "idempotency_key": "repo-issue-comment-0001",
        }
    )

    assert comment.mode.value == "comment"
    assert comment.evidence_ref == "commit:abc123"
    with pytest.raises(ValidationError):
        spec.validate_input(
            {
                "repo_id": "demo",
                "mode": "close",
                "issue_number": 7,
                "idempotency_key": "repo-issue-close-0001",
            }
        )
    schema = spec.input_model.model_json_schema()
    assert set(schema["$defs"]["IssueMode"]["enum"]) == {
        "read",
        "spec",
        "graph",
        "next",
        "comment",
        "close",
        "reopen",
        "link",
        "create",
        "manage",
    }
    output = spec.validate_output(
        {
            "summary": "Applied repo_issue comment",
            "repo_id": "demo",
            "mode": "comment",
            "graph_status": "not_requested",
            "mutation": {
                "operation": "comment",
                "result": "applied",
                "issue_number": 7,
                "marker": "<!-- repoforge-issue-write:" + "a" * 64 + " -->",
                "external_writes": 1,
                "url": "https://github.com/acme/demo/issues/7#issuecomment-1",
            },
        }
    )
    assert output.mutation is not None
    assert output.mutation.external_writes == 1


def test_repo_issue_contract_exposes_closed_issue_graph_manage_branch() -> None:
    from pydantic import ValidationError

    _, registry = _contracts()
    spec = registry.V2_TOOL_SPECS["repo_issue"]
    plan = spec.validate_input(
        {
            "repo_id": "demo",
            "mode": "manage",
            "manage": {
                "action": "plan",
                "root_ref": "epic-232",
                "nodes": [
                    {
                        "client_ref": "epic-232",
                        "title": "Control-plane truth hardening",
                        "ticket_type": "epic",
                        "priority": "p0",
                        "status": "in_progress",
                        "body": "## Objective\n\nShip it.\n\n## Acceptance criteria\n\n- [ ] Done.\n",
                    }
                ],
                "edges": [],
                "adopt_refs": [],
                "expires_in_seconds": 3600,
            },
        }
    )
    assert plan.mode.value == "manage"
    assert plan.manage.action == "plan"

    apply = spec.validate_input(
        {
            "repo_id": "demo",
            "mode": "manage",
            "manage": {
                "action": "apply",
                "proposal_id": "igp-" + "a" * 24,
                "proposal_hash": "b" * 64,
                "plan_id": "igplan-" + "c" * 24,
                "effect_plan_hash": "d" * 64,
                "approval_request_id": "apr-" + "e" * 24,
            },
        }
    )
    assert apply.manage.action == "apply"

    for action in ("status", "reconcile"):
        validated = spec.validate_input(
            {
                "repo_id": "demo",
                "mode": "manage",
                "manage": {
                    "action": action,
                    "publication_id": "igpub-" + "f" * 24,
                },
            }
        )
        assert validated.manage.action == action

    with pytest.raises(ValidationError):
        spec.validate_input({"repo_id": "demo", "mode": "manage"})
    with pytest.raises(ValidationError):
        spec.validate_input(
            {
                "repo_id": "demo",
                "mode": "read",
                "issue_number": 7,
                "manage": {"action": "status", "publication_id": "igpub-" + "f" * 24},
            }
        )
    with pytest.raises(ValidationError):
        spec.validate_input(
            {
                "repo_id": "demo",
                "mode": "manage",
                "manage": {
                    "action": "status",
                    "publication_id": "igpub-" + "f" * 24,
                    "proposal_hash": "b" * 64,
                },
            }
        )

    schema = spec.input_model.model_json_schema()
    assert "manage" in schema["properties"]
    assert set(schema["$defs"]["IssueMode"]["enum"]) == {
        "read",
        "spec",
        "graph",
        "next",
        "comment",
        "close",
        "reopen",
        "link",
        "create",
        "manage",
    }
    output = spec.validate_output(
        {
            "summary": "Issue graph publication is pending operator approval",
            "repo_id": "demo",
            "mode": "manage",
            "graph_status": "not_requested",
            "workflow": {
                "action": "plan",
                "state": "pending_approval",
                "proposal_id": "igp-" + "a" * 24,
                "proposal_hash": "b" * 64,
                "plan_id": "igplan-" + "c" * 24,
                "effect_plan_hash": "d" * 64,
                "approval_request_id": "apr-" + "e" * 24,
                "approval_status": "pending",
                "complete": False,
                "external_writes": 0,
                "recovery_action": "Approve the exact request, then retry apply.",
            },
        }
    )
    assert output.workflow is not None
    assert output.workflow.complete is False


def test_workspace_verify_contract_exposes_planning_routing_and_evidence_fields() -> None:
    _, registry = _contracts()
    spec = registry.V2_TOOL_SPECS["workspace_verify"]

    validated = spec.validate_input(
        {
            "workspace_id": "demo-workspace",
            "mode": "diagnostic",
            "diagnostic_id": "pytest-target",
            "selector": ["tests/test_one.py", "tests/test_two.py"],
            "intent": "tdd_green",
            "expectation": "pass",
            "force_rerun": True,
            "impact_paths": ["src/one.py"],
            "artifact_output_path": "build/verify/result.json",
        }
    )

    assert validated.mode.value == "diagnostic"
    assert validated.intent.value == "tdd_green"
    assert validated.force_rerun is True
    assert validated.impact_paths == ("src/one.py",)
    output_fields = set(spec.output_model.model_fields)
    assert {
        "assessment",
        "recommendations",
        "staleness_warning",
        "steps",
        "failed_step",
        "failure_domain",
        "business_tests_ran",
        "valid_tdd_red_evidence",
        "failure_reused",
        "artifact_paths",
        "execution_evidence",
        "failed_selectors",
        "output_artifact_reference",
        "failure_expectation",
        "failure_chain_id",
        "rerun_of_selectors",
    } <= output_fields


def test_workspace_verify_contract_validates_rerun_failed_evidence() -> None:
    from pydantic import ValidationError

    _, registry = _contracts()
    spec = registry.V2_TOOL_SPECS["workspace_verify"]

    validated = spec.validate_input(
        {
            "workspace_id": "demo-workspace",
            "mode": "diagnostic",
            "diagnostic_id": "pytest-target",
            "rerun": "failed",
        }
    )
    assert validated.rerun == "failed"

    with pytest.raises(ValidationError):
        spec.validate_input(
            {
                "workspace_id": "demo-workspace",
                "mode": "profile",
                "profile_name": "test",
                "rerun": "failed",
            }
        )
    with pytest.raises(ValidationError):
        spec.validate_input(
            {
                "workspace_id": "demo-workspace",
                "mode": "diagnostic",
                "diagnostic_id": "pytest-target",
                "selector": "tests/test_one.py::test_one",
                "rerun": "failed",
            }
        )

    output = spec.validate_output(
        {
            "summary": "Diagnostic pytest-target failed",
            "workspace_id": "demo-workspace",
            "requested_mode": "diagnostic",
            "selected_mode": "diagnostic",
            "routing_reason": "Explicit diagnostic mode was requested.",
            "failure_domain": "test_failure",
            "outcome": "failed",
            "satisfies_commit_gate": False,
            "head_sha": "a" * 40,
            "workspace_fingerprint": "b" * 64,
            "failed_selectors": [
                "tests/test_one.py::test_one",
                "tests/test_two.py::test_two",
            ],
            "output_artifact_reference": "failure-output:" + "c" * 64,
            "failure_expectation": "unexpected",
            "failure_chain_id": "failure-chain-" + "d" * 24,
            "rerun_of_selectors": [
                "tests/test_one.py::test_one",
                "tests/test_two.py::test_two",
            ],
        }
    )
    assert output.failure_expectation == "unexpected"
    assert output.failed_selectors == (
        "tests/test_one.py::test_one",
        "tests/test_two.py::test_two",
    )


def test_execution_capable_outputs_expose_closed_truthful_evidence() -> None:
    from pydantic import ValidationError

    _, registry = _contracts()
    for tool_name in ("workspace_verify", "workspace_format_changed"):
        schema = registry.V2_TOOL_SPECS[tool_name].output_model.model_json_schema()
        rendered = str(schema)
        for field in (
            "adapter_kind",
            "identity_schema_version",
            "environment_identity_hash",
            "requested_policy_hash",
            "effective_policy_hash",
            "requested_network",
            "effective_network",
            "requested_filesystem",
            "effective_filesystem",
            "degraded",
            "enforcement",
            "warnings",
        ):
            assert field in rendered, (tool_name, field)

    evidence_model = (
        registry.V2_TOOL_SPECS["workspace_verify"]
        .output_model.model_fields["execution_evidence"]
        .annotation
    )
    assert evidence_model is not None
    with pytest.raises(ValidationError):
        registry.V2_TOOL_SPECS["workspace_verify"].validate_output(
            {
                "summary": "Verification passed",
                "workspace_id": "workspace-1",
                "requested_mode": "profile",
                "selected_mode": "profile",
                "routing_reason": "Explicit profile mode was requested.",
                "outcome": "passed",
                "satisfies_commit_gate": True,
                "head_sha": "a" * 40,
                "workspace_fingerprint": "b" * 64,
                "execution_evidence": {
                    "adapter_kind": "native",
                    "identity_schema_version": 2,
                    "environment_identity_hash": "c" * 64,
                    "requested_policy_hash": "d" * 64,
                    "effective_policy_hash": "e" * 64,
                    "requested_network": "offline",
                    "effective_network": "host_inherited",
                    "requested_filesystem": "workspace_write",
                    "effective_filesystem": "host_account_access",
                    "degraded": True,
                    "enforcement": {
                        "network": "advisory",
                        "filesystem": "advisory",
                        "timeout": "enforced",
                        "output": "enforced",
                        "process_cleanup": "enforced",
                        "cpu": "unsupported",
                        "memory": "unsupported",
                        "disk": "unsupported",
                        "subprocess_count": "unsupported",
                        "network_bytes": "unsupported",
                    },
                    "warnings": [],
                    "unknown": "rejected",
                },
            }
        )


def test_workspace_verify_selector_sequences_publish_practical_bounds() -> None:
    from pydantic import ValidationError

    _, registry = _contracts()
    spec = registry.V2_TOOL_SPECS["workspace_verify"]
    with pytest.raises(ValidationError):
        spec.validate_input(
            {
                "workspace_id": "demo-workspace",
                "selector": ["x" for _ in range(101)],
            }
        )

    schema = spec.input_model.model_json_schema(mode="validation")
    for field_name in ("selector", "selector2"):
        nullable = schema["properties"][field_name]["anyOf"]
        array_branch = next(branch for branch in nullable if branch.get("type") == "array")
        assert array_branch["maxItems"] == 100
        assert array_branch["items"]["maxLength"] == 4096


def test_mutation_schema_exposes_all_ops_and_bounds() -> None:
    _, registry = _contracts()
    schema = registry.V2_TOOL_SPECS["workspace_mutate"].input_model.model_json_schema()
    operations = schema["properties"]["operations"]

    assert operations["minItems"] == 1
    assert operations["maxItems"] == 100
    rendered = str(schema)
    for operation in (
        "replace_text",
        "write",
        "create",
        "delete",
        "move",
        "apply_patch",
        "restore",
    ):
        assert operation in rendered


def test_workspace_mutate_output_publishes_closed_bounded_syntax_evidence() -> None:
    from pydantic import ValidationError

    _, registry = _contracts()
    spec = registry.V2_TOOL_SPECS["workspace_mutate"]
    payload = {
        "summary": "Applied mutation with parse_ok=false",
        "workspace_id": "demo-workspace",
        "dry_run": False,
        "ready": True,
        "changed": True,
        "would_change": True,
        "operation_count": 1,
        "operations": [
            {
                "index": 0,
                "op": "create",
                "path": "src/broken.py",
                "status": "ready",
                "changed": True,
            }
        ],
        "changed_paths": ["src/broken.py"],
        "head_sha": "a" * 40,
        "workspace_fingerprint": "b" * 64,
        "diff_stat": "1 file changed",
        "change_metrics": {
            "changed_files": 1,
            "added_lines": 1,
            "deleted_lines": 0,
            "diff_lines": 1,
            "binary_files": 0,
            "total_current_bytes": 12,
            "limits": {
                "max_changed_files": 150,
                "max_diff_lines": 12_000,
                "max_total_changed_bytes": 26_214_400,
            },
            "within_limits": True,
        },
        "syntax_diagnostics": {
            "state": "error",
            "parse_ok": False,
            "diagnostics": [
                {
                    "path": "src/broken.py",
                    "line": 1,
                    "message": "Unexpected syntax.",
                    "severity": "error",
                }
            ],
            "analyzed_paths": ["src/broken.py"],
            "unknown_paths": [],
            "truncated": False,
            "legacy_receipt": False,
        },
        "transaction_id": "tx-demo",
    }

    output = spec.validate_output(payload)
    assert output.syntax_diagnostics.state.value == "error"
    assert output.syntax_diagnostics.diagnostics[0].severity.value == "error"
    assert output.change_metrics.binary_files == 0
    assert output.change_metrics.limits.max_changed_files == 150

    schema = spec.output_model.model_json_schema(mode="validation")
    syntax = schema["$defs"]["SyntaxDiagnosticsEvidence"]
    assert syntax["properties"]["diagnostics"]["maxItems"] == 100
    assert syntax["properties"]["analyzed_paths"]["maxItems"] == 1000
    assert syntax["properties"]["unknown_paths"]["maxItems"] == 1000
    assert set(schema["$defs"]["SyntaxDiagnosticState"]["enum"]) == {
        "ok",
        "error",
        "unknown",
    }
    assert schema["$defs"]["SyntaxDiagnosticSeverity"]["enum"] == ["error"]

    invalid = dict(payload)
    invalid["syntax_diagnostics"] = dict(payload["syntax_diagnostics"])
    invalid["syntax_diagnostics"]["parse_ok"] = True
    with pytest.raises(ValidationError):
        spec.validate_output(invalid)
