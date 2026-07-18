"""AGENTS.md/CLAUDE.md/CONTRIBUTING.md advisory ingestion (#205).

Root-level instruction files are budgeted advisory prose for the whole repository. A nested
`AGENTS.md` is scoped to its own subtree; `AGENTS.override.md` in the same directory replaces
it entirely for that subtree. Resolution always picks the single closest-directory document to
a target path -- never merges an ancestor's and a descendant's guidance together.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_ROOT_FILES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md")
_MAX_DOC_BYTES = 100_000
_MAX_NESTED_DOCS = 500


@dataclass(frozen=True, slots=True)
class AdvisoryDocument:
    path: str
    directory: str  # "" for repo root, else a repo-relative posix directory path
    content: str
    is_override: bool = False


def _read_bounded(path: Path) -> str | None:
    try:
        return path.read_bytes()[:_MAX_DOC_BYTES].decode("utf-8", errors="replace")
    except OSError:
        return None


def discover_advisory_documents(repo_root: Path) -> tuple[AdvisoryDocument, ...]:
    """Root files plus every nested AGENTS.md/AGENTS.override.md, deduplicated per directory
    (an override in a directory wins over a plain AGENTS.md in that same directory)."""

    documents: list[AdvisoryDocument] = []
    for name in _ROOT_FILES:
        content = _read_bounded(repo_root / name)
        if content is not None:
            documents.append(AdvisoryDocument(path=name, directory="", content=content))

    by_directory: dict[str, AdvisoryDocument] = {}
    count = 0
    for candidate in sorted(repo_root.rglob("AGENTS*.md")):
        if candidate.parent == repo_root or candidate.name not in (
            "AGENTS.md",
            "AGENTS.override.md",
        ):
            continue
        count += 1
        if count > _MAX_NESTED_DOCS:
            break
        content = _read_bounded(candidate)
        if content is None:
            continue
        directory = candidate.parent.relative_to(repo_root).as_posix()
        is_override = candidate.name == "AGENTS.override.md"
        existing = by_directory.get(directory)
        if existing is not None and existing.is_override and not is_override:
            continue  # an override already claimed this directory; a plain AGENTS.md loses
        by_directory[directory] = AdvisoryDocument(
            path=str(candidate.relative_to(repo_root)),
            directory=directory,
            content=content,
            is_override=is_override,
        )

    documents.extend(by_directory[directory] for directory in sorted(by_directory))
    return tuple(documents)


def resolve_advisory_for_path(
    documents: tuple[AdvisoryDocument, ...], target_path: str
) -> AdvisoryDocument | None:
    """Return the single closest-directory document governing `target_path`, or None.

    Root documents (directory == "") are the fallback; a nested document only applies when
    `target_path` actually falls under its directory.
    """

    target = PurePosixPath(target_path)
    best: AdvisoryDocument | None = None
    best_depth = -1
    for document in documents:
        if document.directory == "":
            continue
        directory = PurePosixPath(document.directory)
        if target == directory or directory in target.parents:
            depth = len(directory.parts)
            if depth > best_depth:
                best = document
                best_depth = depth
    if best is not None:
        return best
    return next((doc for doc in documents if doc.directory == "" and doc.path == "AGENTS.md"), None)
