from __future__ import annotations


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


def test_discriminated_modes_are_real_enums_not_free_form_strings() -> None:
    _, registry = _contracts()
    expectations = {
        "repo_history": {"commit", "log", "compare"},
        "repo_issue": {"read", "spec", "graph", "next"},
        "repo_policy": {"preview", "apply"},
        "workspace_refresh": {"preview", "apply"},
        "workspace_verify": {"auto", "diagnostic", "profile", "adhoc"},
        "workspace_pr": {"create_draft", "update", "watch"},
        "operation": {"get", "list", "cancel"},
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
        field = spec.output_model.model_fields["error"]
        assert common.ToolError in getattr(field.annotation, "__args__", ()), tool_name


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
