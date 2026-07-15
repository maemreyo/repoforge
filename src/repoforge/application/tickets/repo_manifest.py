"""Locate and load the checked-in ticket graph for an enrolled repository."""

from __future__ import annotations

from pathlib import Path

from ...domain.tickets import TicketGraph
from .graph import load_ticket_graph

MANIFEST_RELATIVE_PATH = Path("docs/roadmaps/REPOFORGE_TICKET_GRAPH.json")


def ticket_graph_path(repo_path: Path) -> Path:
    return repo_path / MANIFEST_RELATIVE_PATH


def load_repo_ticket_graph(repo_path: Path) -> TicketGraph | None:
    """Load the repository's checked-in ticket graph, or None if it has none."""
    path = ticket_graph_path(repo_path)
    if not path.is_file():
        return None
    return load_ticket_graph(path)
