from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

import pytest

from repoforge.adapters.git.transport import GitTransportRouter
from repoforge.adapters.subprocess import SubprocessCommandExecutor
from repoforge.config import ServerConfig
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.git_remote_identity import (
    ReviewedSshEndpoint,
    SshAliasDefinition,
    SshKeyProof,
    SshPrincipalProof,
)
from repoforge.domain.git_transport_identity import (
    GitTransportAccess,
    GitTransportActorEvidence,
    GitTransportKind,
    GitTransportSpec,
)
from repoforge.domain.repository_auth_broker import ProcessAuthContext
from repoforge.domain.repository_identity import AuthTargetKind
from repoforge.ports.command import CommandResult

_SHA = "a" * 64
_PERSONAL_TOKEN = "personal-transport-token-111"
_COMPANY_TOKEN = "company-transport-token-222"
_NOW = "2026-08-01T00:00:00+00:00"


class TransportExecutor:
    def __init__(self, results: list[CommandResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []
        self._lock = Lock()

    def run_isolated(self, argv: list[str], **kwargs: Any) -> CommandResult:
        with self._lock:
            self.calls.append({"argv": tuple(argv), **kwargs})
            if self.results:
                return self.results.pop(0)
        return CommandResult(tuple(argv), str(kwargs["cwd"]), 0, "", "")


class FakeIdentityMaterial:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeIdentityMaterial:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class FakeMaterials:
    def __init__(self, material: FakeIdentityMaterial) -> None:
        self.material = material
        self.keys: list[SshKeyProof] = []

    def open_verified(self, expected: SshKeyProof) -> FakeIdentityMaterial:
        self.keys.append(expected)
        return self.material


class FakeEndpointRevalidator:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, ReviewedSshEndpoint]] = []

    def revalidate(
        self,
        *,
        cwd: Path,
        raw_remote_url: str,
        expected: ReviewedSshEndpoint,
    ) -> ReviewedSshEndpoint:
        self.calls.append((cwd, raw_remote_url, expected))
        return expected


def _reviewed_endpoint(
    raw_url: str = "git@github-work:acme/widgets.git",
) -> ReviewedSshEndpoint:
    fingerprint = "SHA256:" + "A" * 43
    alias = SshAliasDefinition(
        alias="github-work",
        canonical_host="github.com",
        user="git",
        port=22,
        identity_file="/home/demo/.ssh/id_rsa_work",
        source_config_digest="b" * 64,
        selected_block_digest="c" * 64,
    )
    key = SshKeyProof(
        canonical_path=alias.identity_file,
        public_key_fingerprint=fingerprint,
        owner_uid=501,
        mode=0o600,
        observed_at=_NOW,
    )
    principal = SshPrincipalProof(
        provider_host="github.com",
        principal_kind="github_account",
        principal_login="company-user",
        expected_actor_id="123456",
        key_fingerprint=fingerprint,
        observed_at=_NOW,
        proof_digest="d" * 64,
    )
    return ReviewedSshEndpoint(
        schema_version=1,
        raw_host="github-work",
        canonical_host="github.com",
        user="git",
        port=22,
        owner="acme",
        repository="widgets",
        raw_url_digest=hashlib.sha256(raw_url.encode("utf-8")).hexdigest(),
        alias=alias,
        key=key,
        principal=principal,
        proof_digest="e" * 64,
    )


def _context(
    *,
    profile_id: str = "company",
    target_id: str = "github-repository-123456",
    token: str | None = None,
) -> ProcessAuthContext:
    environment = [
        ("HOME", "/home/demo"),
        ("PATH", "/safe/bin"),
        ("SSH_AUTH_SOCK", "/tmp/wrong-agent.sock"),
        ("SSH_AGENT_PID", "999"),
        ("GIT_SSH_COMMAND", "ssh -i /tmp/wrong"),
        ("GIT_ASKPASS", "/tmp/wrong-helper"),
        ("GIT_CONFIG_COUNT", "1"),
        ("GIT_CONFIG_KEY_0", "credential.helper"),
        ("GIT_CONFIG_VALUE_0", "personal-helper"),
    ]
    secrets: tuple[str, ...] = ()
    if token is not None:
        environment.append(("REPOFORGE_GIT_HTTPS_TOKEN", token))
        secrets = (token,)
    return ProcessAuthContext(
        profile_id=profile_id,
        material_id=f"material-{profile_id}",
        target_kind=AuthTargetKind.REPOSITORY,
        target_id=target_id,
        environment=tuple(environment),
        _secret_values=secrets,
    )


def _ssh_spec(
    *,
    profile_id: str = "company",
    repository_id: str = "123456",
    target_id: str = "github-repository-123456",
    access: tuple[GitTransportAccess, ...] = (
        GitTransportAccess.READ,
        GitTransportAccess.WRITE,
    ),
    identity_file: str = "/run/repoforge/identity-company",
    endpoint: ReviewedSshEndpoint | None = None,
) -> GitTransportSpec:
    return GitTransportSpec(
        profile_id=profile_id,
        repository_id=repository_id,
        target_id=target_id,
        provider_host="github.com",
        kind=GitTransportKind.SSH,
        credential_fingerprint=_SHA,
        allowed_access=access,
        ssh_identity_file=None if endpoint is not None else identity_file,
        ssh_endpoint=endpoint,
    )


def _https_spec(
    *,
    profile_id: str = "company",
    repository_id: str = "123456",
    target_id: str = "github-repository-123456",
    access: tuple[GitTransportAccess, ...] = (
        GitTransportAccess.READ,
        GitTransportAccess.WRITE,
    ),
) -> GitTransportSpec:
    return GitTransportSpec(
        profile_id=profile_id,
        repository_id=repository_id,
        target_id=target_id,
        provider_host="github.com",
        kind=GitTransportKind.HTTPS,
        credential_fingerprint=_SHA,
        allowed_access=access,
        https_token_environment="REPOFORGE_GIT_HTTPS_TOKEN",
    )


def test_ssh_transport_revalidates_canonicalizes_and_cleans_material(
    tmp_path: Path,
) -> None:
    raw_remote = "git@github-work:acme/widgets.git"
    endpoint = _reviewed_endpoint(raw_remote)
    material_path = tmp_path / "operation-key"
    material_path.write_text("operation scoped key", encoding="utf-8")
    material = FakeIdentityMaterial(material_path)
    materials = FakeMaterials(material)
    revalidator = FakeEndpointRevalidator()
    result = CommandResult(
        ("git",),
        "/repo",
        0,
        "0123456789abcdef0123456789abcdef01234567\trefs/heads/main\n",
        "",
    )
    executor = TransportExecutor([result])

    evidence = GitTransportRouter(
        executor,
        endpoint_revalidator=revalidator,
        identity_materials=materials,
    ).ls_remote(
        Path("/repo"),
        raw_remote,
        "refs/heads/main",
        _ssh_spec(endpoint=endpoint),
        _context(),
    )

    call = executor.calls[0]
    assert call["argv"][2] == endpoint.canonical_url()
    environment = call["environment"]
    ssh_command = environment["GIT_SSH_COMMAND"]
    assert "IdentitiesOnly=yes" in ssh_command
    assert "IdentityAgent=none" in ssh_command
    assert "BatchMode=yes" in ssh_command
    assert "-F /dev/null" in ssh_command
    assert str(material_path) in ssh_command
    assert "SSH_AUTH_SOCK" not in environment
    assert "SSH_AGENT_PID" not in environment
    assert "/tmp/wrong" not in ssh_command
    assert revalidator.calls == [(Path("/repo"), raw_remote, endpoint)]
    assert materials.keys == [endpoint.key]
    assert material.closed is True
    assert evidence.access is GitTransportAccess.READ
    assert evidence.actor_evidence is GitTransportActorEvidence.UNOBSERVABLE
    assert evidence.observed_sha == "0123456789abcdef0123456789abcdef01234567"


def test_https_transport_clears_helpers_and_never_puts_token_in_url_or_argv() -> None:
    executor = TransportExecutor()
    router = GitTransportRouter(executor)

    router.fetch(
        Path("/repo"),
        "https://github.com/acme/widgets.git",
        "+refs/heads/main:refs/remotes/origin/main",
        _https_spec(),
        _context(token=_COMPANY_TOKEN),
    )

    call = executor.calls[0]
    environment = call["environment"]
    assert call["secrets"] == (_COMPANY_TOKEN,)
    assert _COMPANY_TOKEN not in " ".join(call["argv"])
    assert _COMPANY_TOKEN not in call["argv"][2]
    assert environment["GIT_CONFIG_COUNT"] == "3"
    assert environment["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert environment["GIT_CONFIG_VALUE_0"] == ""
    assert environment["GIT_CONFIG_KEY_1"] == "credential.useHttpPath"
    assert environment["GIT_CONFIG_VALUE_1"] == "true"
    helper = environment["GIT_CONFIG_VALUE_2"]
    assert "REPOFORGE_GIT_HTTPS_TOKEN" in helper
    assert _COMPANY_TOKEN not in helper
    assert "personal-helper" not in repr(environment)
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "never"


def test_https_transport_maps_broker_github_token_to_the_reviewed_helper_environment() -> None:
    executor = TransportExecutor()
    context = ProcessAuthContext(
        profile_id="company",
        material_id="material-company",
        target_kind=AuthTargetKind.REPOSITORY,
        target_id="github-repository-123456",
        environment=(
            ("HOME", "/home/demo"),
            ("GH_TOKEN", _COMPANY_TOKEN),
            ("SSH_AUTH_SOCK", "/tmp/wrong-agent.sock"),
        ),
        _secret_values=(_COMPANY_TOKEN,),
    )

    GitTransportRouter(executor).fetch(
        Path("/repo"),
        "https://github.com/acme/widgets.git",
        "+refs/heads/main:refs/remotes/origin/main",
        _https_spec(),
        context,
    )

    environment = executor.calls[0]["environment"]
    assert environment["REPOFORGE_GIT_HTTPS_TOKEN"] == _COMPANY_TOKEN
    assert "GH_TOKEN" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert executor.calls[0]["secrets"] == (_COMPANY_TOKEN,)


def test_parallel_personal_and_company_transport_contexts_do_not_cross() -> None:
    executor = TransportExecutor()
    router = GitTransportRouter(executor)
    personal_spec = _https_spec(
        profile_id="personal",
        repository_id="987654",
        target_id="github-repository-987654",
    )
    company_spec = _https_spec()

    def run(spec: GitTransportSpec, token: str) -> None:
        router.fetch(
            Path("/repo"),
            "https://github.com/acme/widgets.git",
            "+refs/heads/main:refs/remotes/origin/main",
            spec,
            _context(profile_id=spec.profile_id, target_id=spec.target_id, token=token),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, personal_spec, _PERSONAL_TOKEN)
        second = pool.submit(run, company_spec, _COMPANY_TOKEN)
        first.result()
        second.result()

    assert len(executor.calls) == 2
    observed = {
        (call["environment"]["REPOFORGE_GIT_HTTPS_TOKEN"], call["secrets"])
        for call in executor.calls
    }
    assert observed == {
        (_PERSONAL_TOKEN, (_PERSONAL_TOKEN,)),
        (_COMPANY_TOKEN, (_COMPANY_TOKEN,)),
    }


def test_read_proof_and_write_capability_are_separate() -> None:
    executor = TransportExecutor(
        [CommandResult(("git",), "/repo", 0, f"{'1' * 40}\trefs/heads/main\n", "")]
    )
    router = GitTransportRouter(executor)
    spec = _https_spec(access=(GitTransportAccess.READ,))
    context = _context(token=_COMPANY_TOKEN)

    evidence = router.ls_remote(
        Path("/repo"),
        "https://github.com/acme/widgets.git",
        "refs/heads/main",
        spec,
        context,
    )
    assert evidence.access is GitTransportAccess.READ
    assert evidence.safe_payload()["credential_fingerprint"] == _SHA

    with pytest.raises(RepoForgeError) as denied:
        router.push(
            Path("/repo"),
            "https://github.com/acme/widgets.git",
            "HEAD:refs/heads/feature",
            spec,
            context,
        )
    assert denied.value.code is ErrorCode.CREDENTIAL_CAPABILITY_DENIED
    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    ("remote_url", "spec", "code"),
    [
        (
            "git@enterprise.example:acme/widgets.git",
            _ssh_spec(),
            ErrorCode.GIT_TRANSPORT_MIGRATION_REQUIRED,
        ),
        (
            "https://oauth2:must-not-leak@github.com/acme/widgets.git",
            _https_spec(),
            ErrorCode.CREDENTIAL_LEAK_BLOCKED,
        ),
    ],
)
def test_wrong_host_and_url_embedded_material_fail_before_git(
    remote_url: str, spec: GitTransportSpec, code: ErrorCode
) -> None:
    executor = TransportExecutor()
    with pytest.raises(RepoForgeError) as failure:
        GitTransportRouter(executor).fetch(
            Path("/repo"),
            remote_url,
            "+refs/heads/main:refs/remotes/origin/main",
            spec,
            _context(token=_COMPANY_TOKEN) if spec.kind is GitTransportKind.HTTPS else _context(),
        )
    assert failure.value.code is code
    assert executor.calls == []


def test_profile_and_repository_target_mismatch_fail_closed() -> None:
    executor = TransportExecutor()
    router = GitTransportRouter(executor)

    with pytest.raises(RepoForgeError) as profile_failure:
        router.fetch(
            Path("/repo"),
            "https://github.com/acme/widgets.git",
            "main",
            _https_spec(),
            _context(profile_id="personal", token=_PERSONAL_TOKEN),
        )
    assert profile_failure.value.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH

    with pytest.raises(RepoForgeError) as target_failure:
        router.fetch(
            Path("/repo"),
            "https://github.com/acme/widgets.git",
            "main",
            _https_spec(),
            _context(target_id="github-repository-999999", token=_COMPANY_TOKEN),
        )
    assert target_failure.value.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH
    assert executor.calls == []


def test_prompt_required_is_typed_and_never_retries_through_ambient_identity() -> None:
    executor = TransportExecutor(
        [
            CommandResult(
                ("git",),
                "/repo",
                128,
                "",
                "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
            )
        ]
    )
    with pytest.raises(RepoForgeError) as failure:
        GitTransportRouter(executor).fetch(
            Path("/repo"),
            "https://github.com/acme/widgets.git",
            "main",
            _https_spec(),
            _context(token=_COMPANY_TOKEN),
        )
    assert failure.value.code is ErrorCode.CREDENTIAL_INTERACTION_REQUIRED
    assert len(executor.calls) == 1
    assert _COMPANY_TOKEN not in str(failure.value)


def test_transport_authentication_failure_is_typed_without_fallback() -> None:
    raw_remote = "git@github-work:acme/widgets.git"
    endpoint = _reviewed_endpoint(raw_remote)
    material = FakeIdentityMaterial(Path("/run/repoforge/operation-key"))
    materials = FakeMaterials(material)
    revalidator = FakeEndpointRevalidator()
    executor = TransportExecutor(
        [CommandResult(("git",), "/repo", 128, "", "Permission denied (publickey).")]
    )
    with pytest.raises(RepoForgeError) as failure:
        GitTransportRouter(
            executor,
            endpoint_revalidator=revalidator,
            identity_materials=materials,
        ).push(
            Path("/repo"),
            raw_remote,
            "HEAD:refs/heads/company",
            _ssh_spec(endpoint=endpoint),
            _context(),
        )
    assert failure.value.code is ErrorCode.GIT_TRANSPORT_AUTHENTICATION_FAILED
    assert len(executor.calls) == 1
    assert executor.calls[0]["argv"][2] == endpoint.canonical_url()
    assert material.closed is True


def test_legacy_path_only_ssh_transport_requires_migration_before_git() -> None:
    executor = TransportExecutor()

    with pytest.raises(RepoForgeError) as failure:
        GitTransportRouter(executor).ls_remote(
            Path("/repo"),
            "git@github.com:acme/widgets.git",
            "refs/heads/main",
            _ssh_spec(),
            _context(),
        )

    assert failure.value.code is ErrorCode.GIT_TRANSPORT_MIGRATION_REQUIRED
    assert executor.calls == []


def test_transport_evidence_is_secret_free() -> None:
    executor = TransportExecutor(
        [CommandResult(("git",), "/repo", 0, f"{'2' * 40}\trefs/heads/main\n", "")]
    )
    evidence = GitTransportRouter(executor).ls_remote(
        Path("/repo"),
        "https://github.com/acme/widgets.git",
        "refs/heads/main",
        _https_spec(),
        _context(token=_COMPANY_TOKEN),
    )
    rendered = repr(evidence.safe_payload())
    assert _COMPANY_TOKEN not in rendered
    assert "https://github.com/acme/widgets.git" not in rendered
    assert (
        evidence.remote_url_digest
        == hashlib.sha256(b"https://github.com/acme/widgets.git").hexdigest()
    )


def test_real_process_boundary_receives_only_selected_https_identity(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "transport-observation.json"
    executable = fake_bin / "git"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import hashlib, json, os\n"
        "from pathlib import Path\n"
        "payload = {\n"
        "  'ssh_auth_sock': os.environ.get('SSH_AUTH_SOCK'),\n"
        "  'helper_reset': os.environ.get('GIT_CONFIG_VALUE_0'),\n"
        "  'helper': os.environ.get('GIT_CONFIG_VALUE_2'),\n"
        "  'ambient_helper_present': 'personal-helper' in repr(dict(os.environ)),\n"
        "  'token_digest': hashlib.sha256(os.environ['REPOFORGE_GIT_HTTPS_TOKEN'].encode()).hexdigest(),\n"
        "}\n"
        "Path(os.environ['REPOFORGE_TEST_MARKER']).write_text(json.dumps(payload))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    context = ProcessAuthContext(
        profile_id="company",
        material_id="material-company",
        target_kind=AuthTargetKind.REPOSITORY,
        target_id="github-repository-123456",
        environment=(
            ("PATH", str(fake_bin)),
            ("REPOFORGE_TEST_MARKER", str(marker)),
            ("REPOFORGE_GIT_HTTPS_TOKEN", _COMPANY_TOKEN),
            ("SSH_AUTH_SOCK", "/tmp/wrong-agent.sock"),
            ("GIT_CONFIG_COUNT", "1"),
            ("GIT_CONFIG_KEY_0", "credential.helper"),
            ("GIT_CONFIG_VALUE_0", "personal-helper"),
        ),
        _secret_values=(_COMPANY_TOKEN,),
    )
    router = GitTransportRouter(
        SubprocessCommandExecutor(ServerConfig(tmp_path / "workspaces", tmp_path / "state"))
    )

    router.fetch(
        tmp_path,
        "https://github.com/acme/widgets.git",
        "+refs/heads/main:refs/remotes/origin/main",
        _https_spec(),
        context,
    )

    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["ssh_auth_sock"] is None
    assert payload["helper_reset"] == ""
    assert "REPOFORGE_GIT_HTTPS_TOKEN" in payload["helper"]
    assert payload["ambient_helper_present"] is False
    assert payload["token_digest"] == hashlib.sha256(_COMPANY_TOKEN.encode()).hexdigest()
    assert _COMPANY_TOKEN not in marker.read_text(encoding="utf-8")
