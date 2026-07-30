from __future__ import annotations

from repoforge.domain.config_generation import CapabilityDeltaKind, classify_capability_delta


def _lock(*, command: str, profile: str = "test", generation: int = 1) -> str:
    return f'''[repoforge_lock]
format_version = 2
generation = {generation}
source_config = "config.toml"
source_sha256 = "a"

[repoforge_lock.repositories]
demo = "fingerprint"

[server]
workspace_root = "/tmp/workspaces"
state_root = "/tmp/state"

[repositories.demo]
path = "/tmp/demo"

[repositories.demo.profiles.{profile}]
commands = [["{command}"]]
'''


def test_semantic_delta_ignores_generation_and_toml_order() -> None:
    # Given: reviewed policy with a different generation number.
    current = _lock(command="pytest", generation=1)
    candidate = _lock(command="pytest", generation=2)

    # When: configuration semantics are compared.
    delta = classify_capability_delta(current, candidate)

    # Then: the lock is equivalent despite new metadata.
    assert delta.kind is CapabilityDeltaKind.EQUIVALENT


def test_semantic_delta_classifies_added_command_as_expansion() -> None:
    # Given: a profile with one command and a candidate with an added command.
    current = _lock(command="pytest")
    candidate = current.replace('commands = [["pytest"]]', 'commands = [["pytest"], ["ruff"]]')

    # When: capability is compared.
    delta = classify_capability_delta(current, candidate)

    # Then: added executable capability is an expansion.
    assert delta.kind is CapabilityDeltaKind.EXPANSION


def test_semantic_delta_classifies_removed_command_as_restriction() -> None:
    # Given: a profile with two commands and a candidate with one.
    current = _lock(command="pytest").replace(
        'commands = [["pytest"]]', 'commands = [["pytest"], ["ruff"]]'
    )
    candidate = _lock(command="pytest")

    # When: capability is compared.
    delta = classify_capability_delta(current, candidate)

    # Then: removed executable capability is a restriction.
    assert delta.kind is CapabilityDeltaKind.RESTRICTION


def test_semantic_delta_classifies_replaced_command_as_incompatible() -> None:
    # Given: different command capabilities with no subset relation.
    current = _lock(command="pytest")
    candidate = _lock(command="ruff")

    # When: capability is compared.
    delta = classify_capability_delta(current, candidate)

    # Then: explicit review is needed for incompatible policy edits.
    assert delta.kind is CapabilityDeltaKind.INCOMPATIBLE


_AUTH_PROFILE = """
[auth_profiles.personal]
provider = "github"
credential_kind = "stored_account"
credential_reference = "gh-account-personal"
actor_class = "human_operated"
expected_actor_id = "github-user-123"
enabled = true
repository_id = "987654"
repository_patterns = ["github.com/acme/*"]
boundary_id = "acme"
capability_ids = ["github.contents.read"]
github_host = "github.com"
github_login = "acme-operator"
transport_kind = "https"
https_token_environment = "REPOFORGE_GH_TOKEN"
credential_fingerprint = "aaaa"
allowed_access = ["read"]
lease_seconds = 300
"""


def _with_profile(replacement: tuple[str, str] | None = None) -> str:
    text = _lock(command="pytest") + _AUTH_PROFILE
    if replacement is None:
        return text
    return text.replace(*replacement)


def test_semantic_delta_classifies_a_new_auth_profile_as_expansion() -> None:
    # Given: a reviewed policy that declares no actable identity yet.
    delta = classify_capability_delta(_lock(command="pytest"), _with_profile())

    # Then: granting the runtime a new identity needs operator approval.
    assert delta.kind is CapabilityDeltaKind.EXPANSION
    assert [change.path for change in delta.changes] == ["auth_profiles"]


def test_semantic_delta_classifies_a_removed_auth_profile_as_restriction() -> None:
    delta = classify_capability_delta(_with_profile(), _lock(command="pytest"))

    assert delta.kind is CapabilityDeltaKind.RESTRICTION


def test_semantic_delta_classifies_auth_profile_grants_by_direction() -> None:
    # Given: the same identity with one more capability, then one fewer access mode.
    widened = classify_capability_delta(
        _with_profile(),
        _with_profile(
            (
                'capability_ids = ["github.contents.read"]',
                'capability_ids = ["github.contents.read", "github.contents.write"]',
            )
        ),
    )
    disabled = classify_capability_delta(
        _with_profile(), _with_profile(("enabled = true", "enabled = false"))
    )
    shortened = classify_capability_delta(
        _with_profile(), _with_profile(("lease_seconds = 300", "lease_seconds = 60"))
    )

    assert widened.kind is CapabilityDeltaKind.EXPANSION
    assert disabled.kind is CapabilityDeltaKind.RESTRICTION
    assert shortened.kind is CapabilityDeltaKind.RESTRICTION


def test_semantic_delta_classifies_a_swapped_auth_identity_as_incompatible() -> None:
    # Given: the same profile id now pointing at a different actor and credential.
    delta = classify_capability_delta(
        _with_profile(),
        _with_profile(('github_login = "acme-operator"', 'github_login = "someone-else"')),
    )

    assert delta.kind is CapabilityDeltaKind.INCOMPATIBLE
    assert [change.path for change in delta.changes] == ["auth_profiles.personal.identity"]


def test_semantic_delta_keeps_auth_import_provenance_out_of_capability() -> None:
    # Given: only the recorded SSH alias the profile was imported from changes.
    delta = classify_capability_delta(
        _with_profile(),
        _with_profile(("lease_seconds = 300", 'lease_seconds = 300\nsource_ssh_alias = "gh-work"')),
    )

    assert delta.kind is CapabilityDeltaKind.METADATA_ONLY
