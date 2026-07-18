"""Coverage for `.repoforge/skills.yaml` binding (#205)."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.application.skills.binding import load_bindings
from repoforge.domain.delivery import DeliveryCapExceededError, DeliveryClass


def test_absent_skills_yaml_yields_no_bindings(tmp_path: Path) -> None:
    assert load_bindings(tmp_path) == ()


def test_binding_is_parsed_with_defaults(tmp_path: Path) -> None:
    repoforge_dir = tmp_path / ".repoforge"
    repoforge_dir.mkdir()
    (repoforge_dir / "skills.yaml").write_text(
        "- skill: domain-conventions\n  paths: ['src/**/domain/**']\n", encoding="utf-8"
    )
    bindings = load_bindings(tmp_path)
    assert len(bindings) == 1
    assert bindings[0].skill == "domain-conventions"
    assert bindings[0].paths == ("src/**/domain/**",)
    assert bindings[0].delivery is DeliveryClass.ON_ENTRY
    assert bindings[0].phase is None


def test_binding_with_explicit_phase_and_delivery(tmp_path: Path) -> None:
    repoforge_dir = tmp_path / ".repoforge"
    repoforge_dir.mkdir()
    (repoforge_dir / "skills.yaml").write_text(
        "- skill: ponytail-planning\n  phase: plan\n  delivery: always\n", encoding="utf-8"
    )
    bindings = load_bindings(tmp_path)
    assert bindings[0].phase == "plan"
    assert bindings[0].delivery is DeliveryClass.ALWAYS


def test_missing_skill_field_is_rejected(tmp_path: Path) -> None:
    repoforge_dir = tmp_path / ".repoforge"
    repoforge_dir.mkdir()
    (repoforge_dir / "skills.yaml").write_text("- paths: ['x']\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_bindings(tmp_path)


def test_sixth_always_binding_is_rejected(tmp_path: Path) -> None:
    repoforge_dir = tmp_path / ".repoforge"
    repoforge_dir.mkdir()
    entries = "\n".join(f"- skill: s{i}\n  delivery: always" for i in range(6))
    (repoforge_dir / "skills.yaml").write_text(entries + "\n", encoding="utf-8")
    with pytest.raises(DeliveryCapExceededError):
        load_bindings(tmp_path)
