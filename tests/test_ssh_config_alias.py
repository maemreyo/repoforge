"""Constrained, non-executing SSH alias configuration parsing."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.git_remote_identity import GitRemoteKind


def _adapter() -> object:
    try:
        return importlib.import_module("repoforge.adapters.git.remote_identity")
    except ModuleNotFoundError:
        pytest.fail("constrained SSH alias parser is not implemented")


def _resolver(tmp_path: Path, text: str) -> object:
    adapter = _adapter()
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    config = ssh_dir / "config"
    config.write_text(text, encoding="utf-8")
    return adapter.ConstrainedSshConfigAliasResolver(
        paths=adapter.EffectiveUserPaths(home=home, ssh_config=config)
    )


def test_exact_alias_resolves_without_running_openssh(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        """Host github-work
  HostName github.com
  User git
  Port 22
  IdentityFile ~/.ssh/id_rsa_work
  IdentitiesOnly yes
""",
    )

    resolved = resolver.resolve("github-work")

    assert resolved.alias == "github-work"
    assert resolved.canonical_host == "github.com"
    assert resolved.user == "git"
    assert resolved.port == 22
    assert resolved.identity_file == str(tmp_path / "home/.ssh/id_rsa_work")
    assert len(resolved.source_config_digest) == 64
    assert len(resolved.selected_block_digest) == 64


@pytest.mark.parametrize(
    "directive",
    [
        "Match exec true",
        "Include ~/.ssh/conf.d/*",
        "ProxyCommand nc %h %p",
        "ProxyJump bastion",
        "IdentityAgent ~/.ssh/agent.sock",
        "CanonicalizeHostname yes",
    ],
)
def test_command_bearing_or_full_openssh_directives_are_blocking(
    tmp_path: Path,
    directive: str,
) -> None:
    resolver = _resolver(
        tmp_path,
        f"""Host github-work
  HostName github.com
  User git
  Port 22
  IdentityFile ~/.ssh/id_rsa_work
  IdentitiesOnly yes
  {directive}
""",
    )

    with pytest.raises(RepoForgeError) as failure:
        resolver.resolve("github-work")

    assert failure.value.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH


@pytest.mark.parametrize(
    "prefix",
    [
        "IdentityFile ~/.ssh/id_global\n",
        """Host github-*
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_wildcard
  IdentitiesOnly yes
""",
        """Host github-work other-host
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_multi
  IdentitiesOnly yes
""",
    ],
)
def test_global_or_competing_host_rules_make_the_alias_undecidable(
    tmp_path: Path,
    prefix: str,
) -> None:
    resolver = _resolver(
        tmp_path,
        prefix
        + """Host github-work
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_rsa_work
  IdentitiesOnly yes
""",
    )

    with pytest.raises(RepoForgeError) as failure:
        resolver.resolve("github-work")

    assert failure.value.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH


def test_scp_style_remote_is_parsed_without_resolving_the_alias() -> None:
    adapter = _adapter()

    remote = adapter.ConstrainedGitRemoteParser().parse("git@github-work:cicdata-io/portal-spa.git")

    assert remote.kind is GitRemoteKind.SSH
    assert remote.raw_host == "github-work"
    assert remote.user == "git"
    assert remote.port is None
    assert remote.owner == "cicdata-io"
    assert remote.repository == "portal-spa"
    assert len(remote.raw_url_digest) == 64


@pytest.mark.parametrize(
    ("remote_url", "kind", "host", "user", "port"),
    [
        (
            "ssh://git@github.com:2222/cicdata-io/portal-spa.git",
            GitRemoteKind.SSH,
            "github.com",
            "git",
            2222,
        ),
        (
            "https://github.com/cicdata-io/portal-spa.git",
            GitRemoteKind.HTTPS,
            "github.com",
            None,
            None,
        ),
    ],
)
def test_canonical_git_remote_urls_are_parsed_without_credentials(
    remote_url: str,
    kind: GitRemoteKind,
    host: str,
    user: str | None,
    port: int | None,
) -> None:
    remote = _adapter().ConstrainedGitRemoteParser().parse(remote_url)

    assert remote.kind is kind
    assert remote.raw_host == host
    assert remote.user == user
    assert remote.port == port
    assert remote.repository_path == "cicdata-io/portal-spa"


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://user@github.com/cicdata-io/portal-spa.git",
        "https://user:password@github.com/cicdata-io/portal-spa.git",
        "ssh://git:password@github.com/cicdata-io/portal-spa.git",
        "https://github.com/cicdata-io/portal-spa.git?token=secret",
        "https://github.com/cicdata-io/portal-spa.git#fragment",
        "file:///tmp/portal-spa.git",
        "git@localhost:cicdata-io/portal-spa.git",
        "git@github.com:cicdata-io/nested/portal-spa.git",
        "git@github.com:../portal-spa.git",
    ],
)
def test_unsafe_or_non_repository_remote_urls_are_rejected(remote_url: str) -> None:
    with pytest.raises(RepoForgeError) as failure:
        _adapter().ConstrainedGitRemoteParser().parse(remote_url)

    assert failure.value.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH


def test_alias_parser_accepts_standard_tab_separated_directives(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        "Host\tgithub-work\n"
        "\tHostName\tgithub.com\n"
        "\tUser\tgit\n"
        "\tPort\t22\n"
        "\tIdentityFile\t~/.ssh/id_rsa_work\n"
        "\tIdentitiesOnly\tyes\n",
    )

    resolved = resolver.resolve("github-work")

    assert resolved.canonical_host == "github.com"
    assert resolved.identity_file.endswith("/.ssh/id_rsa_work")


def test_alias_parser_rejects_a_group_or_world_writable_config(tmp_path: Path) -> None:
    adapter = _adapter()
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    config = ssh_dir / "config"
    config.write_text(
        "Host github-work\n"
        "  HostName github.com\n"
        "  User git\n"
        "  IdentityFile ~/.ssh/id_rsa_work\n"
        "  IdentitiesOnly yes\n",
        encoding="utf-8",
    )
    config.chmod(0o666)
    resolver = adapter.ConstrainedSshConfigAliasResolver(
        paths=adapter.EffectiveUserPaths(home=home, ssh_config=config)
    )

    with pytest.raises(RepoForgeError) as failure:
        resolver.resolve("github-work")

    assert failure.value.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH


def test_alias_parser_reports_invalid_utf8_as_a_typed_failure(tmp_path: Path) -> None:
    adapter = _adapter()
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    config = ssh_dir / "config"
    config.write_bytes(b"Host github-work\n  HostName github.com\n\xff")
    resolver = adapter.ConstrainedSshConfigAliasResolver(
        paths=adapter.EffectiveUserPaths(home=home, ssh_config=config)
    )

    with pytest.raises(RepoForgeError) as failure:
        resolver.resolve("github-work")

    assert failure.value.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH
