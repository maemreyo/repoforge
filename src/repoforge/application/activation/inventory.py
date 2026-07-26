"""One answer to "what runtimes do I have, and how do I switch?" (`rf runtime ls`).

The information existed but was scattered across three commands with three shapes --
``rf runtime status`` for the live process, ``rf version list`` for releases, and
``rf dev-runtime list`` for candidates -- so nobody could hold the whole picture, and the
release identifiers were bare shas. This assembles one view keyed by labels a human
recognizes, and states the exact command that switches to each entry.

Pure assembly: every input is already-collected data, so this has no filesystem, process
or clock access and the CLI owns all the best-effort probing.
"""

from __future__ import annotations

from ...ports.activation import ObservedRuntime
from ...ports.process_supervisor import RegistrarStatus
from .selection import ReleaseChoice


def build_runtime_inventory(
    *,
    releases: list[ReleaseChoice],
    observed: ObservedRuntime | None,
    agent: RegistrarStatus | None,
    agent_secret_usable: bool,
    dev_runtimes: list[dict[str, object]],
) -> dict[str, object]:
    """Return the ``rf runtime ls`` payload."""
    current = next((choice for choice in releases if choice.is_current), None)
    previous = next((choice for choice in releases if choice.is_previous), None)
    observed_sha = observed.running_release_sha if observed is not None else None
    phase = observed.phase if observed is not None else "unknown"
    # The observed release may not be the desired one mid-activation, so it is reported
    # separately rather than assumed equal to `current`.
    observed_choice = next(
        (choice for choice in releases if choice.commit_sha == observed_sha), None
    )
    running_dev = [
        runtime for runtime in dev_runtimes if runtime.get("phase") not in {"stopped", None}
    ]
    return {
        "production": {
            "active_label": current.label if current is not None else None,
            "active_sha": current.commit_sha if current is not None else None,
            "active_branch": current.branch if current is not None else "",
            "active_subject": current.subject if current is not None else "",
            "observed_label": observed_choice.label if observed_choice is not None else None,
            "observed_sha": observed_sha,
            "converged": observed_sha is not None
            and observed_sha == (current.commit_sha if current is not None else None),
            "phase": phase,
            "pid": observed.pid if observed is not None else None,
            "os_resident": bool(agent.loaded) if agent is not None else False,
            "agent_registered": bool(agent.registered) if agent is not None else False,
            "durable_secret_usable": agent_secret_usable,
        },
        "rollback_target": (
            {
                "label": previous.label,
                "commit_sha": previous.commit_sha,
                "switch_command": f"rf version switch {previous.selector}",
            }
            if previous is not None
            else None
        ),
        "releases": [choice.as_dict() for choice in releases],
        "dev_runtimes": dev_runtimes,
        "counts": {
            "releases": len(releases),
            "dev_runtimes": len(dev_runtimes),
            "dev_runtimes_running": len(running_dev),
        },
        "safe_next_action": _next_action(current, releases, running_dev),
    }


def _next_action(
    current: ReleaseChoice | None,
    releases: list[ReleaseChoice],
    running_dev: list[dict[str, object]],
) -> str:
    if not releases:
        return "No release is installed yet. Run `rf upgrade --from-worktree . --activate`."
    if current is None:
        return "No release is active. Run `rf version switch <branch>` to activate one."
    if running_dev:
        names = ", ".join(str(runtime.get("name")) for runtime in running_dev)
        return f"Dev runtimes running alongside production: {names}."
    return f"Production serves {current.label}. Switch with `rf version switch <branch>`."
