from __future__ import annotations

import io
import json
import os
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, git

from repoforge.adapters.hygiene import CommandHygieneGateway
from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.json_hygiene_cache import JsonHygieneBaselineCache
from repoforge.adapters.subprocess import SubprocessCommandExecutor
from repoforge.application.configuration.document import render_resolved
from repoforge.application.service import CodingService
from repoforge.config import ServerConfig, load_config
from repoforge.domain.config_generation import CapabilityDeltaKind, classify_capability_delta
from repoforge.domain.errors import ConfigError, ErrorCode, RepoForgeError, SecurityError
from repoforge.domain.hygiene import (
    FormatterPolicy,
    HygieneFinding,
    HygieneNetworkPolicy,
    HygieneParserKind,
    compare_hygiene_findings,
)
from repoforge.domain.workspace import VerificationReceipt
from repoforge.ports.command import CommandResult
from repoforge.ports.hygiene import HygieneCacheKey


def _formatter_table(*, summary: str = "Format Python files", check: str = "--check") -> str:
    return f'''[repositories.demo.formatters.ruff-format]
summary = "{summary}"
check_argv = ["uv", "run", "ruff", "format", "{check}", "--"]
fix_argv = ["uv", "run", "ruff", "format", "--"]
include_globs = ["**/*.py"]
timeout_seconds = 120
output_limit = 12000
max_paths = 80
baseline_cache_ttl_seconds = 3600
network_policy = "local_only"
parser = "ruff_format"
'''


def _write_config(tmp_path: Path, formatter: str = "") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(
        f'''[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"

[repositories.demo]
path = "{repo}"

{formatter}
''',
        encoding="utf-8",
    )
    return config


def _document(repo: Path, formatter: dict[str, object] | None = None) -> dict[str, object]:
    repository: dict[str, object] = {"path": str(repo)}
    if formatter is not None:
        repository["formatters"] = {"ruff-format": formatter}
    return {
        "server": {
            "workspace_root": str(repo.parent / "workspaces"),
            "state_root": str(repo.parent / "state"),
        },
        "repositories": {"demo": repository},
    }


def _formatter_document(*, summary: str = "Format Python files") -> dict[str, object]:
    return {
        "summary": summary,
        "check_argv": ["uv", "run", "ruff", "format", "--check", "--"],
        "fix_argv": ["uv", "run", "ruff", "format", "--"],
        "include_globs": ["**/*.py"],
        "timeout_seconds": 120,
        "output_limit": 12000,
        "max_paths": 80,
        "baseline_cache_ttl_seconds": 3600,
        "network_policy": "local_only",
        "parser": "ruff_format",
    }


def _render(document: dict[str, object]) -> str:
    return render_resolved(
        document,
        generation=1,
        source_path="config.toml",
        source_sha256="a" * 64,
        created_at="2026-07-16T00:00:00+00:00",
        reason="test",
        proposal_id=None,
        repository_fingerprints=(("demo", "b" * 64),),
    )


def test_hygiene_finding_identity_is_normalized_and_deterministic() -> None:
    first = HygieneFinding.create(
        path=r"src\repoforge\demo.py",
        rule=" ruff-format ",
        message="  Would   reformat  ",
    )
    second = HygieneFinding.create(
        path="src/repoforge/demo.py",
        rule="ruff-format",
        message="Would reformat",
    )

    assert first == second
    assert first.path == "src/repoforge/demo.py"
    assert first.identity == second.identity
    assert len(first.identity) == 64


def test_hygiene_comparison_partitions_base_workspace_and_changed_paths() -> None:
    shared = HygieneFinding.create("src/shared.py", "ruff-format", "Would reformat")
    base_only = HygieneFinding.create("src/resolved.py", "ruff-format", "Would reformat")
    workspace_only = HygieneFinding.create("src/new.py", "ruff-format", "Would reformat")
    unchanged_workspace = HygieneFinding.create("src/unchanged.py", "ruff-format", "Would reformat")

    comparison = compare_hygiene_findings(
        base=(shared, base_only),
        workspace=(shared, workspace_only, unchanged_workspace),
        changed_paths=("src/new.py",),
    )

    assert comparison.preexisting == (shared,)
    assert comparison.introduced == (workspace_only, unchanged_workspace)
    assert comparison.resolved == (base_only,)
    assert comparison.changed_path_findings == (workspace_only,)


def test_formatter_policy_contract_hash_excludes_summary_but_binds_authority() -> None:
    policy = FormatterPolicy(
        formatter_id="ruff-format",
        summary="Format Python files",
        check_argv=("uv", "run", "ruff", "format", "--check", "--"),
        fix_argv=("uv", "run", "ruff", "format", "--"),
        include_globs=("**/*.py",),
        timeout_seconds=120,
        output_limit=12000,
        max_paths=80,
        baseline_cache_ttl_seconds=3600,
        network_policy=HygieneNetworkPolicy.LOCAL_ONLY,
        parser=HygieneParserKind.RUFF_FORMAT,
    )
    renamed = replace(policy, summary="Reviewed formatter")
    widened = replace(policy, max_paths=81)

    assert policy.contract_hash == renamed.contract_hash
    assert policy.contract_hash != widened.contract_hash


def test_loads_typed_formatter_policy_and_defaults_to_none(tmp_path: Path) -> None:
    loaded = load_config(_write_config(tmp_path, _formatter_table()))
    formatter = loaded.repositories["demo"].formatters["ruff-format"]

    assert formatter.check_argv == ("uv", "run", "ruff", "format", "--check", "--")
    assert formatter.fix_argv == ("uv", "run", "ruff", "format", "--")
    assert formatter.include_globs == ("**/*.py",)
    assert formatter.network_policy is HygieneNetworkPolicy.LOCAL_ONLY
    assert formatter.parser is HygieneParserKind.RUFF_FORMAT
    assert formatter.max_paths == 80
    assert formatter.baseline_cache_ttl_seconds == 3600

    no_formatter = load_config(_write_config(tmp_path / "other"))
    assert no_formatter.repositories["demo"].formatters == {}


@pytest.mark.parametrize(
    ("formatter", "message"),
    [
        (
            _formatter_table().replace(
                'check_argv = ["uv", "run", "ruff", "format", "--check", "--"]',
                'check_argv = ["uv", "run", "ruff", "format", "{paths}"]',
            ),
            "placeholder",
        ),
        (
            _formatter_table().replace(
                'fix_argv = ["uv", "run", "ruff", "format", "--"]',
                'fix_argv = ["sh", "-c", "ruff format"]',
            ),
            "shell",
        ),
        (
            _formatter_table().replace(
                'include_globs = ["**/*.py"]', 'include_globs = ["/tmp/*.py"]'
            ),
            "repository-relative",
        ),
        (
            _formatter_table().replace("max_paths = 80", "max_paths = 0"),
            "max_paths",
        ),
        (
            _formatter_table().replace('parser = "ruff_format"', 'parser = "unknown"'),
            "parser",
        ),
    ],
)
def test_rejects_unsafe_or_unbounded_formatter_policy(
    tmp_path: Path,
    formatter: str,
    message: str,
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(_write_config(tmp_path, formatter))


def test_resolved_config_round_trips_formatters_deterministically(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    rendered = _render(_document(repo, _formatter_document()))

    assert "[repositories.demo.formatters.ruff-format]" in rendered
    assert 'check_argv = ["uv", "run", "ruff", "format", "--check", "--"]' in rendered
    config = tmp_path / "resolved.toml"
    config.write_text(rendered, encoding="utf-8")
    loaded = load_config(config)
    assert loaded.repositories["demo"].formatters["ruff-format"].contract_hash
    assert _render(_document(repo, _formatter_document())) == rendered


def test_formatter_capability_delta_is_semantic(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _render(_document(repo))
    added = _render(_document(repo, _formatter_document()))

    assert classify_capability_delta(base, added).kind is CapabilityDeltaKind.EXPANSION
    assert classify_capability_delta(added, base).kind is CapabilityDeltaKind.RESTRICTION

    command_changed = added.replace('"--check", "--"]', '"--diff", "--"]')
    assert (
        classify_capability_delta(added, command_changed).kind is CapabilityDeltaKind.INCOMPATIBLE
    )
    scope_changed = added.replace('include_globs = ["**/*.py"]', 'include_globs = ["src/**/*.py"]')
    assert classify_capability_delta(added, scope_changed).kind is CapabilityDeltaKind.INCOMPATIBLE
    metadata = added.replace('summary = "Format Python files"', 'summary = "Reviewed formatter"')
    assert classify_capability_delta(added, metadata).kind is CapabilityDeltaKind.METADATA_ONLY
    widened = added.replace("max_paths = 80", "max_paths = 81")
    assert classify_capability_delta(added, widened).kind is CapabilityDeltaKind.EXPANSION


def test_repo_list_exposes_safe_formatter_metadata_without_argv(tmp_path: Path) -> None:
    config = _write_config(tmp_path, _formatter_table())
    repository = CodingService(load_config(config)).repo_list()["repositories"][0]
    formatter = repository["formatters"]["ruff-format"]

    assert formatter == {
        "summary": "Format Python files",
        "include_globs": ["**/*.py"],
        "timeout_seconds": 120,
        "output_limit": 12000,
        "max_paths": 80,
        "baseline_cache_ttl_seconds": 3600,
        "network_policy": "local_only",
        "parser": "ruff_format",
        "contract_hash": formatter["contract_hash"],
    }
    assert len(formatter["contract_hash"]) == 64
    assert "check_argv" not in formatter
    assert "fix_argv" not in formatter


def _runtime_formatter() -> FormatterPolicy:
    check_script = (
        "import sys; from pathlib import Path; "
        "bad=[p for p in sys.argv[1:] if 'bad' in Path(p).read_text()]; "
        "[print('Would reformat: ' + p) for p in bad]; raise SystemExit(1 if bad else 0)"
    )
    fix_script = (
        "import sys; from pathlib import Path; "
        "[(lambda p: p.write_text(p.read_text().replace('bad', 'good')))(Path(x)) "
        "for x in sys.argv[1:]]"
    )
    return FormatterPolicy(
        formatter_id="ruff-format",
        summary="Format Python files",
        check_argv=("python3", "-c", check_script),
        fix_argv=("python3", "-c", fix_script),
        include_globs=("**/*.py",),
        timeout_seconds=30,
        output_limit=4000,
        max_paths=100,
        baseline_cache_ttl_seconds=3600,
        network_policy=HygieneNetworkPolicy.LOCAL_ONLY,
        parser=HygieneParserKind.RUFF_FORMAT,
    )


def _command_gateway(tmp_path: Path) -> CommandHygieneGateway:
    server = ServerConfig(
        workspace_root=tmp_path / "workspaces",
        state_root=tmp_path / "state",
        path_prefixes=("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"),
    )
    return CommandHygieneGateway(SubprocessCommandExecutor(server))


def test_exact_base_inspection_uses_commit_archive_and_ignores_dirty_clone(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", cwd=repo)
    git("config", "user.name", "Test User", cwd=repo)
    git("config", "user.email", "test@example.com", cwd=repo)
    source = repo / "demo.py"
    source.write_text("bad\n", encoding="utf-8")
    git("add", "demo.py", cwd=repo)
    git("commit", "-m", "baseline", cwd=repo)
    base_sha = git("rev-parse", "HEAD", cwd=repo)
    source.write_text("good\n", encoding="utf-8")

    gateway = _command_gateway(tmp_path)
    base = gateway.inspect_base(
        repo,
        base_sha,
        _runtime_formatter(),
        ("demo.py",),
        max_archive_bytes=1_000_000,
    )
    workspace = gateway.inspect_workspace(repo, _runtime_formatter(), ("demo.py",))

    assert [finding.path for finding in base.findings] == ["demo.py"]
    assert workspace.findings == ()
    assert source.read_text(encoding="utf-8") == "good\n"
    assert git("status", "--porcelain", cwd=repo) == "M demo.py"


class _ArchiveExecutor:
    def __init__(self, archive: bytes) -> None:
        self.archive = archive

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {}

    def run(self, *args: object, **kwargs: object) -> CommandResult:
        raise AssertionError("unsafe archive must fail before formatter execution")

    def run_bytes(self, *args: object, **kwargs: object) -> bytes:
        return self.archive


def _tar_entry(name: str, *, entry_type: bytes = tarfile.REGTYPE) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.type = entry_type
        if entry_type == tarfile.REGTYPE:
            data = b"bad\n"
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        else:
            info.linkname = "target.py"
            archive.addfile(info)
    return stream.getvalue()


@pytest.mark.parametrize(
    ("name", "entry_type"),
    [
        ("/absolute.py", tarfile.REGTYPE),
        ("../escape.py", tarfile.REGTYPE),
        ("linked.py", tarfile.SYMTYPE),
        ("hard.py", tarfile.LNKTYPE),
        ("device.py", tarfile.CHRTYPE),
    ],
)
def test_exact_base_archive_rejects_unsafe_or_non_regular_entries(
    tmp_path: Path,
    name: str,
    entry_type: bytes,
) -> None:
    gateway = CommandHygieneGateway(_ArchiveExecutor(_tar_entry(name, entry_type=entry_type)))

    with pytest.raises(SecurityError):
        gateway.inspect_base(
            tmp_path,
            "a" * 40,
            _runtime_formatter(),
            ("safe.py",),
            max_archive_bytes=1_000_000,
        )


def _cache_key() -> HygieneCacheKey:
    return HygieneCacheKey(
        repo_id="demo",
        base_sha="a" * 40,
        config_identity="b" * 64,
        environment_identity="c" * 64,
        formatter_contract_hash="d" * 64,
        ttl_seconds=3600,
    )


def test_hygiene_cache_binds_every_identity_dimension_and_expiry(tmp_path: Path) -> None:
    cache = JsonHygieneBaselineCache(tmp_path, FcntlLockManager(tmp_path / "locks"))
    finding = HygieneFinding.create("demo.py", "ruff-format", "Would reformat")
    key = _cache_key()
    cache.put(key, (finding,), now_epoch=1000.0)

    assert cache.get(key, now_epoch=1001.0) == (finding,)
    for changed in (
        replace(key, repo_id="other"),
        replace(key, base_sha="e" * 40),
        replace(key, config_identity="e" * 64),
        replace(key, environment_identity="e" * 64),
        replace(key, formatter_contract_hash="e" * 64),
        replace(key, ttl_seconds=7200),
    ):
        assert cache.get(changed, now_epoch=1001.0) is None
    assert cache.get(key, now_epoch=4601.0) is None
    assert os.stat(cache._path).st_mode & 0o777 == 0o600


def test_hygiene_cache_corruption_or_future_schema_is_a_miss(tmp_path: Path) -> None:
    cache = JsonHygieneBaselineCache(tmp_path, FcntlLockManager(tmp_path / "locks"))
    finding = HygieneFinding.create("demo.py", "ruff-format", "Would reformat")
    key = _cache_key()
    cache.put(key, (finding,), now_epoch=1000.0)
    document = json.loads(cache._path.read_text(encoding="utf-8"))
    entry = next(iter(document["entries"].values()))
    entry["frame"]["findings"][0]["message"] = "tampered"
    cache._path.write_text(json.dumps(document), encoding="utf-8")
    assert cache.get(key, now_epoch=1001.0) is None

    document["version"] = 99
    cache._path.write_text(json.dumps(document), encoding="utf-8")
    assert cache.get(key, now_epoch=1001.0) is None


def _write_workspace_text(
    forge_env: ForgeEnvironment,
    workspace_id: str,
    path: str,
    content: str,
) -> dict[str, object]:
    current = forge_env.service.workspace_read_file(workspace_id, path)
    return forge_env.service.workspace_write_file(
        workspace_id,
        path,
        content,
        str(current["sha256"]),
    )


def test_workspace_hygiene_status_distinguishes_introduced_findings_and_uses_cache(
    forge_env: ForgeEnvironment,
) -> None:
    created = forge_env.service.workspace_create("demo", "hygiene status")
    workspace_id = created["workspace_id"]
    _write_workspace_text(forge_env, workspace_id, "hello.txt", "needs-format\n")

    first = forge_env.service.workspace_hygiene_status(workspace_id)
    second = forge_env.service.workspace_hygiene_status(workspace_id)

    assert first["status"] == "available"
    assert first["formatter_id"] == "test-format"
    assert first["preexisting"] == []
    assert [item["path"] for item in first["introduced"]] == ["hello.txt"]
    assert [item["path"] for item in first["changed_path_findings"]] == ["hello.txt"]
    assert first["base_cache_hit"] is False
    assert second["base_cache_hit"] is True
    assert second["workspace_fingerprint"] == first["workspace_fingerprint"]

    events = [
        json.loads(line)
        for line in (forge_env.root / "state" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    audit = [item for item in events if item["action"] == "workspace_hygiene_status"][-1]
    assert audit["details"]["introduced_count"] == 1
    assert audit["details"]["changed_path_finding_count"] == 1
    assert "hello.txt" not in json.dumps(audit["details"], sort_keys=True)


def test_workspace_hygiene_status_is_explicitly_unavailable_without_policy(
    forge_env: ForgeEnvironment,
) -> None:
    forge_env.service.config.repositories["demo"].formatters.clear()
    created = forge_env.service.workspace_create("demo", "no hygiene policy")

    result = forge_env.service.workspace_hygiene_status(created["workspace_id"])

    assert result["status"] == "unavailable"
    assert result["reason"] == "no_reviewed_formatter"
    assert result["next_safe_actions"][0]["action"] == "propose_formatter_policy"


def test_workspace_format_changed_mutates_only_selected_changed_paths_and_invalidates_receipt(
    forge_env: ForgeEnvironment,
) -> None:
    (forge_env.source / "unchanged.txt").write_text("needs-format\n", encoding="utf-8")
    git("add", "unchanged.txt", cwd=forge_env.source)
    git("commit", "-m", "add unchanged fixture", cwd=forge_env.source)
    git("push", cwd=forge_env.source)
    created = forge_env.service.workspace_create("demo", "format changed")
    workspace_id = created["workspace_id"]
    _write_workspace_text(forge_env, workspace_id, "hello.txt", "needs-format\n")
    status = forge_env.service.workspace_status(workspace_id)
    record = forge_env.service.state.load(workspace_id)
    record.last_verification = VerificationReceipt(
        "full",
        status["workspace_fingerprint"],
        "2026-07-16T00:00:00+00:00",
        [],
    )
    forge_env.service.state.save(record)

    result = forge_env.service.workspace_format_changed(
        workspace_id,
        status["workspace_fingerprint"],
    )

    assert result["selected_paths"] == ["hello.txt"]
    assert result["modified_paths"] == ["hello.txt"]
    assert result["fingerprint_changed"] is True
    assert result["verification_invalidated"] is True
    assert forge_env.service.state.load(workspace_id).last_verification is None
    assert Path(created["path"]).joinpath("hello.txt").read_text() == "formatted\n"
    assert Path(created["path"]).joinpath("unchanged.txt").read_text() == "needs-format\n"

    events = [
        json.loads(line)
        for line in (forge_env.root / "state" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    audit = [item for item in events if item["action"] == "workspace_format_changed"][-1]
    assert "selected_paths" not in audit["details"]
    assert audit["details"]["selected_path_count"] == 1
    assert len(audit["details"]["selected_paths_digest"]) == 64


def test_workspace_format_changed_noop_preserves_fingerprint_and_receipt(
    forge_env: ForgeEnvironment,
) -> None:
    created = forge_env.service.workspace_create("demo", "format noop")
    workspace_id = created["workspace_id"]
    _write_workspace_text(forge_env, workspace_id, "hello.txt", "already formatted\n")
    status = forge_env.service.workspace_status(workspace_id)
    record = forge_env.service.state.load(workspace_id)
    record.last_verification = VerificationReceipt(
        "full",
        status["workspace_fingerprint"],
        "2026-07-16T00:00:00+00:00",
        [],
    )
    forge_env.service.state.save(record)

    result = forge_env.service.workspace_format_changed(
        workspace_id,
        status["workspace_fingerprint"],
    )

    assert result["selected_paths"] == ["hello.txt"]
    assert result["modified_paths"] == []
    assert result["fingerprint_changed"] is False
    assert result["verification_invalidated"] is False
    assert forge_env.service.state.load(workspace_id).last_verification is not None


def test_workspace_format_changed_rejects_stale_fingerprint_before_execution(
    forge_env: ForgeEnvironment,
) -> None:
    created = forge_env.service.workspace_create("demo", "format stale")
    workspace_id = created["workspace_id"]
    _write_workspace_text(forge_env, workspace_id, "hello.txt", "needs-format\n")

    with pytest.raises(RepoForgeError) as stale:
        forge_env.service.workspace_format_changed(workspace_id, "0" * 64)

    assert stale.value.code is ErrorCode.STALE_STATE
    assert Path(created["path"]).joinpath("hello.txt").read_text() == "needs-format\n"


def test_workspace_format_changed_keeps_space_path_as_one_argument(
    forge_env: ForgeEnvironment,
) -> None:
    special = forge_env.source / "space name.txt"
    special.write_text("clean\n", encoding="utf-8")
    git("add", "space name.txt", cwd=forge_env.source)
    git("commit", "-m", "add spaced fixture", cwd=forge_env.source)
    git("push", cwd=forge_env.source)
    created = forge_env.service.workspace_create("demo", "format spaced path")
    workspace_id = created["workspace_id"]
    _write_workspace_text(forge_env, workspace_id, "space name.txt", "needs-format\n")
    status = forge_env.service.workspace_status(workspace_id)

    result = forge_env.service.workspace_format_changed(
        workspace_id,
        status["workspace_fingerprint"],
    )

    assert result["selected_paths"] == ["space name.txt"]
    assert Path(created["path"]).joinpath("space name.txt").read_text() == "formatted\n"


def test_workspace_format_changed_detects_unexpected_formatter_mutation(
    forge_env: ForgeEnvironment,
) -> None:
    repo = forge_env.service.config.repositories["demo"]
    policy = repo.formatters["test-format"]
    repo.formatters["test-format"] = replace(
        policy,
        fix_argv=(
            "python3",
            "-c",
            "from pathlib import Path; Path('README.md').write_text('unexpected'); "
            "import sys; [(lambda p: p.write_text('formatted'))(Path(x)) for x in sys.argv[1:]]",
        ),
    )
    created = forge_env.service.workspace_create("demo", "unexpected formatter mutation")
    workspace_id = created["workspace_id"]
    _write_workspace_text(forge_env, workspace_id, "hello.txt", "needs-format\n")
    status = forge_env.service.workspace_status(workspace_id)
    record = forge_env.service.state.load(workspace_id)
    record.last_verification = VerificationReceipt(
        "full",
        status["workspace_fingerprint"],
        "2026-07-16T00:00:00+00:00",
        [],
    )
    forge_env.service.state.save(record)

    with pytest.raises(SecurityError, match="outside"):
        forge_env.service.workspace_format_changed(
            workspace_id,
            status["workspace_fingerprint"],
        )

    assert forge_env.service.state.load(workspace_id).last_verification is None
    assert Path(created["path"]).joinpath("README.md").read_text() == "unexpected"
