#!/usr/bin/env python3
"""Validate and query the checked-in RepoForge ticket graph."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from repoforge.adapters.github.ticket_graph import GitHubTicketGraphReader
from repoforge.adapters.subprocess.command_executor import SubprocessCommandExecutor
from repoforge.application.tickets.graph import (
    compare_live_ticket_metadata,
    load_ticket_graph,
    select_ready_tickets,
    validate_ticket_graph,
)
from repoforge.config import ServerConfig
from repoforge.domain.tickets import TicketGraphError

DEFAULT_MANIFEST = Path("docs/roadmaps/REPOFORGE_TICKET_GRAPH.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--next", action="store_true", dest="show_next")
    parser.add_argument("--limit", type=int, default=7)
    parser.add_argument(
        "--live-repo",
        help="Reserved for the bounded read-only live drift adapter; offline validation remains authoritative for this command.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        graph = load_ticket_graph(args.manifest)
        diagnostics = validate_ticket_graph(graph)
        if args.live_repo:
            executor = SubprocessCommandExecutor(
                ServerConfig(workspace_root=Path.cwd(), state_root=Path.cwd())
            )
            live = GitHubTicketGraphReader(executor, cwd=Path.cwd()).read(
                args.live_repo,
                tuple(sorted(node.number for node in graph.nodes)),
            )
            diagnostics = tuple(sorted((*diagnostics, *compare_live_ticket_metadata(graph, live))))
        selected = select_ready_tickets(graph, limit=args.limit) if args.show_next else ()
    except TicketGraphError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
        return 2

    payload = {
        "diagnostics": [
            {
                "code": item.code,
                "issue_number": item.issue_number,
                "message": item.message,
            }
            for item in diagnostics
        ],
        "live_repo": args.live_repo,
        "manifest": str(args.manifest),
        "next": [
            {
                "number": item.number,
                "priority": item.priority.value,
                "status": item.status.value,
                "title": item.title,
            }
            for item in selected
        ],
        "node_count": len(graph.nodes),
        "program_issue": graph.program_issue,
        "valid": not diagnostics,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if not diagnostics else 1


if __name__ == "__main__":
    raise SystemExit(main())
