"""Name an installed release the way a human remembers it, not by full sha.

Switching runtimes per branch is unusable when every identifier is 40 hex characters: the
operator has to read a listing, copy a sha, and hope it was the right one. These resolvers
accept what someone actually recalls -- a branch name, or a short sha the way git does --
and refuse ambiguity out loud instead of guessing.

Pure functions over the :class:`ReleaseStore` boundary: no filesystem, process or clock
access of their own, so the CLI and any future caller share one resolution contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.activation import ReleaseManifest
from ...domain.errors import ConfigError
from ...ports import ReleaseStore

# git's own floor for an abbreviated object name. Shorter prefixes match too much to be a
# selection -- "a" would happily resolve to whichever release happened to sort first.
MIN_SHA_PREFIX = 4


@dataclass(frozen=True, slots=True)
class ReleaseChoice:
    """One installed release, described the way a listing should show it."""

    commit_sha: str
    branch: str
    subject: str
    built_at: str
    is_current: bool
    is_previous: bool

    @property
    def label(self) -> str:
        return self.branch or self.commit_sha[:12]

    def as_dict(self) -> dict[str, object]:
        return {
            "commit_sha": self.commit_sha,
            "short_sha": self.commit_sha[:12],
            "label": self.label,
            "branch": self.branch,
            "subject": self.subject,
            "built_at": self.built_at,
            "current": self.is_current,
            "previous": self.is_previous,
            # The exact command that makes this release live, so a listing is actionable
            # without the reader having to assemble one.
            "switch_command": f"rf version switch {self.label}",
        }


def release_choices(store: ReleaseStore) -> list[ReleaseChoice]:
    """Every installed release, newest first, annotated with current/previous."""
    current = store.current_sha()
    previous = store.previous_sha()
    return [
        ReleaseChoice(
            commit_sha=manifest.commit_sha,
            branch=manifest.branch,
            subject=manifest.subject,
            built_at=manifest.built_at,
            is_current=manifest.commit_sha == current,
            is_previous=manifest.commit_sha == previous,
        )
        for manifest in store.list_releases()
    ]


def resolve_release(store: ReleaseStore, selector: str) -> ReleaseManifest:
    """Resolve ``selector`` to exactly one installed release.

    Accepted, in this order: an exact commit sha; a branch name recorded at build time; a
    sha prefix of at least :data:`MIN_SHA_PREFIX` characters. Anything matching more than
    one release raises rather than picking, because picking silently would switch the live
    runtime to a release the operator did not name.
    """
    wanted = selector.strip()
    if not wanted:
        raise ConfigError("RELEASE_SELECTOR_EMPTY: name a release by branch or commit sha")
    manifests = store.list_releases()
    if not manifests:
        raise ConfigError("NO_RELEASES_INSTALLED: nothing to switch to")

    for manifest in manifests:
        if manifest.commit_sha == wanted:
            return manifest

    lowered = wanted.lower()
    by_branch = [manifest for manifest in manifests if manifest.branch == wanted]
    if len(by_branch) == 1:
        return by_branch[0]
    if len(by_branch) > 1:
        raise _ambiguous(wanted, by_branch, "branch")

    if all(character in "0123456789abcdef" for character in lowered):
        by_prefix = [manifest for manifest in manifests if manifest.commit_sha.startswith(lowered)]
        if len(lowered) < MIN_SHA_PREFIX:
            # Say why, instead of reporting "not found" for something that plainly does
            # prefix-match: a bare "not found" would send the reader looking for a release
            # that is right there in the list.
            if by_prefix:
                raise ConfigError(
                    f"RELEASE_SELECTOR_TOO_SHORT: {wanted!r} matches {len(by_prefix)} "
                    f"release(s) but a sha prefix must be at least {MIN_SHA_PREFIX} "
                    f"characters ({_known(by_prefix)})"
                )
        elif len(by_prefix) == 1:
            return by_prefix[0]
        elif len(by_prefix) > 1:
            raise _ambiguous(wanted, by_prefix, "sha prefix")

    raise ConfigError(
        f"RELEASE_NOT_FOUND: no installed release matches {wanted!r}. "
        "Known releases: " + _known(manifests) + ". Run `rf runtime ls` to see them all."
    )


def resolve_receipt_id(store: ReleaseStore, selector: str) -> str:
    """Resolve a receipt id, accepting a unique prefix of one.

    Receipt ids are long enough (``act-20260725-001``) that typing them in full is where
    rollback commands get mistyped, and a mistyped rollback is a refusal at best.
    """
    wanted = selector.strip()
    if not wanted:
        raise ConfigError("RECEIPT_SELECTOR_EMPTY: name a receipt id")
    ids = [receipt.receipt_id for receipt in store.receipt_history().valid]
    if wanted in ids:
        return wanted
    matches = [candidate for candidate in ids if candidate.startswith(wanted)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ConfigError(
            f"RECEIPT_SELECTOR_AMBIGUOUS: {wanted!r} matches {', '.join(sorted(matches))}"
        )
    # Not found among READABLE receipts. The id may still exist on disk as an unreadable
    # file, so the caller is passed through unchanged rather than refused here: the store
    # is the authority on existence, and it reports corruption with its own error.
    return wanted


def _ambiguous(selector: str, matches: list[ReleaseManifest], kind: str) -> ConfigError:
    return ConfigError(
        f"RELEASE_SELECTOR_AMBIGUOUS: {selector!r} matches {len(matches)} releases by "
        f"{kind} ({_known(matches)}). Use the full commit sha."
    )


def _known(manifests: list[ReleaseManifest]) -> str:
    return ", ".join(f"{manifest.label} ({manifest.commit_sha[:12]})" for manifest in manifests)
