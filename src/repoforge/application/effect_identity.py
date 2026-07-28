"""Bounded authoritative identity capture for durable mutation outcomes."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any

from ..domain.errors import RepoForgeError

if TYPE_CHECKING:
    from .context import ApplicationContext

_IDENTITY_KEYS = frozenset(
    {
        "workspace_id",
        "repo_id",
        "issue_number",
        "target_issue",
        "branch",
        "remote",
        "base",
        "head_sha",
        "previous_head_sha",
        "remote_head_before",
        "remote_head_after",
        "workspace_fingerprint",
        "sha256",
        "path",
        "url",
        "pr_number",
        "commit",
        "generation",
        "target_base_sha",
        "transaction_id",
    }
)


def _bounded_scalars(value: Any) -> dict[str, str | int | bool]:
    if value is None:
        return {}
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if not isinstance(value, dict):
        return {}
    selected: dict[str, str | int | bool] = {}
    for key, raw in value.items():
        if key not in _IDENTITY_KEYS or not isinstance(raw, (str, int, bool)):
            continue
        if isinstance(raw, str) and (
            not raw or len(raw) > 512 or any(ord(character) < 32 for character in raw)
        ):
            continue
        selected[key] = raw
    return selected


def capture_effect_identity(
    ctx: ApplicationContext,
    details: dict[str, Any] | None,
    *,
    result: Any | None = None,
) -> dict[str, str | int | bool]:
    """Capture a canonical, bounded pre/post identity without raw payload content."""

    identity = _bounded_scalars(details)
    workspace_id = identity.get("workspace_id")
    if isinstance(workspace_id, str):
        try:
            record, repo, workspace = ctx.workspace(workspace_id)
            identity.setdefault("repo_id", repo.repo_id)
            identity.setdefault("branch", record.branch)
            identity.setdefault("base", record.base)
            identity.setdefault("remote", record.remote)
            identity["head_sha"] = ctx.git.head_sha(workspace)
            identity["workspace_fingerprint"] = ctx.git.fingerprint(workspace)
        except RepoForgeError:
            # Workspace creation legitimately has no pre-state yet. The exact
            # request identity remains captured and the post-state adds it.
            pass
    identity.update(_bounded_scalars(result))
    return dict(sorted(identity.items()))
