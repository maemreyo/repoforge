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
