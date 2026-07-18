"""`.repoforge/skills.yaml` binding (#205): one line gives a skill full delivery semantics
without forking it. Untrusted input: `yaml.safe_load` only, bounded file size.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ...domain.delivery import DeliveryClass, validate_always_cap

_MAX_BINDING_FILE_BYTES = 500_000


@dataclass(frozen=True, slots=True)
class SkillBinding:
    skill: str
    paths: tuple[str, ...] = ()
    phase: str | None = None
    delivery: DeliveryClass = DeliveryClass.ON_ENTRY


def load_bindings(repo_root: Path) -> tuple[SkillBinding, ...]:
    path = repo_root / ".repoforge" / "skills.yaml"
    if not path.is_file():
        return ()
    if path.stat().st_size > _MAX_BINDING_FILE_BYTES:
        raise ValueError(f"{path} exceeds the {_MAX_BINDING_FILE_BYTES}-byte bound")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} is not valid YAML") from exc
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a YAML list of bindings")

    bindings: list[SkillBinding] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("skill"), str):
            raise ValueError(f"{path} has a binding entry missing a 'skill' string")
        paths_raw = entry.get("paths", [])
        if not isinstance(paths_raw, list) or not all(isinstance(item, str) for item in paths_raw):
            raise ValueError(f"{path}: binding for {entry['skill']!r} has invalid 'paths'")
        phase = entry.get("phase")
        if phase is not None and not isinstance(phase, str):
            raise ValueError(f"{path}: binding for {entry['skill']!r} has an invalid 'phase'")
        delivery_raw = entry.get("delivery", DeliveryClass.ON_ENTRY.value)
        try:
            delivery = DeliveryClass(delivery_raw)
        except ValueError as exc:
            raise ValueError(
                f"{path}: binding for {entry['skill']!r} has an invalid delivery: {delivery_raw!r}"
            ) from exc
        bindings.append(
            SkillBinding(
                skill=entry["skill"], paths=tuple(paths_raw), phase=phase, delivery=delivery
            )
        )

    validate_always_cap({binding.skill: binding.delivery for binding in bindings})
    return tuple(bindings)
