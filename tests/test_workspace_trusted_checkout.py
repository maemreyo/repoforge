"""Attaching to an operator-registered external checkout the model never names by path
(#373).

The model only ever supplies an alias; only the operator, through repository
configuration, decides what that alias points to. Everything here runs against real git
clones so repository-identity (shared root commit) and symlink/containment checks are
exercised against real filesystem and git state, not a fake.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from conftest import _clone_template_repo, create_forge_environment, git

from repoforge.config import ConfigError, load_config
from repoforge.domain.errors import SecurityError, WorkspaceError
from repoforge.domain.policy import canonical_remote_identity
from repoforge.domain.workspace import WorkspaceKind


def test_canonical_remote_identity_matches_ssh_and_https_forms() -> None:
    ssh = canonical_remote_identity("git@github.com:acme/widgets.git")
    https = canonical_remote_identity("https://github.com/acme/widgets")
    https_dotgit = canonical_remote_identity("https://github.com/ACME/widgets.git/")
    assert ssh == https == https_dotgit

    different_repo = canonical_remote_identity("https://github.com/acme/other")
    assert different_repo != ssh

    different_host = canonical_remote_identity("https://gitlab.com/acme/widgets")
    assert different_host != ssh

    assert canonical_remote_identity("") is None
    assert canonical_remote_identity("   ") is None
    assert canonical_remote_identity("./relative/bare/path") is None


def _external_checkout(tmp_path: Path, name: str, branch: str) -> Path:
    """An independent clone of the same template repo -- the operator's own checkout,
    living entirely outside anything RepoForge created."""
    root = tmp_path / name
    root.mkdir()
    _remote, source = _clone_template_repo(root)
    git("checkout", "-q", "-b", branch, cwd=source)
    (source / "wip.txt").write_text(f"external wip on {branch}\n", encoding="utf-8")
    git("add", "wip.txt", cwd=source)
    git(
        "-c",
        "user.email=s@x",
        "-c",
        "user.name=s",
        "commit",
        "-q",
        "-m",
        f"wip on {branch}",
        cwd=source,
    )
    return source


def test_attach_via_registered_alias(tmp_path: Path) -> None:
    external = _external_checkout(tmp_path, "external", "ops/external-work")

    env = create_forge_environment(
        tmp_path, trusted_external_checkouts={"ops-clone": str(external)}
    )
    created = env.service.workspace_create(
        "demo", "attach via alias", attach_checkout_alias="ops-clone"
    )

    assert created["attached"] is True
    assert created["branch"] == "ops/external-work"
    assert created["path"] == str(external)
    record = env.service.state.load(created["workspace_id"])
    assert record.kind is WorkspaceKind.ATTACHED_SHARED


def test_reattaching_the_same_alias_returns_the_same_workspace_id(tmp_path: Path) -> None:
    external = _external_checkout(tmp_path, "external", "ops/reattach")
    env = create_forge_environment(
        tmp_path, trusted_external_checkouts={"ops-clone": str(external)}
    )

    first = env.service.workspace_create(
        "demo", "first phrasing", attach_checkout_alias="ops-clone"
    )
    second = env.service.workspace_create(
        "demo", "a completely different task_slug", attach_checkout_alias="ops-clone"
    )

    assert first["workspace_id"] == second["workspace_id"]


def test_reattach_self_heals_when_the_alias_checkout_switched_branch(tmp_path: Path) -> None:
    external = _external_checkout(tmp_path, "external", "ops/first-branch")
    env = create_forge_environment(
        tmp_path, trusted_external_checkouts={"ops-clone": str(external)}
    )
    first = env.service.workspace_create("demo", "attach it", attach_checkout_alias="ops-clone")
    assert first["branch"] == "ops/first-branch"

    git("checkout", "-q", "-b", "ops/second-branch", cwd=external)

    second = env.service.workspace_create(
        "demo", "attach it again", attach_checkout_alias="ops-clone"
    )

    assert second["workspace_id"] == first["workspace_id"]
    assert second["branch"] == "ops/second-branch"


def test_attach_refuses_a_second_alias_that_now_resolves_to_an_already_attached_checkout(
    tmp_path: Path,
) -> None:
    """Config-time collision detection (see test_workspace_trusted_checkout's sibling
    config-level tests) only catches two aliases pointing at the same path AS OF THE LOAD
    THAT VALIDATED THEM. A symlink retargeted afterward, without a config reload, can
    still make two aliases converge on the same live checkout -- this is the live,
    attach-time backstop for exactly that gap."""
    shared = _external_checkout(tmp_path, "shared-external", "ops/shared")
    placeholder = tmp_path / "placeholder"
    placeholder.mkdir()
    link = tmp_path / "link-alias-target"
    link.symlink_to(placeholder)

    env = create_forge_environment(
        tmp_path,
        trusted_external_checkouts={"direct": str(shared), "via_link": str(link)},
    )
    env.service.workspace_create("demo", "attach direct", attach_checkout_alias="direct")

    link.unlink()
    link.symlink_to(shared)

    with pytest.raises(WorkspaceError, match="ATTACH_CHECKOUT_ALREADY_REGISTERED"):
        env.service.workspace_create(
            "demo", "attach via retargeted symlink", attach_checkout_alias="via_link"
        )


def test_attach_alias_not_registered_fails_with_evidence(tmp_path: Path) -> None:
    env = create_forge_environment(tmp_path)

    with pytest.raises(WorkspaceError, match="ATTACH_ALIAS_NOT_REGISTERED"):
        env.service.workspace_create(
            "demo", "attach nothing", attach_checkout_alias="never-registered"
        )


def test_attach_alias_repository_mismatch_fails_closed(tmp_path: Path) -> None:
    """The alias points at a real git repository -- just not this one."""
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    git("init", "-q", "-b", "main", cwd=unrelated)
    (unrelated / "other.txt").write_text("unrelated repo\n", encoding="utf-8")
    git("add", "other.txt", cwd=unrelated)
    git(
        "-c",
        "user.email=s@x",
        "-c",
        "user.name=s",
        "commit",
        "-q",
        "-m",
        "unrelated commit",
        cwd=unrelated,
    )

    env = create_forge_environment(
        tmp_path, trusted_external_checkouts={"wrong-repo": str(unrelated)}
    )

    with pytest.raises(WorkspaceError, match="ATTACH_REPOSITORY_MISMATCH"):
        env.service.workspace_create(
            "demo", "attach wrong repo", attach_checkout_alias="wrong-repo"
        )


def test_attach_alias_remote_mismatch_fails_closed_despite_shared_root_commit(
    tmp_path: Path,
) -> None:
    """Review finding: a shared root commit alone proves common ancestry, not the same
    repository -- a fork or an unrelated clone taken from the same history shares it too.
    Every other test in this file uses bare filesystem-path remotes (what
    `_clone_template_repo`/`_external_checkout` set up), which don't parse as a
    comparable identity and so correctly don't engage this check at all; this uses
    file:// URLs specifically to exercise the comparison firing."""
    external = _external_checkout(tmp_path, "external", "ops/remote-mismatch")
    unrelated_remote = tmp_path / "unrelated-remote.git"
    unrelated_remote.mkdir()
    git("init", "-q", "--bare", cwd=unrelated_remote)
    git("remote", "set-url", "origin", f"file://{unrelated_remote}", cwd=external)

    env = create_forge_environment(tmp_path, trusted_external_checkouts={"forked": str(external)})
    git("remote", "set-url", "origin", f"file://{env.remote}", cwd=env.source)

    with pytest.raises(WorkspaceError, match="ATTACH_REPOSITORY_MISMATCH"):
        env.service.workspace_create("demo", "attach forked remote", attach_checkout_alias="forked")


def test_attach_alias_missing_checkout_fails_with_evidence(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist-on-disk"
    env = create_forge_environment(tmp_path, trusted_external_checkouts={"gone": str(missing)})

    with pytest.raises(WorkspaceError, match="ATTACH_CHECKOUT_MISSING"):
        env.service.workspace_create("demo", "attach missing", attach_checkout_alias="gone")


def test_attach_alias_detached_checkout_is_refused(tmp_path: Path) -> None:
    external = _external_checkout(tmp_path, "external", "ops/for-detach")
    head = git("rev-parse", "HEAD", cwd=external)
    git("checkout", "-q", "--detach", head, cwd=external)

    env = create_forge_environment(tmp_path, trusted_external_checkouts={"detached": str(external)})

    with pytest.raises(WorkspaceError, match="ATTACH_CHECKOUT_DETACHED"):
        env.service.workspace_create("demo", "attach detached", attach_checkout_alias="detached")


def test_attach_alias_protected_branch_is_refused(tmp_path: Path) -> None:
    external = _external_checkout(tmp_path, "external", "wip/temp")
    git("checkout", "-q", "main", cwd=external)  # protected in the demo config

    env = create_forge_environment(tmp_path, trusted_external_checkouts={"on-main": str(external)})

    with pytest.raises(SecurityError, match="Protected branch"):
        env.service.workspace_create("demo", "attach protected", attach_checkout_alias="on-main")


def test_attach_alias_symlink_escaping_to_workspace_root_fails_closed(tmp_path: Path) -> None:
    """A registered alias whose target is swapped, after config load, for a symlink into
    workspace_root must fail closed on the live resolution, not trust a stale snapshot."""
    external = _external_checkout(tmp_path, "external", "ops/symlink-target")
    link_path = tmp_path / "alias-link"
    link_path.symlink_to(external, target_is_directory=True)

    env = create_forge_environment(
        tmp_path, trusted_external_checkouts={"via-link": str(link_path)}
    )
    # Sanity: works while the symlink points at a legitimate external checkout.
    created = env.service.workspace_create(
        "demo", "attach via link", attach_checkout_alias="via-link"
    )
    assert created["attached"] is True

    # Now substitute the symlink target to point inside workspace_root.
    workspace_root = env.root / "workspaces"
    workspace_root.mkdir(parents=True, exist_ok=True)
    link_path.unlink()
    link_path.symlink_to(workspace_root, target_is_directory=True)

    with pytest.raises(SecurityError, match="TRUSTED_CHECKOUT_ESCAPES_TO_WORKSPACE_ROOT"):
        env.service.workspace_create(
            "demo", "attach via substituted link", attach_checkout_alias="via-link"
        )


def test_removing_an_alias_attached_workspace_detaches_registry_only(tmp_path: Path) -> None:
    external = _external_checkout(tmp_path, "external", "ops/attach-remove")
    env = create_forge_environment(
        tmp_path, trusted_external_checkouts={"ops-clone": str(external)}
    )
    created = env.service.workspace_create(
        "demo", "attach then remove", attach_checkout_alias="ops-clone"
    )

    result = env.service.workspace_remove(created["workspace_id"])

    assert result["removed"] is True
    assert external.is_dir()
    with pytest.raises(WorkspaceError, match="Unknown workspace id"):
        env.service.state.load(created["workspace_id"])


def test_revoking_an_alias_immediately_prevents_new_attach(tmp_path: Path) -> None:
    """Revocation is a config edit: the alias simply stops existing in the next-loaded
    generation. This proves the *effect* of revocation -- a config without the alias
    refuses to attach through it -- which is what the runtime actually observes across a
    real `rf config approve` + reload, not the CLI mechanics of getting there."""
    external = _external_checkout(tmp_path, "external", "ops/revoke-me")
    before_dir = tmp_path / "before-revoke"
    before_dir.mkdir()
    granted = create_forge_environment(
        before_dir, trusted_external_checkouts={"ops-clone": str(external)}
    )
    created = granted.service.workspace_create(
        "demo", "attach before revoke", attach_checkout_alias="ops-clone"
    )
    assert created["attached"] is True

    after_dir = tmp_path / "after-revoke"
    after_dir.mkdir()
    revoked = create_forge_environment(after_dir)

    with pytest.raises(WorkspaceError, match="ATTACH_ALIAS_NOT_REGISTERED"):
        revoked.service.workspace_create(
            "demo", "attach after revoke", attach_checkout_alias="ops-clone"
        )


def test_revoking_an_alias_cuts_off_an_already_attached_workspace(tmp_path: Path) -> None:
    """Review finding: the sibling test above only proves a NEW attach attempt fails
    after revocation -- it builds a second, independent environment and never re-
    exercises the already-attached workspace from the first one. This is the actual AC:
    revoking a trusted checkout must reach a workspace already attached through it, not
    just block new attachments. Simulates the config-reload the operator would trigger
    (`rf config approve`) by swapping the live context's config in place, the same way
    tests/test_service_tools.py and tests/test_v2_shipping.py already do."""
    external = _external_checkout(tmp_path, "external", "ops/revoke-existing")
    # workspace_status computes ahead-of-base against origin/{base} unconditionally, so
    # the branch needs a remote counterpart in the external checkout's own origin --
    # exactly what a genuine operator checkout would already have.
    git("push", "-q", "origin", "ops/revoke-existing", cwd=external)
    env = create_forge_environment(
        tmp_path, trusted_external_checkouts={"ops-clone": str(external)}
    )
    created = env.service.workspace_create(
        "demo", "attach then revoke", attach_checkout_alias="ops-clone"
    )
    workspace_id = created["workspace_id"]
    env.service.workspace_status(workspace_id)  # sanity: works before revocation

    ctx = env.service.application.context
    configured = env.service.config.repositories["demo"]
    revoked_config = replace(
        env.service.config,
        repositories={
            **env.service.config.repositories,
            "demo": replace(configured, trusted_external_checkouts={}),
        },
    )
    object.__setattr__(ctx, "config", revoked_config)
    env.service.config = revoked_config

    with pytest.raises(SecurityError, match="ATTACH_CHECKOUT_REVOKED"):
        env.service.workspace_status(workspace_id)

    # Review finding F-002: revocation must not create a dead end. workspace_remove is
    # the only public recovery action for a stale attached-workspace record, and it must
    # still succeed -- as a registry-only detach that never touches the now-unauthorized
    # checkout -- even though every other call on this workspace_id is now refused.
    result = env.service.workspace_remove(workspace_id)
    assert result["removed"] is True
    assert external.is_dir()
    with pytest.raises(WorkspaceError, match="Unknown workspace id"):
        env.service.state.load(workspace_id)


def test_attach_alias_and_branch_together_are_refused(tmp_path: Path) -> None:
    external = _external_checkout(tmp_path, "external", "ops/conflict")
    env = create_forge_environment(
        tmp_path, trusted_external_checkouts={"ops-clone": str(external)}
    )

    with pytest.raises(WorkspaceError, match="ADOPT_BASE_CONFLICT"):
        env.service.workspace_create(
            "demo",
            "conflicting",
            attach_branch="main",
            attach_checkout_alias="ops-clone",
        )


# --- Config-loading validation (#373 static safety net) ---


def test_config_rejects_unsafe_alias(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unsafe alias"):
        create_forge_environment(
            tmp_path, trusted_external_checkouts={"../escape": "/tmp/whatever"}
        )


def test_config_rejects_alias_path_inside_workspace_root(tmp_path: Path) -> None:
    inside = tmp_path / "workspaces" / "demo" / "sneaky"
    with pytest.raises(ConfigError, match="inside workspace_root"):
        create_forge_environment(tmp_path, trusted_external_checkouts={"sneaky": str(inside)})


def test_config_rejects_the_same_path_registered_by_two_repositories(tmp_path: Path) -> None:
    shared = tmp_path / "shared-external"
    shared.mkdir()

    config_path = tmp_path / "two-repo-config.toml"
    workspace_root = tmp_path / "workspaces"
    state_root = tmp_path / "state"
    _remote_a, source_a = _clone_template_repo(tmp_path / "repo-a")
    _remote_b, source_b = _clone_template_repo(tmp_path / "repo-b")
    config_path.write_text(
        f'''[server]
workspace_root = "{workspace_root}"
state_root = "{state_root}"

[repositories.repo_a]
path = "{source_a}"
default_base = "main"
allowed_base_branches = ["main"]

[repositories.repo_a.trusted_external_checkouts]
shared = "{shared}"

[repositories.repo_b]
path = "{source_b}"
default_base = "main"
allowed_base_branches = ["main"]

[repositories.repo_b.trusted_external_checkouts]
shared = "{shared}"
''',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="registered by both"):
        load_config(config_path)


def test_config_rejects_the_same_path_registered_by_two_aliases_in_the_same_repository(
    tmp_path: Path,
) -> None:
    """Review finding: the collision check only compared owning repo_id, so two aliases
    under the SAME repo pointing at the same checkout were not rejected -- each would mint
    its own workspace_id (and thus its own mutation lock) for the identical filesystem
    checkout, letting two operations run against it concurrently unlocked."""
    shared = tmp_path / "shared-external"
    shared.mkdir()

    config_path = tmp_path / "same-repo-two-aliases.toml"
    workspace_root = tmp_path / "workspaces"
    state_root = tmp_path / "state"
    _remote, source = _clone_template_repo(tmp_path / "repo-a")
    config_path.write_text(
        f'''[server]
workspace_root = "{workspace_root}"
state_root = "{state_root}"

[repositories.demo]
path = "{source}"
default_base = "main"
allowed_base_branches = ["main"]

[repositories.demo.trusted_external_checkouts]
editor = "{shared}"
terminal = "{shared}"
''',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="registered by both"):
        load_config(config_path)


def test_config_rejects_a_symlink_alias_colliding_with_its_real_target(
    tmp_path: Path,
) -> None:
    """Review finding: the collision check compared unresolved config-declared paths,
    while attach resolves symlinks live -- so a symlink alias and a direct alias to the
    same real checkout, spelled differently, evaded config-time collision detection."""
    real = tmp_path / "real-external"
    real.mkdir()
    link = tmp_path / "link-to-real-external"
    link.symlink_to(real)

    config_path = tmp_path / "symlink-collision.toml"
    workspace_root = tmp_path / "workspaces"
    state_root = tmp_path / "state"
    _remote, source = _clone_template_repo(tmp_path / "repo-a")
    config_path.write_text(
        f'''[server]
workspace_root = "{workspace_root}"
state_root = "{state_root}"

[repositories.demo]
path = "{source}"
default_base = "main"
allowed_base_branches = ["main"]

[repositories.demo.trusted_external_checkouts]
direct = "{real}"
via_symlink = "{link}"
''',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="registered by both"):
        load_config(config_path)
