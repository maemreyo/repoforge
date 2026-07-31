"""Reviewed auth-profile configuration: parse, project, load, and survive a refresh.

The declaration is secret-free by construction, and the runtime model builds the existing
identity primitives rather than a parallel bag of strings. The round-trip and copy-helper
tests exist because a reviewed profile silently dropped during an unrelated repository
refresh would leave the runtime with no identity and no error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomli as tomllib

from repoforge.application.configuration.document import apply_auth_profiles, render_resolved
from repoforge.application.configuration.source import (
    SourceAuthProfile,
    SourceRepository,
    add_source_repository,
    parse_source,
    remove_source_repository,
    render_source,
)
from repoforge.config import AuthProfileConfig, load_config
from repoforge.domain.errors import ConfigError
from repoforge.domain.git_transport_identity import GitTransportKind
from repoforge.domain.github_api_identity import (
    GitHubAppInstallationSpec,
    StoredGhAccountSpec,
)
from repoforge.domain.repository_identity import ActorClass

_SHA_A = "a" * 64
_SSH_FINGERPRINT = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _stored_profile_table() -> str:
    return f'''[auth_profiles.personal]
provider = "github"
credential_kind = "stored_account"
credential_reference = "gh-account-personal"
actor_class = "human_operated"
expected_actor_id = "github-user-123"
enabled = true
repository_id = "987654"
repository_patterns = ["github.com/maemreyo/*"]
boundary_id = "personal-owner"
capability_ids = ["github.contents.read", "github.contents.write"]
github_host = "github.com"
github_login = "maemreyo"
transport_kind = "https"
https_token_environment = "REPOFORGE_GH_PERSONAL_TOKEN"
credential_fingerprint = "{_SHA_A}"
allowed_access = ["read", "write"]
lease_seconds = 300
'''


def _source_text() -> str:
    return 'version = 2\n[[repo]]\nid = "demo"\npath = "/tmp/demo"\n\n' + _stored_profile_table()


def _ssh_profile_table() -> str:
    return f'''[auth_profiles.work]
provider = "github"
credential_kind = "stored_account"
credential_reference = "gh-account-matw-ngo"
actor_class = "human_operated"
expected_actor_id = "173029271"
enabled = true
repository_id = "341454638"
repository_patterns = ["github.com/cicdata-io/*"]
boundary_id = "github-com-cicdata-io"
capability_ids = ["github.contents.read", "github.contents.write"]
github_host = "github.com"
github_login = "matw-ngo"
transport_kind = "ssh"
credential_fingerprint = "{_SHA_A}"
allowed_access = ["read", "write"]
lease_seconds = 300

[auth_profiles.work.ssh_endpoint]
schema_version = 1
raw_host = "github-work"
canonical_host = "github.com"
user = "git"
port = 22
owner = "cicdata-io"
repository = "portal-spa"
raw_url_digest = "{_SHA_A}"
canonical_path = "/Users/trung.ngo/.ssh/id_rsa_work"
public_key_fingerprint = "{_SSH_FINGERPRINT}"
owner_uid = 501
mode = 384
key_observed_at = "2026-08-01T00:00:00+00:00"
source_config_digest = "{"b" * 64}"
selected_block_digest = "{"c" * 64}"
principal_kind = "github_account"
principal_login = "matw-ngo"
principal_actor_id = "173029271"
principal_observed_at = "2026-08-01T00:00:00+00:00"
principal_proof_digest = "{"d" * 64}"
proof_digest = "{"e" * 64}"
'''


def _legacy_ssh_profile_table() -> str:
    return f'''[auth_profiles.work]
provider = "github"
credential_kind = "stored_account"
credential_reference = "gh-account-matw-ngo"
actor_class = "human_operated"
expected_actor_id = "173029271"
enabled = true
repository_id = "341454638"
repository_patterns = ["github.com/cicdata-io/*"]
boundary_id = "github-com-cicdata-io"
capability_ids = ["github.contents.read", "github.contents.write"]
github_host = "github.com"
github_login = "matw-ngo"
transport_kind = "ssh"
ssh_identity_file = "/Users/trung.ngo/.ssh/id_rsa_work"
source_ssh_alias = "github-work"
credential_fingerprint = "{_SHA_A}"
allowed_access = ["read", "write"]
lease_seconds = 300
'''


def _write_runtime_config(tmp_path: Path, *, extra: str = "") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    path = tmp_path / "config.toml"
    path.write_text(
        f'''[repositories.demo]
path = "{repo}"

{_stored_profile_table()}
{extra}
''',
        encoding="utf-8",
    )
    return path


def _write_ssh_runtime_config(tmp_path: Path) -> Path:
    repo = tmp_path / "portal-spa"
    repo.mkdir()
    (repo / ".git").mkdir()
    path = tmp_path / "ssh-config.toml"
    path.write_text(
        f'''[repositories.portal-spa]
path = "{repo}"

{_ssh_profile_table()}
''',
        encoding="utf-8",
    )
    return path


def test_source_auth_profiles_round_trip_and_copy_helpers_preserve_them() -> None:
    parsed = parse_source(_source_text())

    assert len(parsed.auth_profiles) == 1
    profile = parsed.auth_profiles[0]
    assert isinstance(profile, SourceAuthProfile)
    assert profile.profile_id == "personal"
    assert profile.github_login == "maemreyo"
    assert profile.repository_patterns == ("github.com/maemreyo/*",)
    assert parse_source(render_source(parsed)) == parsed

    added = add_source_repository(parsed, SourceRepository("second", "/tmp/second"))
    removed = remove_source_repository(added, "second")
    assert added.auth_profiles == parsed.auth_profiles
    assert removed.auth_profiles == parsed.auth_profiles


def test_source_ssh_endpoint_proof_round_trips_without_losing_identity_evidence() -> None:
    source = (
        'version = 2\n[[repo]]\nid = "portal-spa"\npath = "/tmp/portal-spa"\n\n'
        + _ssh_profile_table()
    )

    parsed = parse_source(source)

    endpoint = parsed.auth_profiles[0].ssh_endpoint
    assert endpoint is not None
    assert endpoint.raw_host == "github-work"
    assert endpoint.canonical_host == "github.com"
    assert endpoint.key.public_key_fingerprint == _SSH_FINGERPRINT
    assert endpoint.principal.principal_login == "matw-ngo"
    assert parse_source(render_source(parsed)) == parsed


def test_source_ssh_endpoint_rejects_conflicting_legacy_transport_metadata() -> None:
    source = (
        'version = 2\n[[repo]]\nid = "portal-spa"\npath = "/tmp/portal-spa"\n\n'
        + _ssh_profile_table().replace(
            'transport_kind = "ssh"',
            'transport_kind = "ssh"\n'
            'ssh_identity_file = "/Users/trung.ngo/.ssh/id_rsa_personal"\n'
            'source_ssh_alias = "github-personal"',
        )
    )

    with pytest.raises(ValueError, match="legacy SSH metadata must match ssh_endpoint"):
        parse_source(source)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('principal_login = "matw-ngo"', 'principal_login = "maemreyo"'),
        ('principal_actor_id = "173029271"', 'principal_actor_id = "47786834"'),
    ],
)
def test_source_ssh_endpoint_principal_must_match_the_profile_actor(
    old: str,
    new: str,
) -> None:
    source = (
        'version = 2\n[[repo]]\nid = "portal-spa"\npath = "/tmp/portal-spa"\n\n'
        + _ssh_profile_table().replace(old, new)
    )

    with pytest.raises(ValueError, match="principal must match the profile actor"):
        parse_source(source)


def test_source_copy_helpers_preserve_the_reviewed_tunnel_connection_ttl() -> None:
    # The TTL lives under [tunnel], so only a tunnel-bound source can carry it at all.
    parsed = parse_source(
        'version = 2\n[tunnel]\nid = "tunnel-1"\nprofile = "repoforge"\n'
        "mcp_connection_max_ttl_seconds = 3600\n"
        '\n[[repo]]\nid = "demo"\npath = "/tmp/demo"\n' + _stored_profile_table()
    )

    assert parsed.mcp_connection_max_ttl_seconds == 3600
    assert parse_source(render_source(parsed)) == parsed

    added = add_source_repository(parsed, SourceRepository("second", "/tmp/second"))
    removed = remove_source_repository(added, "second")
    assert added.mcp_connection_max_ttl_seconds == 3600
    assert removed.mcp_connection_max_ttl_seconds == 3600


def test_source_auth_profiles_are_strict_and_secret_free() -> None:
    with pytest.raises(ValueError, match="unsupported fields"):
        parse_source(
            _source_text().replace("lease_seconds = 300", 'lease_seconds = 300\ntoken = "raw"')
        )

    with pytest.raises(ValueError, match="credential_reference"):
        parse_source(
            _source_text().replace(
                'credential_reference = "gh-account-personal"',
                'credential_reference = "ghp_this_must_never_be_stored"',
            )
        )

    with pytest.raises(ValueError, match="identity-file"):
        parse_source(
            _source_text()
            .replace('transport_kind = "https"', 'transport_kind = "ssh"')
            .replace(
                'https_token_environment = "REPOFORGE_GH_PERSONAL_TOKEN"',
                'ssh_identity_file = "relative/id_ed25519"',
            )
        )


def test_runtime_auth_profile_builds_existing_identity_primitives(tmp_path: Path) -> None:
    loaded = load_config(_write_runtime_config(tmp_path))

    configured = loaded.auth_profiles["personal"]
    assert isinstance(configured, AuthProfileConfig)
    assert configured.profile.profile_id == "personal"
    assert configured.profile.credential_ref.scheme == "gh-account"
    assert configured.profile.credential_ref.reference_id == "gh-account-personal"
    assert configured.profile.actor_class is ActorClass.HUMAN_OPERATED
    assert configured.eligibility.repository_patterns == ("github.com/maemreyo/*",)
    assert isinstance(configured.api_identity, StoredGhAccountSpec)
    assert configured.api_identity.login == "maemreyo"
    assert configured.transport.kind is GitTransportKind.HTTPS
    assert configured.transport.https_token_environment == "REPOFORGE_GH_PERSONAL_TOKEN"
    assert loaded.identity_migration_required is False


def test_runtime_ssh_profile_compiles_the_reviewed_endpoint_proof(tmp_path: Path) -> None:
    loaded = load_config(_write_ssh_runtime_config(tmp_path))

    transport = loaded.auth_profiles["work"].transport
    assert transport.kind is GitTransportKind.SSH
    assert transport.ssh_endpoint is not None
    assert transport.ssh_endpoint.canonical_url() == (
        "ssh://git@github.com:22/cicdata-io/portal-spa.git"
    )
    assert transport.ssh_identity_file is None


def test_legacy_path_only_ssh_profile_remains_decodable_without_endpoint_authority(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "portal-spa"
    repo.mkdir()
    (repo / ".git").mkdir()
    path = tmp_path / "legacy-ssh.toml"
    path.write_text(
        f'''[repositories.portal-spa]
path = "{repo}"

{_legacy_ssh_profile_table()}
''',
        encoding="utf-8",
    )

    transport = load_config(path).auth_profiles["work"].transport

    assert transport.kind is GitTransportKind.SSH
    assert transport.ssh_endpoint is None
    assert transport.ssh_identity_file == "/Users/trung.ngo/.ssh/id_rsa_work"


def test_github_app_profile_constructs_app_installation_spec(tmp_path: Path) -> None:
    path = _write_runtime_config(tmp_path)
    text = path.read_text(encoding="utf-8")
    text = text.replace("[auth_profiles.personal]", "[auth_profiles.automation]")
    text = text.replace('credential_kind = "stored_account"', 'credential_kind = "github_app"')
    text = text.replace(
        'credential_reference = "gh-account-personal"', 'credential_reference = "github-app-prod"'
    )
    text = text.replace('actor_class = "human_operated"', 'actor_class = "autonomous_agent"')
    text = text.replace(
        'expected_actor_id = "github-user-123"', 'expected_actor_id = "github-app-42"'
    )
    text = text.replace(
        'github_login = "maemreyo"',
        'github_app_id = "42"\ngithub_installation_id = "314"\ngithub_permissions = ["contents:write", "pull_requests:write"]',
    )
    path.write_text(text, encoding="utf-8")

    configured = load_config(path).auth_profiles["automation"]

    assert configured.profile.credential_ref.scheme == "github-app"
    assert configured.profile.credential_ref.reference_id == "github-app-prod"
    assert isinstance(configured.api_identity, GitHubAppInstallationSpec)
    assert configured.api_identity.installation_id == "314"
    assert configured.api_identity.permission_ids == ("contents:write", "pull_requests:write")


def test_legacy_runtime_config_loads_with_explicit_migration_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = tmp_path / "legacy.toml"
    path.write_text(f'[repositories.demo]\npath = "{repo}"\n', encoding="utf-8")

    loaded = load_config(path)

    assert loaded.auth_profiles == {}
    assert loaded.identity_migration_required is True


def test_resolved_renderer_preserves_auth_profile_tables() -> None:
    source_profile = tomllib.loads(_stored_profile_table())["auth_profiles"]["personal"]
    source = parse_source(_source_text())
    document = apply_auth_profiles(
        {"repositories": {"demo": {"path": "/tmp/demo"}}},
        source.auth_profiles,
    )
    rendered = render_resolved(
        document,
        generation=1,
        source_path="/tmp/source.toml",
        source_sha256=_SHA_A,
        created_at="2026-07-30T12:00:00+00:00",
        reason="test",
        proposal_id=None,
        repository_fingerprints=(("demo", _SHA_A),),
    )

    reparsed = tomllib.loads(rendered)
    assert reparsed["auth_profiles"]["personal"] == source_profile


def test_resolved_renderer_preserves_structured_ssh_endpoint_proof() -> None:
    source = parse_source(
        'version = 2\n[[repo]]\nid = "portal-spa"\npath = "/tmp/portal-spa"\n\n'
        + _ssh_profile_table()
    )
    document = apply_auth_profiles(
        {"repositories": {"portal-spa": {"path": "/tmp/portal-spa"}}},
        source.auth_profiles,
    )

    rendered = render_resolved(
        document,
        generation=2,
        source_path="/tmp/source.toml",
        source_sha256=_SHA_A,
        created_at="2026-08-01T00:00:00+00:00",
        reason="test",
        proposal_id=None,
        repository_fingerprints=(("portal-spa", _SHA_A),),
    )

    endpoint = tomllib.loads(rendered)["auth_profiles"]["work"]["ssh_endpoint"]
    assert endpoint["raw_host"] == "github-work"
    assert endpoint["public_key_fingerprint"] == _SSH_FINGERPRINT
    assert endpoint["principal_login"] == "matw-ngo"


def test_commit_identity_must_reference_compatible_declared_auth_profile(tmp_path: Path) -> None:
    extra = """
[repositories.demo.commit_identity]
profile_id = "personal"
actor_class = "autonomous_agent"
author_name = "RepoForge Agent"
author_email = "agent@example.com"
committer_name = "RepoForge Agent"
committer_email = "agent@example.com"
"""

    with pytest.raises(ConfigError, match=r"commit_identity.*auth profile"):
        load_config(_write_runtime_config(tmp_path, extra=extra))
