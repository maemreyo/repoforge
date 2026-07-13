"""Shared deterministic parsing for global and repository-scoped onboarding inputs."""

from __future__ import annotations


def parse_assignments(values: tuple[str, ...] | list[str], *, option: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, selected = value.partition("=")
        if not separator or not key or not selected:
            raise ValueError(f"{option} must use CODE=CHOICE or REPO_ID.CODE=CHOICE")
        result[key] = selected
    return result


def for_repository(values: dict[str, str], repo_id: str) -> dict[str, str]:
    selected = {key: value for key, value in values.items() if "." not in key}
    prefix = f"{repo_id}."
    selected.update(
        {key.removeprefix(prefix): value for key, value in values.items() if key.startswith(prefix)}
    )
    return selected
