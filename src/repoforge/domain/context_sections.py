"""The one place a task-context section name is declared.

A section name has to be known by the public contract (to advertise and validate it), by
the application engine (to accept it and to pick a default set), and by whatever builds the
section. Before this module those were three independent literal lists plus a builder
chain, coupled only by string value -- so a name could be added to one and missed in
another, and the symptom would be a contract that accepts a request the engine then
rejects.

This lives in ``domain`` rather than ``contracts`` because ``application`` must be able to
read it too, and ``application`` does not depend on the contract layer. ``contracts``
re-exports it, so the generated JSON Schema is unchanged.
"""

from __future__ import annotations

from enum import Enum


class ContextSectionName(str, Enum):
    REPOSITORY = "repository"
    STATUS = "status"
    TICKET = "ticket"
    TICKET_WORKFLOW = "ticket_workflow"
    WORKSPACE = "workspace"
    RECENT_COMMITS = "recent_commits"
    RULES = "rules"


#: What a caller gets when it asks for no particular sections. Deliberately not every
#: section: `ticket_workflow` is a resume path, and `rules` costs a git snapshot read, so
#: neither belongs in an ordinary first look. Changing this changes what every caller that
#: passes no `sections` receives, without any of them changing a line.
DEFAULT_CONTEXT_SECTIONS: tuple[ContextSectionName, ...] = (
    ContextSectionName.REPOSITORY,
    ContextSectionName.STATUS,
    ContextSectionName.TICKET,
    ContextSectionName.WORKSPACE,
    ContextSectionName.RECENT_COMMITS,
)

#: The same set as plain strings, for the application layer's command shape, which carries
#: transport values rather than enum members.
DEFAULT_CONTEXT_SECTION_VALUES: tuple[str, ...] = tuple(
    item.value for item in DEFAULT_CONTEXT_SECTIONS
)

#: Every name the engine will accept, derived rather than restated.
CONTEXT_SECTION_VALUES: frozenset[str] = frozenset(item.value for item in ContextSectionName)


__all__ = [
    "CONTEXT_SECTION_VALUES",
    "DEFAULT_CONTEXT_SECTIONS",
    "DEFAULT_CONTEXT_SECTION_VALUES",
    "ContextSectionName",
]
