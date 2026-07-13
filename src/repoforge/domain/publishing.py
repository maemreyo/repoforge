"""Pure commit and pull-request validation and rendering."""

from __future__ import annotations


def validate_commit_message(message: str) -> str:
    normalized = message.strip()
    if not normalized or len(normalized) > 1000 or "\x00" in normalized:
        raise ValueError("Commit message must contain 1-1000 characters")
    return normalized


def validate_pr_update(title: str | None, body: str | None) -> tuple[str | None, str | None]:
    if title is None and body is None:
        raise ValueError("At least one of title or body must be provided")
    normalized_title = title.strip() if title is not None else None
    if normalized_title is not None and (not normalized_title or len(normalized_title) > 256):
        raise ValueError("PR title must contain 1-256 characters")
    if body is not None and len(body) > 100000:
        raise ValueError("PR body is too large")
    return (normalized_title, body)


def validate_pr_create(title: str, body: str) -> tuple[str, str]:
    normalized = title.strip()
    if not normalized or len(normalized) > 256:
        raise ValueError("PR title must contain 1-256 characters")
    if len(body) > 96000:
        raise ValueError("PR body is too large")
    return (normalized, body)


def render_pr_body(
    body: str,
    *,
    branch: str,
    head_sha: str,
    verification_profile: str | None,
    verification_completed_at: str | None,
) -> str:
    footer = f"\n\n<!-- repoforge -->\n---\nCreated by **RepoForge** from an isolated local worktree.\n\n- Branch: `{branch}`\n- Head: `{head_sha}`\n- Verification: `{verification_profile or 'not recorded'}`"
    if verification_completed_at:
        footer += f" at `{verification_completed_at}`"
    return body.rstrip() + footer + "\n"
