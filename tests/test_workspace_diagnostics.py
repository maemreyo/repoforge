from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.application.configuration.document import render_resolved
from repoforge.application.workspace.diagnostic_parser import parse_diagnostic
from repoforge.application.workspace.diagnostic_selector import resolve_diagnostic_selector
from repoforge.config import load_config
from repoforge.domain.config_generation import CapabilityDeltaKind, classify_capability_delta
from repoforge.domain.diagnostics import (
    DiagnosticMutability,
    DiagnosticNetworkPolicy,
    DiagnosticParserKind,
    DiagnosticProfileConfig,
    DiagnosticSelectorConfig,
    DiagnosticSelectorKind,
    validate_diagnostic_profile,
)
from repoforge.domain.errors import ConfigError, ErrorCode, RepoForgeError
from repoforge.domain.workspace import VerificationReceipt
from repoforge.ports.command import CommandResult


def _write_config(tmp_path: Path, diagnostics: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'''[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"

[repositories.demo]
path = "{repo}"

{diagnostics}
''',
        encoding="utf-8",
    )
    return config


def test_loads_typed_diagnostic_profiles(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        """[repositories.demo.diagnostics.pytest-target]
summary = "Run one tracked pytest target"
argv = ["uv", "run", "--extra", "dev", "pytest", "{selector}", "-q"]
selector_kind = "pytest_node"
timeout_seconds = 45
network_policy = "local_only"
mutability = "read_only"
parser = "pytest"
output_limit = 12000

[repositories.demo.diagnostics.release-contract-diff]
summary = "Check release contract drift"
argv = ["uv", "run", "--extra", "dev", "python", "scripts/check_release_contracts.py"]
selector_kind = "none"
timeout_seconds = 30
network_policy = "local_only"
mutability = "read_only"
parser = "release_contract"
output_limit = 8000
""",
    )

    loaded = load_config(config)
    pytest_target = loaded.repositories["demo"].diagnostics["pytest-target"]
    assert pytest_target.argv_template == (
        "uv",
        "run",
        "--extra",
        "dev",
        "pytest",
        "{selector}",
        "-q",
    )
    assert pytest_target.selector.kind is DiagnosticSelectorKind.PYTEST_NODE
    assert pytest_target.network_policy is DiagnosticNetworkPolicy.LOCAL_ONLY
    assert pytest_target.mutability is DiagnosticMutability.READ_ONLY
    assert pytest_target.parser is DiagnosticParserKind.PYTEST
    assert pytest_target.output_limit == 12_000

    release_contract = loaded.repositories["demo"].diagnostics["release-contract-diff"]
    assert release_contract.selector.kind is DiagnosticSelectorKind.NONE
    assert release_contract.parser is DiagnosticParserKind.RELEASE_CONTRACT


def test_existing_config_defaults_to_no_diagnostics(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "")
    assert load_config(config).repositories["demo"].diagnostics == {}


@pytest.mark.parametrize(
    ("diagnostics", "message"),
    [
        (
            """[repositories.demo.diagnostics.bad]
summary = "bad"
argv = ["pytest", "{selector}", "{selector}"]
selector_kind = "tracked_path"
timeout_seconds = 10
network_policy = "local_only"
mutability = "read_only"
parser = "pytest"
output_limit = 1000
""",
            "placeholder",
        ),
        (
            """[repositories.demo.diagnostics.bad]
summary = "bad"
argv = ["pytest"]
selector_kind = "tracked_path"
timeout_seconds = 10
network_policy = "local_only"
mutability = "read_only"
parser = "pytest"
output_limit = 1000
""",
            "selector",
        ),
        (
            """[repositories.demo.diagnostics.bad]
summary = "bad"
argv = ["pytest", "--target={selector}"]
selector_kind = "tracked_path"
timeout_seconds = 10
network_policy = "local_only"
mutability = "read_only"
parser = "pytest"
output_limit = 1000
""",
            "complete argv element",
        ),
        (
            """[repositories.demo.diagnostics.bad]
summary = "bad"
argv = ["python", "tool.py"]
selector_kind = "none"
timeout_seconds = 10
network_policy = "local_only"
mutability = "artifacts"
parser = "text"
output_limit = 1000
""",
            "artifact_paths",
        ),
        (
            """[repositories.demo.diagnostics.bad]
summary = "bad"
argv = ["python", "tool.py"]
selector_kind = "none"
timeout_seconds = 10
network_policy = "unrestricted"
mutability = "read_only"
parser = "text"
output_limit = 1000
""",
            "network_policy",
        ),
        (
            """[repositories.demo.diagnostics.bad]
summary = "bad"
argv = ["python", "tool.py"]
selector_kind = "none"
working_directory = "/tmp"
timeout_seconds = 10
network_policy = "local_only"
mutability = "read_only"
parser = "text"
output_limit = 1000
""",
            "repository-relative path",
        ),
        (
            """[repositories.demo.diagnostics.bad]
summary = "bad"
argv = ["python", "tool.py"]
selector_kind = "none"
timeout_seconds = 10
network_policy = "local_only"
mutability = "artifacts"
parser = "text"
output_limit = 1000
artifact_paths = ["/tmp/**"]
""",
            "repository-relative path",
        ),
    ],
)
def test_rejects_unsafe_diagnostic_profiles(
    tmp_path: Path,
    diagnostics: str,
    message: str,
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(_write_config(tmp_path, diagnostics))


def _diagnostic_document(repo: Path, *, argv: list[str] | None = None) -> dict[str, object]:
    return {
        "server": {
            "workspace_root": str(repo.parent / "workspaces"),
            "state_root": str(repo.parent / "state"),
        },
        "repositories": {
            "demo": {
                "path": str(repo),
                "diagnostics": {
                    "pytest-target": {
                        "summary": "Run one target",
                        "argv": argv or ["pytest", "{selector}", "-q"],
                        "selector_kind": "pytest_node",
                        "selector_values": [],
                        "timeout_seconds": 45,
                        "network_policy": "local_only",
                        "mutability": "read_only",
                        "parser": "pytest",
                        "output_limit": 12000,
                        "artifact_paths": [],
                    }
                },
            }
        },
    }


def _render(document: dict[str, object]) -> str:
    return render_resolved(
        document,
        generation=1,
        source_path="config.toml",
        source_sha256="a" * 64,
        created_at="2026-07-14T00:00:00+00:00",
        reason="test",
        proposal_id=None,
        repository_fingerprints=(("demo", "b" * 64),),
    )


def test_resolved_config_round_trips_diagnostics(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    rendered = _render(_diagnostic_document(repo))
    assert "[repositories.demo.diagnostics.pytest-target]" in rendered
    assert 'argv = ["pytest", "{selector}", "-q"]' in rendered
    config = tmp_path / "resolved.toml"
    config.write_text(rendered, encoding="utf-8")
    loaded = load_config(config)
    assert loaded.repositories["demo"].diagnostics["pytest-target"].timeout_seconds == 45


def test_diagnostic_capability_delta_is_semantic(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    document = _diagnostic_document(repo)
    before = _render(
        {
            "server": document["server"],
            "repositories": {"demo": {"path": str(repo)}},
        }
    )
    added = _render(_diagnostic_document(repo))
    assert classify_capability_delta(before, added).kind is CapabilityDeltaKind.EXPANSION
    assert classify_capability_delta(added, before).kind is CapabilityDeltaKind.RESTRICTION

    replaced = _render(_diagnostic_document(repo, argv=["ruff", "check", "{selector}"]))
    assert classify_capability_delta(added, replaced).kind is CapabilityDeltaKind.INCOMPATIBLE

    metadata = added.replace('summary = "Run one target"', 'summary = "Run a reviewed target"')
    assert classify_capability_delta(added, metadata).kind is CapabilityDeltaKind.METADATA_ONLY

    widened = added.replace("timeout_seconds = 45", "timeout_seconds = 60")
    assert classify_capability_delta(added, widened).kind is CapabilityDeltaKind.EXPANSION


def _profile(
    *,
    selector_kind: DiagnosticSelectorKind = DiagnosticSelectorKind.PYTEST_NODE,
    parser: DiagnosticParserKind = DiagnosticParserKind.PYTEST,
    selector_values: tuple[str, ...] = (),
) -> DiagnosticProfileConfig:
    return DiagnosticProfileConfig(
        diagnostic_id="pytest-target",
        summary="Run one tracked target",
        argv_template=("pytest", "{selector}", "-q")
        if selector_kind is not DiagnosticSelectorKind.NONE
        else ("python", "scripts/check_release_contracts.py"),
        selector=DiagnosticSelectorConfig(kind=selector_kind, values=selector_values),
        working_directory=None,
        timeout_seconds=30,
        network_policy=DiagnosticNetworkPolicy.LOCAL_ONLY,
        mutability=DiagnosticMutability.READ_ONLY,
        parser=parser,
        output_limit=2_000,
    )


def test_resolves_tracked_pytest_node_to_one_argv_token(forge_env: ForgeEnvironment) -> None:
    created = forge_env.service.workspace_create("demo", "diagnostic selector")
    record, repo, workspace = forge_env.service.application.context.workspace(
        created["workspace_id"]
    )
    assert record.workspace_id == created["workspace_id"]

    resolved = resolve_diagnostic_selector(
        _profile(),
        "hello.txt::test_example[param]",
        workspace=workspace,
        repo=repo,
        git=forge_env.service.application.context.git,
    )

    assert resolved.value == "hello.txt::test_example[param]"
    assert resolved.argv == ("pytest", "hello.txt::test_example[param]", "-q")


@pytest.mark.parametrize(
    "selector",
    [
        "../escape.py::test_x",
        "-k expression",
        ".github/workflows/ci.yml::test_x",
        "missing.py::test_x",
        "hello.txt;echo-owned::test_x",
    ],
)
def test_rejects_unsafe_or_untracked_selectors(
    forge_env: ForgeEnvironment,
    selector: str,
) -> None:
    created = forge_env.service.workspace_create("demo", "bad diagnostic selector")
    _, repo, workspace = forge_env.service.application.context.workspace(created["workspace_id"])
    with pytest.raises(RepoForgeError) as invalid:
        resolve_diagnostic_selector(
            _profile(),
            selector,
            workspace=workspace,
            repo=repo,
            git=forge_env.service.application.context.git,
        )
    assert invalid.value.code is ErrorCode.DIAGNOSTIC_SELECTOR_INVALID


def test_enum_and_none_selector_contracts(forge_env: ForgeEnvironment) -> None:
    created = forge_env.service.workspace_create("demo", "enum selector")
    _, repo, workspace = forge_env.service.application.context.workspace(created["workspace_id"])
    git = forge_env.service.application.context.git
    enum_profile = _profile(
        selector_kind=DiagnosticSelectorKind.ENUM,
        selector_values=("unit", "integration"),
    )
    assert resolve_diagnostic_selector(
        enum_profile,
        "unit",
        workspace=workspace,
        repo=repo,
        git=git,
    ).argv == ("pytest", "unit", "-q")
    with pytest.raises(RepoForgeError) as enum_error:
        resolve_diagnostic_selector(
            enum_profile,
            "other",
            workspace=workspace,
            repo=repo,
            git=git,
        )
    assert enum_error.value.code is ErrorCode.DIAGNOSTIC_SELECTOR_INVALID

    none_profile = _profile(
        selector_kind=DiagnosticSelectorKind.NONE,
        parser=DiagnosticParserKind.RELEASE_CONTRACT,
    )
    assert (
        resolve_diagnostic_selector(
            none_profile,
            None,
            workspace=workspace,
            repo=repo,
            git=git,
        ).value
        is None
    )
    with pytest.raises(RepoForgeError) as unexpected:
        resolve_diagnostic_selector(
            none_profile,
            "extra",
            workspace=workspace,
            repo=repo,
            git=git,
        )
    assert unexpected.value.code is ErrorCode.DIAGNOSTIC_SELECTOR_INVALID


# ---------------------------------------------------------------------------
# #168: generalized typed diagnostics -- token selectors, multi-value
# selectors, two named placeholders, and flag-injection defense.
# ---------------------------------------------------------------------------


def _token_profile(
    *,
    char_classes: tuple[str, ...] = ("alnum", "underscore"),
    max_length: int = 32,
    prefix: str | None = None,
    suffix: str | None = None,
    max_values: int = 1,
    expansion: str = "repeat",
    separator: str | None = None,
    allow_leading_dash: bool = False,
    argv_template: tuple[str, ...] = ("pytest", "-k", "{selector}"),
) -> DiagnosticProfileConfig:
    return DiagnosticProfileConfig(
        diagnostic_id="token-diag",
        summary="Run pytest filtered by a keyword token",
        argv_template=argv_template,
        selector=DiagnosticSelectorConfig(
            kind=DiagnosticSelectorKind.TOKEN,
            char_classes=char_classes,
            max_length=max_length,
            prefix=prefix,
            suffix=suffix,
            max_values=max_values,
            expansion=expansion,
            separator=separator,
            allow_leading_dash=allow_leading_dash,
        ),
        working_directory=None,
        timeout_seconds=30,
        network_policy=DiagnosticNetworkPolicy.LOCAL_ONLY,
        mutability=DiagnosticMutability.READ_ONLY,
        parser=DiagnosticParserKind.TEXT,
        output_limit=2_000,
    )


def test_token_selector_accepts_allowlisted_characters(forge_env: ForgeEnvironment) -> None:
    created = forge_env.service.workspace_create("demo", "token selector")
    _, repo, workspace = forge_env.service.application.context.workspace(created["workspace_id"])
    git = forge_env.service.application.context.git
    resolved = resolve_diagnostic_selector(
        _token_profile(), "test_example", workspace=workspace, repo=repo, git=git
    )
    assert resolved.argv == ("pytest", "-k", "test_example")


@pytest.mark.parametrize(
    "value",
    [
        "bad space",  # space class not enabled
        "semi;colon",  # shell metacharacter
        "-leading",  # leading dash rejected by default
        "x" * 64,  # exceeds max_length=32
    ],
)
def test_token_selector_rejects_disallowed_values(forge_env: ForgeEnvironment, value: str) -> None:
    created = forge_env.service.workspace_create("demo", "bad token selector")
    _, repo, workspace = forge_env.service.application.context.workspace(created["workspace_id"])
    git = forge_env.service.application.context.git
    with pytest.raises(RepoForgeError) as exc:
        resolve_diagnostic_selector(
            _token_profile(), value, workspace=workspace, repo=repo, git=git
        )
    assert exc.value.code is ErrorCode.DIAGNOSTIC_SELECTOR_INVALID


def test_token_selector_enforces_prefix_and_suffix(forge_env: ForgeEnvironment) -> None:
    created = forge_env.service.workspace_create("demo", "token prefix selector")
    _, repo, workspace = forge_env.service.application.context.workspace(created["workspace_id"])
    git = forge_env.service.application.context.git
    profile = _token_profile(char_classes=("alnum", "path"), prefix="test_", suffix="_case")
    resolved = resolve_diagnostic_selector(
        profile, "test_foo_case", workspace=workspace, repo=repo, git=git
    )
    assert resolved.argv == ("pytest", "-k", "test_foo_case")
    with pytest.raises(RepoForgeError):
        resolve_diagnostic_selector(profile, "foo_case", workspace=workspace, repo=repo, git=git)
    with pytest.raises(RepoForgeError):
        resolve_diagnostic_selector(profile, "test_foo", workspace=workspace, repo=repo, git=git)


def _multi_path_profile(
    *, max_values: int = 8, expansion: str = "repeat", separator: str | None = None
) -> DiagnosticProfileConfig:
    return DiagnosticProfileConfig(
        diagnostic_id="pytest-files",
        summary="Run tracked pytest files",
        argv_template=("pytest", "{selector}", "-q"),
        selector=DiagnosticSelectorConfig(
            kind=DiagnosticSelectorKind.TRACKED_PATH,
            max_values=max_values,
            expansion=expansion,
            separator=separator,
        ),
        working_directory=None,
        timeout_seconds=30,
        network_policy=DiagnosticNetworkPolicy.LOCAL_ONLY,
        mutability=DiagnosticMutability.READ_ONLY,
        parser=DiagnosticParserKind.PYTEST,
        output_limit=2_000,
    )


def test_multi_value_repeat_expansion_produces_one_element_per_value(
    forge_env: ForgeEnvironment,
) -> None:
    created = forge_env.service.workspace_create("demo", "multi selector")
    _, repo, workspace = forge_env.service.application.context.workspace(created["workspace_id"])
    git = forge_env.service.application.context.git
    resolved = resolve_diagnostic_selector(
        _multi_path_profile(),
        ["hello.txt", "README.md"],
        workspace=workspace,
        repo=repo,
        git=git,
    )
    assert resolved.argv == ("pytest", "hello.txt", "README.md", "-q")
    assert resolved.values["selector"] == ("hello.txt", "README.md")


def test_multi_value_selector_rejects_values_beyond_max_values(
    forge_env: ForgeEnvironment,
) -> None:
    created = forge_env.service.workspace_create("demo", "multi selector cap")
    _, repo, workspace = forge_env.service.application.context.workspace(created["workspace_id"])
    git = forge_env.service.application.context.git
    with pytest.raises(RepoForgeError) as exc:
        resolve_diagnostic_selector(
            _multi_path_profile(max_values=1),
            ["hello.txt", "README.md"],
            workspace=workspace,
            repo=repo,
            git=git,
        )
    assert exc.value.code is ErrorCode.DIAGNOSTIC_SELECTOR_INVALID
    assert exc.value.details == {
        "selector_name": "selector",
        "selector_kind": "tracked_path",
        "max_values": 1,
        "received_values": 2,
        "expansion": "repeat",
    }


def test_multi_value_join_expansion_uses_declared_separator(
    forge_env: ForgeEnvironment,
) -> None:
    created = forge_env.service.workspace_create("demo", "join selector")
    _, repo, workspace = forge_env.service.application.context.workspace(created["workspace_id"])
    git = forge_env.service.application.context.git
    profile = _token_profile(
        char_classes=("alnum",),
        max_values=3,
        expansion="join",
        separator="|",
        argv_template=("go", "test", "-run", "{selector}"),
    )
    resolved = resolve_diagnostic_selector(
        profile, ["TestA", "TestB"], workspace=workspace, repo=repo, git=git
    )
    assert resolved.argv == ("go", "test", "-run", "TestA|TestB")


def test_two_named_selectors_resolve_independently(forge_env: ForgeEnvironment) -> None:
    created = forge_env.service.workspace_create("demo", "two selectors")
    _, repo, workspace = forge_env.service.application.context.workspace(created["workspace_id"])
    git = forge_env.service.application.context.git
    profile = DiagnosticProfileConfig(
        diagnostic_id="go-test-pkg",
        summary="Run go test for one package filtered by name",
        argv_template=("go", "test", "{selector}", "-run", "{selector:name}"),
        selector=DiagnosticSelectorConfig(
            kind=DiagnosticSelectorKind.TOKEN,
            char_classes=("alnum", "path"),
        ),
        selector2=DiagnosticSelectorConfig(
            kind=DiagnosticSelectorKind.TOKEN,
            name="name",
            char_classes=("alnum",),
        ),
        working_directory=None,
        timeout_seconds=30,
        network_policy=DiagnosticNetworkPolicy.LOCAL_ONLY,
        mutability=DiagnosticMutability.READ_ONLY,
        parser=DiagnosticParserKind.TEXT,
        output_limit=2_000,
    )
    resolved = resolve_diagnostic_selector(
        profile, "./pkg", "TestFoo", workspace=workspace, repo=repo, git=git
    )
    assert resolved.argv == ("go", "test", "./pkg", "-run", "TestFoo")
    assert resolved.values == {"selector": ("./pkg",), "name": ("TestFoo",)}


def test_second_selector_rejected_when_not_declared(forge_env: ForgeEnvironment) -> None:
    created = forge_env.service.workspace_create("demo", "unexpected second selector")
    _, repo, workspace = forge_env.service.application.context.workspace(created["workspace_id"])
    git = forge_env.service.application.context.git
    with pytest.raises(RepoForgeError) as exc:
        resolve_diagnostic_selector(
            _token_profile(), "unit", "extra", workspace=workspace, repo=repo, git=git
        )
    assert exc.value.code is ErrorCode.DIAGNOSTIC_SELECTOR_INVALID


def test_allow_leading_dash_requires_literal_terminator_at_config_load() -> None:
    profile = DiagnosticProfileConfig(
        diagnostic_id="bad-dash",
        summary="bad dash",
        argv_template=("pytest", "{selector}"),
        selector=DiagnosticSelectorConfig(
            kind=DiagnosticSelectorKind.TOKEN,
            char_classes=("alnum",),
            allow_leading_dash=True,
        ),
        working_directory=None,
        timeout_seconds=30,
        network_policy=DiagnosticNetworkPolicy.LOCAL_ONLY,
        mutability=DiagnosticMutability.READ_ONLY,
        parser=DiagnosticParserKind.TEXT,
        output_limit=2_000,
    )
    with pytest.raises(ConfigError, match="allow_leading_dash"):
        validate_diagnostic_profile(profile)


def test_allow_leading_dash_with_terminator_permits_leading_dash(
    forge_env: ForgeEnvironment,
) -> None:
    profile = DiagnosticProfileConfig(
        diagnostic_id="ok-dash",
        summary="ok dash",
        argv_template=("pytest", "--", "{selector}"),
        selector=DiagnosticSelectorConfig(
            kind=DiagnosticSelectorKind.TOKEN,
            char_classes=("alnum", "path"),
            allow_leading_dash=True,
        ),
        working_directory=None,
        timeout_seconds=30,
        network_policy=DiagnosticNetworkPolicy.LOCAL_ONLY,
        mutability=DiagnosticMutability.READ_ONLY,
        parser=DiagnosticParserKind.TEXT,
        output_limit=2_000,
    )
    validate_diagnostic_profile(profile)
    created = forge_env.service.workspace_create("demo", "dash selector")
    _, repo, workspace = forge_env.service.application.context.workspace(created["workspace_id"])
    git = forge_env.service.application.context.git
    resolved = resolve_diagnostic_selector(profile, "-x", workspace=workspace, repo=repo, git=git)
    assert resolved.argv == ("pytest", "--", "-x")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_values": 0}, "max_values"),
        ({"max_values": 17}, "max_values"),
        ({"expansion": "scatter"}, "expansion"),
        ({"max_values": 3, "expansion": "join"}, "separator"),
        ({"max_values": 3, "expansion": "join", "separator": " "}, "separator"),
        ({"char_classes": ("nope",)}, "char_classes"),
    ],
)
def test_token_selector_config_shape_is_validated_at_load_time(
    kwargs: dict[str, object], message: str
) -> None:
    base = {
        "char_classes": ("alnum",),
        "max_length": 32,
        "max_values": 1,
        "expansion": "repeat",
        "separator": None,
    }
    base.update(kwargs)
    profile = DiagnosticProfileConfig(
        diagnostic_id="bad-shape",
        summary="bad shape",
        argv_template=("pytest", "-k", "{selector}"),
        selector=DiagnosticSelectorConfig(kind=DiagnosticSelectorKind.TOKEN, **base),
        working_directory=None,
        timeout_seconds=30,
        network_policy=DiagnosticNetworkPolicy.LOCAL_ONLY,
        mutability=DiagnosticMutability.READ_ONLY,
        parser=DiagnosticParserKind.TEXT,
        output_limit=2_000,
    )
    with pytest.raises(ConfigError, match=message):
        validate_diagnostic_profile(profile)


def test_char_classes_rejected_for_non_token_kind() -> None:
    profile = DiagnosticProfileConfig(
        diagnostic_id="bad-kind",
        summary="bad kind",
        argv_template=("pytest", "{selector}"),
        selector=DiagnosticSelectorConfig(
            kind=DiagnosticSelectorKind.PACKAGE_NAME,
            char_classes=("alnum",),
        ),
        working_directory=None,
        timeout_seconds=30,
        network_policy=DiagnosticNetworkPolicy.LOCAL_ONLY,
        mutability=DiagnosticMutability.READ_ONLY,
        parser=DiagnosticParserKind.TEXT,
        output_limit=2_000,
    )
    with pytest.raises(ConfigError, match="char_classes"):
        validate_diagnostic_profile(profile)


def test_duplicate_selector_names_rejected_at_load_time() -> None:
    profile = DiagnosticProfileConfig(
        diagnostic_id="dup",
        summary="dup",
        argv_template=("go", "test", "{selector}", "{selector:selector}"),
        selector=DiagnosticSelectorConfig(
            kind=DiagnosticSelectorKind.TOKEN, char_classes=("alnum",)
        ),
        selector2=DiagnosticSelectorConfig(
            kind=DiagnosticSelectorKind.TOKEN, name="selector", char_classes=("alnum",)
        ),
        working_directory=None,
        timeout_seconds=30,
        network_policy=DiagnosticNetworkPolicy.LOCAL_ONLY,
        mutability=DiagnosticMutability.READ_ONLY,
        parser=DiagnosticParserKind.TEXT,
        output_limit=2_000,
    )
    with pytest.raises(ConfigError, match="duplicate"):
        validate_diagnostic_profile(profile)


def test_argv_expansion_beyond_bound_rejected_at_load_time() -> None:
    argv_template = tuple(["pytest", *[f"--flag{i}" for i in range(30)], "{selector}"])
    profile = DiagnosticProfileConfig(
        diagnostic_id="too-wide",
        summary="too wide",
        argv_template=argv_template,
        selector=DiagnosticSelectorConfig(
            kind=DiagnosticSelectorKind.TOKEN,
            char_classes=("alnum",),
            max_values=16,
            expansion="repeat",
        ),
        working_directory=None,
        timeout_seconds=30,
        network_policy=DiagnosticNetworkPolicy.LOCAL_ONLY,
        mutability=DiagnosticMutability.READ_ONLY,
        parser=DiagnosticParserKind.TEXT,
        output_limit=2_000,
    )
    with pytest.raises(ConfigError, match="argv"):
        validate_diagnostic_profile(profile)


def test_config_load_supports_token_selector_and_second_named_selector(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        """[repositories.demo.diagnostics.go-test-pkg]
summary = "Run go test for one package filtered by name"
argv = ["go", "test", "{selector}", "-run", "{selector:name}"]
selector_kind = "token"
selector_char_classes = ["alnum", "path"]
selector_max_length = 128
timeout_seconds = 60
network_policy = "local_only"
mutability = "read_only"
parser = "text"
output_limit = 8000

[repositories.demo.diagnostics.go-test-pkg.selectors.name]
kind = "token"
char_classes = ["alnum"]
max_length = 64
""",
    )
    loaded = load_config(config)
    diagnostic = loaded.repositories["demo"].diagnostics["go-test-pkg"]
    assert diagnostic.selector.kind is DiagnosticSelectorKind.TOKEN
    assert diagnostic.selector2 is not None
    assert diagnostic.selector2.name == "name"
    assert diagnostic.selector2.kind is DiagnosticSelectorKind.TOKEN


def test_repo_context_surfaces_diagnostic_schema_and_pack_suggestions(
    forge_env: ForgeEnvironment,
) -> None:
    context = forge_env.service.repo_context("demo")
    assert context["diagnostics"], "the demo repo enrolls at least one diagnostic"
    schema = context["diagnostics"][0]["selectors"][0]
    assert {"name", "kind", "max_values", "expansion"} <= set(schema)
    # The demo repo already enrolls diagnostics, so no pack suggestions are surfaced.
    assert context["diagnostic_pack_suggestions"] == []


def test_ecosystem_diagnostic_packs_are_suggested_when_none_enrolled(tmp_path: Path) -> None:
    from repoforge.domain.diagnostic_packs import (
        detect_ecosystems,
        ecosystem_diagnostic_packs,
    )

    ecosystems = detect_ecosystems(("pyproject.toml", "Makefile", "README.md"))
    assert ecosystems == {"python", "make"}
    packs = ecosystem_diagnostic_packs(ecosystems)
    assert packs, "python/make packs should be suggested"
    assert all(pack.config_snippet.strip() for pack in packs)
    # Nothing is ever auto-enrolled: packs are plain strings, never config mutations.
    assert all(isinstance(pack.config_snippet, str) for pack in packs)


def test_full_profile_failure_mentions_available_diagnostics(
    forge_env: ForgeEnvironment,
) -> None:
    created = forge_env.service.workspace_create("demo", "profile failure guidance")
    workspace_id = created["workspace_id"]
    # The "full" profile's command asserts hello.txt starts with "changed", which is
    # false on a freshly created workspace -- it fails deterministically without edits.
    with pytest.raises(RepoForgeError) as exc:
        forge_env.service.workspace_run_profile(workspace_id, "full")
    assert "pytest-target" in (exc.value.safe_next_action or "")
    assert "workspace_run_diagnostic" in (exc.value.safe_next_action or "")


def test_parses_pytest_and_release_contract_output() -> None:
    pytest_result = CommandResult(
        ("pytest", "hello.txt", "-q"),
        "/workspace",
        1,
        "2 passed, 1 failed, 3 skipped in 0.20s\nFAILED hello.txt::test_bad",
        "",
    )
    parsed = parse_diagnostic(_profile(), pytest_result)
    assert parsed.outcome == "failed"
    assert parsed.failure_class == "test_failure"
    assert parsed.fields == {
        "passed": 2,
        "failed": 1,
        "errors": 0,
        "skipped": 3,
        "collected": 6,
    }
    assert parsed.business_tests_ran is True
    assert "FAILED hello.txt::test_bad" in parsed.excerpt

    release_profile = _profile(
        selector_kind=DiagnosticSelectorKind.NONE,
        parser=DiagnosticParserKind.RELEASE_CONTRACT,
    )
    release = parse_diagnostic(
        release_profile,
        CommandResult(
            ("python", "scripts/check_release_contracts.py"),
            "/workspace",
            0,
            "release contracts match: 37 MCP tools, surface=abc, runtime-protocol=1\n",
            "",
        ),
    )
    assert release.outcome == "passed"
    assert release.fields["tool_count"] == 37


def test_parser_reports_dependency_failure_and_truncation() -> None:
    result = CommandResult(
        ("pytest", "hello.txt", "-q"),
        "/workspace",
        2,
        "",
        "ModuleNotFoundError: No module named 'demo'",
        stdout_truncated=False,
        stderr_truncated=True,
    )
    parsed = parse_diagnostic(_profile(), result)
    assert parsed.failure_class == "dependency_missing"
    assert parsed.output_truncated is True


@pytest.mark.parametrize(
    ("output", "expected_failure_class"),
    [
        (
            "ERROR collecting tests/test_demo.py\n1 error in 0.02s",
            "collection_error",
        ),
        (
            "SyntaxError: invalid syntax\nERROR collecting tests/test_demo.py",
            "syntax_error",
        ),
        (
            "ImportError: cannot import name 'missing' from 'demo'\nERROR collecting",
            "import_error",
        ),
        (
            "ModuleNotFoundError: No module named 'demo'\nERROR collecting",
            "dependency_missing",
        ),
        ("Permission denied while importing test module", "environment_mismatch"),
    ],
)
def test_pytest_parser_classifies_non_business_failures(
    output: str,
    expected_failure_class: str,
) -> None:
    parsed = parse_diagnostic(
        _profile(),
        CommandResult(("pytest", "tests/test_demo.py", "-q"), "/workspace", 2, output, ""),
    )

    assert parsed.failure_class == expected_failure_class
    assert parsed.business_tests_ran is False
    assert parsed.fields["collected"] == 0


def test_pytest_parser_rejects_zero_collected_tests_as_business_evidence() -> None:
    parsed = parse_diagnostic(
        _profile(),
        CommandResult(
            ("pytest", "tests/test_demo.py", "-q"),
            "/workspace",
            5,
            "no tests ran in 0.01s",
            "",
        ),
    )

    assert parsed.failure_class == "collection_error"
    assert parsed.business_tests_ran is False
    assert parsed.fields["collected"] == 0


def _runtime_profile(
    diagnostic_id: str,
    argv: tuple[str, ...],
    *,
    mutability: DiagnosticMutability = DiagnosticMutability.READ_ONLY,
    artifact_paths: tuple[str, ...] = (),
    timeout_seconds: int = 30,
    output_limit: int = 2_000,
    parser: DiagnosticParserKind = DiagnosticParserKind.TEXT,
) -> DiagnosticProfileConfig:
    return DiagnosticProfileConfig(
        diagnostic_id=diagnostic_id,
        summary=f"Run {diagnostic_id}",
        argv_template=argv,
        selector=DiagnosticSelectorConfig(DiagnosticSelectorKind.NONE),
        working_directory=None,
        timeout_seconds=timeout_seconds,
        network_policy=DiagnosticNetworkPolicy.LOCAL_ONLY,
        mutability=mutability,
        parser=parser,
        output_limit=output_limit,
        artifact_paths=artifact_paths,
    )


def test_deterministic_diagnostic_failure_is_reused_and_forceable(
    forge_env: ForgeEnvironment,
    tmp_path: Path,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "dedupe-diagnostic-failure")["workspace_id"]
    counter = tmp_path / "diagnostic-attempts.txt"
    script = (
        "from pathlib import Path; import sys; "
        f"p=Path({str(counter)!r}); "
        "p.write_text(str((int(p.read_text()) if p.exists() else 0)+1)); "
        "sys.exit(7)"
    )
    profile = _runtime_profile(
        "counting-failure",
        ("python3", "-c", script),
    )
    service.config.repositories["demo"].diagnostics[profile.diagnostic_id] = profile

    first = service.workspace_run_diagnostic(workspace_id, profile.diagnostic_id)
    assert first["outcome"] == "failed"
    assert first["failure_reused"] is False
    assert counter.read_text(encoding="utf-8") == "1"

    second = service.workspace_run_diagnostic(workspace_id, profile.diagnostic_id)
    assert second["outcome"] == "failed"
    assert second["failure_reused"] is True
    assert second["reuse_binding"]
    assert counter.read_text(encoding="utf-8") == "1"
    assert service.workspace_status(workspace_id)["last_verification"] is None

    forced = service.workspace_run_diagnostic(
        workspace_id,
        profile.diagnostic_id,
        force_rerun=True,
    )
    assert forced["failure_reused"] is False
    assert counter.read_text(encoding="utf-8") == "2"


def test_flaky_truncated_and_timeout_diagnostics_are_not_reused(
    forge_env: ForgeEnvironment,
    tmp_path: Path,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "diagnostic reuse exclusions")["workspace_id"]

    flaky_counter = tmp_path / "flaky-attempts.txt"
    flaky_script = (
        "from pathlib import Path; import sys; "
        f"p=Path({str(flaky_counter)!r}); "
        "p.write_text(str((int(p.read_text()) if p.exists() else 0)+1)); "
        "print('1 failed in 0.01s\\nFAILED demo.py::test_flaky'); sys.exit(1)"
    )
    flaky = _runtime_profile(
        "flaky-failure",
        ("python3", "-c", flaky_script),
        parser=DiagnosticParserKind.PYTEST,
    )
    service.config.repositories["demo"].diagnostics[flaky.diagnostic_id] = flaky
    for _ in range(2):
        result = service.workspace_run_diagnostic(workspace_id, flaky.diagnostic_id)
        assert result["failure_class"] == "test_failure"
        assert result["failure_reused"] is False
    assert flaky_counter.read_text(encoding="utf-8") == "2"

    truncated_counter = tmp_path / "truncated-attempts.txt"
    truncated_script = (
        "from pathlib import Path; import sys; "
        f"p=Path({str(truncated_counter)!r}); "
        "p.write_text(str((int(p.read_text()) if p.exists() else 0)+1)); "
        "print('x'*5000); sys.exit(1)"
    )
    truncated = _runtime_profile(
        "truncated-failure",
        ("python3", "-c", truncated_script),
        output_limit=100,
    )
    service.config.repositories["demo"].diagnostics[truncated.diagnostic_id] = truncated
    for _ in range(2):
        result = service.workspace_run_diagnostic(workspace_id, truncated.diagnostic_id)
        assert result["output_truncated"] is True
        assert result["failure_reused"] is False
    assert truncated_counter.read_text(encoding="utf-8") == "2"

    timeout_counter = tmp_path / "timeout-attempts.txt"
    timeout_script = (
        "from pathlib import Path; import time; "
        f"p=Path({str(timeout_counter)!r}); "
        "p.write_text(str((int(p.read_text()) if p.exists() else 0)+1)); "
        "time.sleep(2)"
    )
    timeout = _runtime_profile(
        "timeout-failure",
        ("python3", "-c", timeout_script),
        timeout_seconds=1,
    )
    service.config.repositories["demo"].diagnostics[timeout.diagnostic_id] = timeout
    for _ in range(2):
        with pytest.raises(RepoForgeError) as timed_out:
            service.workspace_run_diagnostic(workspace_id, timeout.diagnostic_id)
        assert timed_out.value.code is ErrorCode.DIAGNOSTIC_TIMEOUT
    assert timeout_counter.read_text(encoding="utf-8") == "2"


def test_workspace_diagnostic_evaluates_tdd_expectations(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "diagnostic expectations")
    workspace_id = created["workspace_id"]
    failing = _runtime_profile(
        "expected-test-failure",
        (
            "python3",
            "-c",
            "import sys; print('1 failed in 0.01s\\nFAILED demo.py::test_bad'); sys.exit(1)",
        ),
        parser=DiagnosticParserKind.PYTEST,
    )
    service.config.repositories["demo"].diagnostics[failing.diagnostic_id] = failing

    red = service.workspace_run_diagnostic(
        workspace_id,
        failing.diagnostic_id,
        intent="tdd_red",
        expectation="fail",
        expected_failure_class="test_failure",
    )
    assert red["intent"] == "tdd_red"
    assert red["expectation"] == "fail"
    assert red["expectation_met"] is True
    assert red["business_tests_ran"] is True
    assert red["valid_tdd_red_evidence"] is True

    mismatch = service.workspace_run_diagnostic(
        workspace_id,
        failing.diagnostic_id,
        intent="tdd_red",
        expectation="fail",
        expected_failure_class="collection_error",
    )
    assert mismatch["expectation_met"] is False
    assert mismatch["valid_tdd_red_evidence"] is False

    status = service.workspace_status(workspace_id)
    green = service.workspace_run_diagnostic(
        workspace_id,
        "pytest-target",
        selector="hello.txt::test_example",
        expected_fingerprint=status["workspace_fingerprint"],
        intent="tdd_green",
        expectation="pass",
    )
    assert green["expectation_met"] is True
    assert green["business_tests_ran"] is True
    assert green["valid_tdd_red_evidence"] is False


def test_workspace_diagnostic_rejects_invalid_expectation_inputs(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "invalid diagnostic expectation")
    workspace_id = created["workspace_id"]

    with pytest.raises(ConfigError, match="expected_failure_class"):
        service.workspace_run_diagnostic(
            workspace_id,
            "pytest-target",
            selector="hello.txt::test_example",
            expectation="pass",
            expected_failure_class="test_failure",
        )
    with pytest.raises(ConfigError, match="verification intent"):
        service.workspace_run_diagnostic(
            workspace_id,
            "pytest-target",
            selector="hello.txt::test_example",
            intent="unknown",
        )


def test_workspace_diagnostic_read_only_success_and_stale_guard(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "run diagnostic")
    workspace_id = created["workspace_id"]
    profile = _runtime_profile(
        "echo-diagnostic",
        ("python3", "-c", "print('diagnostic-ok')"),
    )
    service.config.repositories["demo"].diagnostics[profile.diagnostic_id] = profile
    status = service.workspace_status(workspace_id)

    result = service.workspace_run_diagnostic(
        workspace_id,
        profile.diagnostic_id,
        expected_fingerprint=status["workspace_fingerprint"],
    )
    assert result["outcome"] == "passed"
    assert result["excerpt"] == "diagnostic-ok"
    assert result["fingerprint_changed"] is False
    assert result["fingerprint_before"] == result["fingerprint_after"]
    assert result["argv"] == ["python3", "-c", "print('diagnostic-ok')"]
    assert result["satisfies_commit_gate"] is False

    marker_profile = _runtime_profile(
        "stale-marker",
        (
            "python3",
            "-c",
            "from pathlib import Path; Path('stale-marker.txt').write_text('ran')",
        ),
        mutability=DiagnosticMutability.ARTIFACTS,
        artifact_paths=("stale-marker.txt",),
    )
    service.config.repositories["demo"].diagnostics[marker_profile.diagnostic_id] = marker_profile
    with pytest.raises(RepoForgeError) as stale:
        service.workspace_run_diagnostic(
            workspace_id,
            marker_profile.diagnostic_id,
            expected_fingerprint="0" * 64,
        )
    assert stale.value.code is ErrorCode.DIAGNOSTIC_STALE_WORKSPACE
    assert not Path(created["path"]).joinpath("stale-marker.txt").exists()


def test_read_only_mutation_fails_and_reports_changed_paths(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "diagnostic mutation")
    workspace_id = created["workspace_id"]
    profile = _runtime_profile(
        "bad-read-only",
        (
            "python3",
            "-c",
            "from pathlib import Path; Path('unexpected.txt').write_text('changed')",
        ),
    )
    service.config.repositories["demo"].diagnostics[profile.diagnostic_id] = profile
    with pytest.raises(RepoForgeError) as mutation:
        service.workspace_run_diagnostic(workspace_id, profile.diagnostic_id)
    assert mutation.value.code is ErrorCode.DIAGNOSTIC_UNEXPECTED_MUTATION
    assert "unexpected.txt" in str(mutation.value)
    assert Path(created["path"]).joinpath("unexpected.txt").exists()


def test_artifact_diagnostic_invalidates_verification_and_enforces_patterns(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "artifact diagnostic")
    workspace_id = created["workspace_id"]
    record = service.state.load(workspace_id)
    record.last_verification = VerificationReceipt(
        "full", "a" * 64, "2026-07-14T00:00:00+00:00", []
    )
    service.state.save(record)

    artifact = _runtime_profile(
        "coverage-artifact",
        (
            "python3",
            "-c",
            "from pathlib import Path; Path('artifacts').mkdir(exist_ok=True); Path('artifacts/report.txt').write_text('ok')",
        ),
        mutability=DiagnosticMutability.ARTIFACTS,
        artifact_paths=("artifacts/**",),
    )
    service.config.repositories["demo"].diagnostics[artifact.diagnostic_id] = artifact
    result = service.workspace_run_diagnostic(workspace_id, artifact.diagnostic_id)
    assert result["outcome"] == "passed"
    assert result["fingerprint_changed"] is True
    assert result["verification_invalidated"] is True
    assert result["changed_paths"] == ["artifacts/report.txt"]
    assert result["unexpected_paths"] == []
    assert service.state.load(workspace_id).last_verification is None

    forbidden = _runtime_profile(
        "wrong-artifact",
        (
            "python3",
            "-c",
            "from pathlib import Path; Path('outside.txt').write_text('bad')",
        ),
        mutability=DiagnosticMutability.ARTIFACTS,
        artifact_paths=("artifacts/**",),
    )
    service.config.repositories["demo"].diagnostics[forbidden.diagnostic_id] = forbidden
    with pytest.raises(RepoForgeError) as outside:
        service.workspace_run_diagnostic(workspace_id, forbidden.diagnostic_id)
    assert outside.value.code is ErrorCode.DIAGNOSTIC_UNEXPECTED_MUTATION
    assert "outside.txt" in str(outside.value)


def test_diagnostic_missing_tool_timeout_parser_failure_and_output_bound(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "diagnostic failures")
    workspace_id = created["workspace_id"]
    repo = service.config.repositories["demo"]

    missing = _runtime_profile("missing-tool", ("definitely-not-installed-xyz",))
    repo.diagnostics[missing.diagnostic_id] = missing
    with pytest.raises(RepoForgeError) as tool:
        service.workspace_run_diagnostic(workspace_id, missing.diagnostic_id)
    assert tool.value.code is ErrorCode.DIAGNOSTIC_TOOL_MISSING

    timeout = _runtime_profile(
        "timeout-diagnostic",
        ("python3", "-c", "import time; time.sleep(2)"),
        timeout_seconds=1,
    )
    repo.diagnostics[timeout.diagnostic_id] = timeout
    with pytest.raises(RepoForgeError) as timed_out:
        service.workspace_run_diagnostic(workspace_id, timeout.diagnostic_id)
    assert timed_out.value.code is ErrorCode.DIAGNOSTIC_TIMEOUT

    malformed = _runtime_profile(
        "malformed-contract",
        ("python3", "-c", "print('not a release contract result')"),
        parser=DiagnosticParserKind.RELEASE_CONTRACT,
    )
    repo.diagnostics[malformed.diagnostic_id] = malformed
    with pytest.raises(RepoForgeError) as parser:
        service.workspace_run_diagnostic(workspace_id, malformed.diagnostic_id)
    assert parser.value.code is ErrorCode.DIAGNOSTIC_PARSER_FAILED

    oversized = _runtime_profile(
        "bounded-output",
        ("python3", "-c", "print('x' * 5000)"),
        output_limit=100,
    )
    repo.diagnostics[oversized.diagnostic_id] = oversized
    bounded = service.workspace_run_diagnostic(workspace_id, oversized.diagnostic_id)
    assert bounded["outcome"] == "passed"
    assert bounded["output_truncated"] is True
    assert len(bounded["excerpt"]) < 500


def test_repo_list_exposes_safe_diagnostic_metadata(forge_env: ForgeEnvironment) -> None:
    repository = forge_env.service.repo_list()["repositories"][0]
    diagnostic = repository["diagnostics"]["pytest-target"]
    assert diagnostic == {
        "summary": "Run one tracked pytest target",
        "selector_kind": "pytest_node",
        "mutability": "read_only",
        "parser": "pytest",
        "network_policy": "local_only",
        "timeout_seconds": 30,
        "output_limit": 2000,
        "artifact_paths": [],
    }
    assert "argv" not in diagnostic


def test_doctor_checks_diagnostic_executable_without_running_it(
    forge_env: ForgeEnvironment,
) -> None:
    checks = forge_env.service.doctor()["checks"]
    diagnostic = next(
        check
        for check in checks
        if check["name"] == "diagnostic_executable:demo:pytest-target:python3"
    )
    assert diagnostic["ok"] is True
