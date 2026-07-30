"""A task-context section is declared once, and every declared one actually works.

Groundwork for #206, which adds four sections. Before this, a name was written out in
four independent places -- the contract enum, the engine's accepted set, the command's
default tuple, and the builder chain -- coupled only by string value. Adding a name to
some of them and not others produces a contract that advertises a section the engine
refuses, and nothing failed until a caller asked for it.

Three of those places now derive from one declaration in `domain`. The fourth, the
builder, cannot be derived -- so it is tested instead.
"""

from __future__ import annotations

import pytest
from conftest import ForgeEnvironment

from repoforge.application.repository.family_v2 import RepositoryTaskContextV2
from repoforge.contracts.v2 import ContextSectionName as ContractSectionName
from repoforge.contracts.v2 import RepoTaskContextInput
from repoforge.domain.context_sections import (
    CONTEXT_SECTION_VALUES,
    DEFAULT_CONTEXT_SECTION_VALUES,
    DEFAULT_CONTEXT_SECTIONS,
    ContextSectionName,
)

# -------------------------------------------------------- one declaration, three users


def test_the_contract_re_exports_the_domain_enum_rather_than_defining_its_own() -> None:
    """Same object, not merely the same values: two enums could drift while both looking right."""

    assert ContractSectionName is ContextSectionName


def test_the_engine_accepts_exactly_the_declared_names() -> None:
    assert RepositoryTaskContextV2._SECTIONS == CONTEXT_SECTION_VALUES
    assert {item.value for item in ContextSectionName} == CONTEXT_SECTION_VALUES


def test_the_contract_and_engine_defaults_are_the_same_set() -> None:
    """The default is what every caller that passes no `sections` receives."""

    contract_default = RepoTaskContextInput.model_fields["sections"].default

    assert tuple(item.value for item in contract_default) == DEFAULT_CONTEXT_SECTION_VALUES
    assert contract_default == DEFAULT_CONTEXT_SECTIONS


# ------------------------------------------------ the fourth site, which cannot derive


@pytest.mark.parametrize("section", sorted(CONTEXT_SECTION_VALUES))
def test_every_declared_section_is_actually_built(
    forge_env: ForgeEnvironment,
    section: str,
) -> None:
    """The builder chain is a hand-written branch per section, so it can fall behind.

    Adding a name to the enum without a branch would advertise a section that comes back
    missing. Asked for one at a time, so a failure names the section that is unbuilt.
    """

    service = forge_env.service
    workspace_id = service.workspace_create("demo", "section coverage")["workspace_id"]

    result = service.repo_task_context_v2(
        "demo",
        issue_number=55,
        workspace_id=workspace_id,
        sections=[section],
        byte_budget=20_000,
    )

    assert [item["name"] for item in result["sections"]] == [section], (
        f"section {section!r} is declared but produced no section"
    )


def test_an_undeclared_section_is_refused(forge_env: ForgeEnvironment) -> None:
    with pytest.raises(ValueError, match="Unknown task-context section"):
        forge_env.service.repo_task_context_v2("demo", sections=["not_a_section"])
