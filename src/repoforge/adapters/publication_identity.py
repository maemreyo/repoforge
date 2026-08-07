"""Durable publication repository resolution and pinned authorization checks."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from ..domain.errors import ErrorCode, RepoForgeError
from ..domain.git_remote_identity import ReviewedSshEndpoint
from ..domain.repository_auth_broker import ProcessAuthContext
from ..domain.repository_identity import (
    AuthTargetKind,
    PublicationIntent,
    RepositoryIdentityBinding,
)
from ..ports.publication import (
    PublicationAuthorization,
    PublicationRepositoryMetadata,
)
from ..ports.repository_binding_store import RepositoryBindingStore

_SCP_URL = re.compile(r"^(?:(?:[A-Za-z0-9._-]+)@)?(?P<host>[A-Za-z0-9.-]+):(?P<path>[^\x00]+)$")


def _error(code: ErrorCode, message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=("No external publication effect was started.",),
    )


def _canonical_url(
    url: str,
    *,
    ssh_endpoints: tuple[ReviewedSshEndpoint, ...] = (),
) -> str:
    if not isinstance(url, str) or not url or "\x00" in url:
        raise _error(ErrorCode.PUBLICATION_TARGET_MISMATCH, "Publication URL is invalid.")
    host: str | None
    path: str
    if "://" in url:
        parsed = urlsplit(url)
        host = parsed.hostname
        path = parsed.path
    else:
        match = _SCP_URL.fullmatch(url)
        if match is None:
            raise _error(
                ErrorCode.PUBLICATION_TARGET_MISMATCH,
                "Publication URL is not an explicit Git repository URL.",
            )
        host = match.group("host")
        path = match.group("path")
    parts = path.strip("/").removesuffix(".git").split("/")
    if host is None or len(parts) != 2 or not all(parts):
        raise _error(
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
            "Publication URL must identify exactly one owner/repository.",
        )
    normalized_host = host.lower()
    reviewed_hosts = {
        endpoint.canonical_host
        for endpoint in ssh_endpoints
        if endpoint.raw_host == normalized_host
        and endpoint.owner == parts[0]
        and endpoint.repository == parts[1]
    }
    if len(reviewed_hosts) > 1:
        raise _error(
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
            "Publication SSH alias resolves to conflicting reviewed repository endpoints.",
        )
    canonical_host = next(iter(reviewed_hosts), normalized_host)
    return f"{canonical_host}/{parts[0]}/{parts[1]}"


def _metadata(repository_id: str, canonical_name: str) -> PublicationRepositoryMetadata:
    owner = "/".join(canonical_name.split("/")[:2])
    boundary_id = "boundary-" + hashlib.sha256(owner.encode()).hexdigest()[:24]
    return PublicationRepositoryMetadata(repository_id, canonical_name, boundary_id)


class DurableBindingPublicationRepositoryResolver:
    """Resolve effective URLs only through reviewed durable repository bindings."""

    def __init__(
        self,
        bindings: RepositoryBindingStore,
        *,
        ssh_endpoints: tuple[ReviewedSshEndpoint, ...] = (),
    ) -> None:
        self._bindings = bindings
        self._ssh_endpoints = ssh_endpoints

    def _values(self) -> tuple[RepositoryIdentityBinding, ...]:
        page = self._bindings.list_bindings(max_records=2_000)
        if page.scan_truncated or page.unreadable_record_ids:
            raise _error(
                ErrorCode.EVIDENCE_INVALID,
                "Durable repository bindings are incomplete; publication is denied.",
            )
        return tuple(record.value for record in page.records)

    def resolve_url(self, url: str) -> PublicationRepositoryMetadata:
        canonical = _canonical_url(url, ssh_endpoints=self._ssh_endpoints)
        matches = tuple(
            binding for binding in self._values() if binding.canonical_name == canonical
        )
        if len(matches) != 1:
            raise _error(
                ErrorCode.PUBLICATION_TARGET_MISMATCH,
                "Effective publication URL does not resolve to one durable repository binding.",
            )
        binding = matches[0]
        return _metadata(binding.repository_id, binding.canonical_name)

    def resolve_id(self, repository_id: str) -> PublicationRepositoryMetadata:
        matches = tuple(
            binding for binding in self._values() if binding.repository_id == repository_id
        )
        if len(matches) != 1:
            raise _error(
                ErrorCode.PUBLICATION_TARGET_MISMATCH,
                "Publication repository ID does not resolve to one durable binding.",
            )
        binding = matches[0]
        return _metadata(binding.repository_id, binding.canonical_name)


class PinnedPublicationAuthorizationGateway:
    """Reject any revalidation that escapes the admitted lease and process context."""

    def revalidate(
        self,
        intent: PublicationIntent,
        expected: PublicationAuthorization,
        *,
        requested_capability_ids: tuple[str, ...],
        auth_context: ProcessAuthContext,
    ) -> PublicationAuthorization:
        lease = expected.lease
        if (
            lease.repository_id != intent.destination_repository_id
            or lease.target_kind is not AuthTargetKind.REPOSITORY
            or lease.target_id != intent.destination_repository_id
            or lease.profile_id != expected.profile_id
            or lease.actor_id != expected.actor_id
            or auth_context.profile_id != expected.profile_id
            or auth_context.target_kind is not AuthTargetKind.REPOSITORY
            or auth_context.target_id != intent.destination_repository_id
        ):
            raise _error(
                ErrorCode.OPERATION_IDENTITY_MISMATCH,
                "Live publication authorization escaped its pinned repository identity.",
            )
        if not requested_capability_ids or any(
            not item.startswith("github.") for item in requested_capability_ids
        ):
            raise _error(
                ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
                "Publication revalidation requires exact GitHub capabilities.",
            )
        return expected


__all__ = [
    "DurableBindingPublicationRepositoryResolver",
    "PinnedPublicationAuthorizationGateway",
]
