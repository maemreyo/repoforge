from __future__ import annotations

from repoforge.domain.config_generation import (
    CapabilityDeltaKind,
    classify_capability_delta,
)


def _lock(*, command: str, generation: int = 1) -> str:
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

[repositories.demo.profiles.test]
commands = [["{command}"]]
'''


def test_profile_command_addition_is_expansion() -> None:
    current = _lock(command="pytest")
    candidate = current.replace('commands = [["pytest"]]', 'commands = [["pytest"], ["ruff"]]')

    delta = classify_capability_delta(current, candidate)

    assert delta.kind is CapabilityDeltaKind.EXPANSION
    assert {change.direction for change in delta.changes} == {CapabilityDeltaKind.EXPANSION}


def test_profile_command_removal_is_restriction() -> None:
    current = _lock(command="pytest").replace(
        'commands = [["pytest"]]', 'commands = [["pytest"], ["ruff"]]'
    )
    candidate = _lock(command="pytest")

    delta = classify_capability_delta(current, candidate)

    assert delta.kind is CapabilityDeltaKind.RESTRICTION
    assert {change.direction for change in delta.changes} == {CapabilityDeltaKind.RESTRICTION}


def test_profile_command_replacement_is_incompatible() -> None:
    delta = classify_capability_delta(_lock(command="pytest"), _lock(command="ruff"))

    assert delta.kind is CapabilityDeltaKind.INCOMPATIBLE


def test_profile_description_change_is_metadata_only() -> None:
    current = _lock(command="pytest").replace(
        'commands = [["pytest"]]', 'description = "old"\ncommands = [["pytest"]]'
    )
    candidate = current.replace('description = "old"', 'description = "new"')

    delta = classify_capability_delta(current, candidate)

    assert delta.kind is CapabilityDeltaKind.METADATA_ONLY
