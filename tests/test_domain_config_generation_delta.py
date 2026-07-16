from __future__ import annotations

from repoforge.domain.config_generation import (
    CapabilityDeltaKind,
    classify_capability_delta,
)


def _lock(
    *,
    command: str,
    generation: int = 1,
    execution_mode: str = "strict",
    adhoc_runners: tuple[str, ...] = (),
    adhoc_timeout_seconds: int = 300,
) -> str:
    runners = ", ".join(f'"{runner}"' for runner in adhoc_runners)
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
execution_mode = "{execution_mode}"
adhoc_runners = [{runners}]
adhoc_timeout_seconds = {adhoc_timeout_seconds}

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


def test_strict_to_relaxed_execution_is_expansion() -> None:
    current = _lock(command="pytest")
    candidate = _lock(command="pytest", execution_mode="relaxed", adhoc_runners=("uv",))

    delta = classify_capability_delta(current, candidate)

    assert delta.kind is CapabilityDeltaKind.EXPANSION
    assert {change.path for change in delta.changes} >= {
        "repositories.demo.execution_mode",
        "repositories.demo.adhoc_runners",
    }


def test_relaxed_to_strict_execution_is_restriction() -> None:
    current = _lock(command="pytest", execution_mode="relaxed", adhoc_runners=("uv",))
    candidate = _lock(command="pytest", execution_mode="strict", adhoc_runners=("uv",))

    delta = classify_capability_delta(current, candidate)

    assert delta.kind is CapabilityDeltaKind.RESTRICTION


def test_adhoc_runner_allowlist_addition_and_removal_are_directional() -> None:
    base = _lock(command="pytest", execution_mode="relaxed", adhoc_runners=("uv",))
    expanded = _lock(
        command="pytest",
        execution_mode="relaxed",
        adhoc_runners=("uv", "python3"),
    )

    assert classify_capability_delta(base, expanded).kind is CapabilityDeltaKind.EXPANSION
    assert classify_capability_delta(expanded, base).kind is CapabilityDeltaKind.RESTRICTION


def test_adhoc_timeout_increase_and_decrease_are_directional() -> None:
    short = _lock(
        command="pytest",
        execution_mode="relaxed",
        adhoc_runners=("uv",),
        adhoc_timeout_seconds=300,
    )
    long = _lock(
        command="pytest",
        execution_mode="relaxed",
        adhoc_runners=("uv",),
        adhoc_timeout_seconds=600,
    )

    assert classify_capability_delta(short, long).kind is CapabilityDeltaKind.EXPANSION
    assert classify_capability_delta(long, short).kind is CapabilityDeltaKind.RESTRICTION
