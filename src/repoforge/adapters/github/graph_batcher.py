"""Batched aliased GraphQL fetching with stripped-selection retry.

One ``_fetch_batch`` call fetches a frontier level of issues via aliased
``gh api graphql`` and retries failed aliases with progressively stripped
selections so optional-capability failures (comments, dependencies, sub-issues)
do not erase the core issue data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...config import ServerConfig
from ...domain.tickets import GraphEvidenceCapability
from ...ports.command import CommandExecutor
from .graph_decode import alias_issue, failed_capabilities
from .graph_query import FULL_SELECTION, build_query, selection_capabilities, stripped_selection
from .graph_transport import Stats, run_graphql


def fetch_batch(
    executor: CommandExecutor,
    server: ServerConfig,
    cwd: Path,
    slug: str,
    numbers: list[int],
    stats: Stats,
) -> tuple[dict[int, dict[str, Any]], dict[int, frozenset[GraphEvidenceCapability]]]:
    """Fetch one frontier level with aliased GraphQL, retrying failed
    aliases with progressively stripped selections.

    Returns ``(parsed_issue_dicts, failed_capabilities_by_number)``. A
    number present in ``parsed`` is usable; its entry in the second dict
    lists the capabilities that could not be read for it. Core issue
    identity/metadata is never accepted partially: any error on the issue
    object itself (including a missing object without an attributable
    error) fails the whole issue closed.
    """
    parsed: dict[int, dict[str, Any]] = {}
    failed: dict[int, set[GraphEvidenceCapability]] = {}
    pending: dict[int, str] = {number: FULL_SELECTION for number in numbers}
    while pending:
        selection = next(iter(pending.values()))
        group = [number for number, current in pending.items() if current == selection]
        query = build_query(slug, group, selection)
        payload, errors = run_graphql(
            executor,
            server,
            cwd,
            query,
            stats,
            capabilities=selection_capabilities(selection),
        )
        for index, number in enumerate(group):
            alias = f"r{index}"
            capabilities = failed_capabilities(errors, alias)
            issue = alias_issue(payload, alias)
            if issue is not None and not capabilities:
                parsed[number] = issue
                pending.pop(number, None)
                continue
            if GraphEvidenceCapability.ISSUE in capabilities:
                failed.setdefault(number, set()).add(GraphEvidenceCapability.ISSUE)
                pending.pop(number, None)
                continue
            if issue is not None:
                # Partial object with optional-capability failures: retry
                # stripped so accepted data only carries capabilities that
                # actually succeeded.
                failed.setdefault(number, set()).update(capabilities)
                next_selection = stripped_selection(failed[number])
                if next_selection == selection:
                    parsed[number] = issue
                    pending.pop(number, None)
                else:
                    pending[number] = next_selection
                continue
            # No issue object, only optional-capability failures: retry
            # stripped; if stripping changes nothing the object is truly
            # missing and the core read fails closed.
            failed.setdefault(number, set()).update(capabilities)
            next_selection = stripped_selection(failed[number])
            if next_selection == selection:
                failed.setdefault(number, set()).add(GraphEvidenceCapability.ISSUE)
                pending.pop(number, None)
            else:
                pending[number] = next_selection
    return parsed, {number: frozenset(caps) for number, caps in failed.items()}


__all__ = ["fetch_batch"]
