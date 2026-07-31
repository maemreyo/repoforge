"""Read-only topology inspection and exact publication effects."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from ..domain.errors import ErrorCode, RepoForgeError
from ..domain.git_transport_identity import GitTransportSpec
from ..domain.github_capability_preflight import (
    GitHubCapabilityPreflightRequest,
    GitHubOperationCapability,
    authorize_github_capabilities,
    github_capability_requirements,
)
from ..domain.publication import (
    PublicationEvidence,
    RemoteTopology,
    RepositoryEndpoint,
    ReviewedPublication,
    review_publication,
)
from ..domain.repository_auth_broker import ProcessAuthContext
from ..domain.repository_identity import (
    IdentityEvidenceKind,
    IdentitySurface,
    PublicationIntent,
    PublicationKind,
)
from ..ports.command import CommandExecutor
from ..ports.git_transport import GitTransportGateway
from ..ports.github_capability_preflight import GitHubCapabilityPreflightGateway
from ..ports.publication import (
    GitHubPublicationGateway,
    PublicationAuthorization,
    PublicationAuthorizationGateway,
    PublicationEffect,
    PublicationRepositoryMetadata,
    PublicationRepositoryResolver,
    PullRequestPublication,
)

_REWRITE_KEY = re.compile(r"^url\.(.+)\.(insteadOf|pushInsteadOf)$")


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _publication_error(code: ErrorCode, message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=("No external publication effect was started.",),
        safe_next_action="Re-inspect the exact repository topology and identity before retrying.",
    )


class PublicationAdapter:
    """Own topology inspection, write-time review, exact effects, and reconciliation."""

    def __init__(
        self,
        *,
        commands: CommandExecutor,
        repositories: PublicationRepositoryResolver,
        authorization: PublicationAuthorizationGateway,
        capability_preflight: GitHubCapabilityPreflightGateway,
        transport: GitTransportGateway,
        github: GitHubPublicationGateway,
        clock: Callable[[], str],
    ) -> None:
        self._commands = commands
        self._repositories = repositories
        self._authorization = authorization
        self._capability_preflight = capability_preflight
        self._transport = transport
        self._github = github
        self._clock = clock

    def _git_lines(self, cwd: Path, argv: list[str]) -> tuple[str, ...]:
        result = self._commands.run(["git", *argv], cwd=cwd, check=False, output_limit=1_000_000)
        return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())

    def _rewrites(self, cwd: Path) -> tuple[tuple[str, str, str], ...]:
        rows = self._git_lines(
            cwd,
            ["config", "--get-regexp", r"^url\..*\.(insteadOf|pushInsteadOf)$"],
        )
        rules: list[tuple[str, str, str]] = []
        for row in rows:
            fields = row.split(None, 1)
            if len(fields) != 2:
                raise _publication_error(
                    ErrorCode.EVIDENCE_INVALID,
                    "Git URL rewrite configuration returned malformed evidence.",
                )
            match = _REWRITE_KEY.fullmatch(fields[0])
            if match is None or not fields[1]:
                raise _publication_error(
                    ErrorCode.EVIDENCE_INVALID,
                    "Git URL rewrite configuration returned malformed evidence.",
                )
            rules.append((match.group(1), match.group(2), fields[1]))
        return tuple(rules)

    @staticmethod
    def _rewrite(
        url: str,
        rules: tuple[tuple[str, str, str], ...],
        *,
        push: bool,
    ) -> str:
        candidates = [
            (base, kind, prefix)
            for base, kind, prefix in rules
            if url.startswith(prefix) and (kind == "insteadOf" or push)
        ]
        if not candidates:
            return url
        base, _kind, prefix = max(
            candidates,
            key=lambda item: (len(item[2]), item[1] == "pushInsteadOf"),
        )
        return base + url[len(prefix) :]

    @staticmethod
    def _endpoint(
        metadata: PublicationRepositoryMetadata,
        *,
        exact_ref: str | None,
        urls: tuple[str, ...],
    ) -> RepositoryEndpoint:
        digests = tuple(sorted({_digest(url) for url in urls}))
        if not digests:
            digests = (_digest(f"repository-id:{metadata.repository_id}"),)
        return RepositoryEndpoint(
            repository_id=metadata.repository_id,
            canonical_name=metadata.canonical_name,
            boundary_id=metadata.boundary_id,
            exact_ref=exact_ref,
            url_digests=digests,
        )

    def _resolve_urls(
        self,
        urls: tuple[str, ...],
    ) -> tuple[PublicationRepositoryMetadata, tuple[str, ...]]:
        if not urls:
            raise _publication_error(
                ErrorCode.PUBLICATION_TARGET_MISMATCH,
                "Publication remote has no reviewed URL.",
            )
        metadata = tuple(self._repositories.resolve_url(url) for url in urls)
        repository_ids = {item.repository_id for item in metadata}
        if len(repository_ids) != 1:
            raise _publication_error(
                ErrorCode.PUBLICATION_TARGET_MISMATCH,
                "One remote resolves to multiple stable repository IDs.",
            )
        return metadata[0], tuple(dict.fromkeys(urls))

    def _inspect_with_push_url(
        self,
        cwd: Path,
        intent: PublicationIntent,
    ) -> tuple[RemoteTopology, str]:
        fetch_raw = self._git_lines(
            cwd,
            ["config", "--get-all", f"remote.{intent.remote_name}.url"],
        )
        push_raw = (
            self._git_lines(
                cwd,
                ["config", "--get-all", f"remote.{intent.remote_name}.pushurl"],
            )
            or fetch_raw
        )
        rewrites = self._rewrites(cwd)
        fetch_urls = tuple(self._rewrite(url, rewrites, push=False) for url in fetch_raw)
        push_urls = tuple(
            dict.fromkeys(self._rewrite(url, rewrites, push=True) for url in push_raw)
        )
        if len(push_urls) != 1:
            raise _publication_error(
                ErrorCode.PUBLICATION_TARGET_MISMATCH,
                "Managed publication requires exactly one effective push URL.",
            )
        fetch_metadata, fetch_urls = self._resolve_urls(fetch_urls)
        push_metadata, push_urls = self._resolve_urls(push_urls)
        fetch = self._endpoint(fetch_metadata, exact_ref=intent.source_ref, urls=fetch_urls)
        push = self._endpoint(push_metadata, exact_ref=intent.destination_ref, urls=push_urls)
        if intent.kind is PublicationKind.PULL_REQUEST:
            base_metadata = self._repositories.resolve_id(intent.destination_repository_id)
            head_metadata = self._repositories.resolve_id(intent.source_repository_id)
            base_urls = fetch_urls if base_metadata.repository_id == fetch.repository_id else ()
            head_urls = push_urls if head_metadata.repository_id == push.repository_id else ()
            base = self._endpoint(base_metadata, exact_ref=intent.base_ref, urls=base_urls)
            head = self._endpoint(head_metadata, exact_ref=intent.head_ref, urls=head_urls)
        else:
            base = replace(fetch, exact_ref=intent.source_ref)
            head = replace(push, exact_ref=intent.destination_ref)
        topology = RemoteTopology(
            remote_name=intent.remote_name,
            fetch=fetch,
            push=push,
            base=base,
            head=head,
            source_ref=intent.source_ref,
            destination_ref=intent.destination_ref,
            rewrite_digest=_digest(rewrites),
            observed_at=self._clock(),
        )
        return topology, push_urls[0]

    def inspect(self, cwd: Path, intent: PublicationIntent) -> RemoteTopology:
        topology, _push_url = self._inspect_with_push_url(cwd, intent)
        return topology

    def _source_objects(self, cwd: Path, source_ref: str) -> tuple[str, str]:
        commit_rows = self._git_lines(cwd, ["rev-parse", f"{source_ref}^{{commit}}"])
        tree_rows = self._git_lines(cwd, ["rev-parse", f"{source_ref}^{{tree}}"])
        if len(commit_rows) != 1 or len(tree_rows) != 1:
            raise _publication_error(
                ErrorCode.EVIDENCE_INVALID,
                "Publication source commit or tree evidence is incomplete.",
            )
        return commit_rows[0].lower(), tree_rows[0].lower()

    @staticmethod
    def _requested_capabilities(
        requested_capability_ids: tuple[str, ...],
    ) -> tuple[GitHubOperationCapability, ...]:
        if not requested_capability_ids:
            raise _publication_error(
                ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
                "Publication requires at least one exact GitHub operation capability.",
            )
        try:
            capabilities = tuple(
                GitHubOperationCapability(item) for item in requested_capability_ids
            )
        except ValueError as exc:
            raise _publication_error(
                ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
                "Publication requested an unknown GitHub operation capability.",
            ) from exc
        if len(set(capabilities)) != len(capabilities):
            raise _publication_error(
                ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
                "Publication GitHub operation capabilities must be unique.",
            )
        return capabilities

    @staticmethod
    def _pinned_metadata(
        expected: PublicationAuthorization,
        key: str,
    ) -> str:
        value = dict(expected.lease.provider_metadata).get(key)
        if not isinstance(value, str) or not value:
            raise _publication_error(
                ErrorCode.EVIDENCE_INVALID,
                f"Publication auth lease is missing pinned {key} evidence.",
            )
        return value

    def _capability_authorization(
        self,
        cwd: Path,
        intent: PublicationIntent,
        expected: PublicationAuthorization,
        *,
        requested_capability_ids: tuple[str, ...],
        auth_context: ProcessAuthContext,
    ) -> tuple[PublicationAuthorization, str]:
        capabilities = self._requested_capabilities(requested_capability_ids)
        requirements = github_capability_requirements()
        permission_ids = tuple(
            sorted({requirements[capability].permission_id for capability in capabilities})
        )
        request = GitHubCapabilityPreflightRequest(
            host=self._pinned_metadata(expected, "github_host"),
            actor_id=expected.actor_id,
            repository_id=intent.destination_repository_id,
            installation_id=expected.installation_id,
            capability_ids=capabilities,
            permission_ids=permission_ids,
            config_revision=expected.lease.config_revision,
            policy_revision=expected.lease.policy_revision,
            observed_at=self._clock(),
        )
        report = authorize_github_capabilities(
            self._capability_preflight.preflight(cwd, request, auth_context)
        )
        metadata = dict(expected.lease.provider_metadata)
        profile_capability = metadata.get("github_capability_digest")
        profile_permission = metadata.get("github_permission_digest")
        pinned_config = metadata.get("config_revision")
        pinned_policy = metadata.get("policy_revision")
        if report.repository_id != intent.destination_repository_id:
            raise _publication_error(
                ErrorCode.GITHUB_API_REPOSITORY_MISMATCH,
                "Write-time GitHub preflight observed a different repository.",
            )
        if (
            report.actor_id != expected.actor_id
            or report.installation_id != expected.installation_id
            or report.config_revision != expected.lease.config_revision
            or report.policy_revision != expected.lease.policy_revision
            or (pinned_config is not None and pinned_config != report.config_revision)
            or (pinned_policy is not None and pinned_policy != report.policy_revision)
        ):
            raise _publication_error(
                ErrorCode.OPERATION_IDENTITY_MISMATCH,
                "Write-time GitHub preflight changed a pinned publication identity field.",
            )
        if report.capability_ids != capabilities:
            raise _publication_error(
                ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
                "Write-time GitHub preflight returned a different capability set.",
            )
        if report.permission_ids != permission_ids:
            raise _publication_error(
                ErrorCode.GITHUB_API_PERMISSION_DENIED,
                "Write-time GitHub preflight returned a different permission set.",
            )
        if (
            not isinstance(profile_capability, str)
            or report.capability_digest != expected.capability_digest
        ):
            raise _publication_error(
                ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
                "Write-time GitHub capability digest changed before publication.",
            )
        if (
            not isinstance(profile_permission, str)
            or report.permission_digest != expected.permission_digest
        ):
            raise _publication_error(
                ErrorCode.GITHUB_API_PERMISSION_DENIED,
                "Write-time GitHub permission digest changed before publication.",
            )

        live = self._authorization.revalidate(
            intent,
            expected,
            requested_capability_ids=requested_capability_ids,
            auth_context=auth_context,
        )
        surfaces = tuple(
            replace(
                item,
                observed_at=report.observed_at,
                evidence_digest=report.evidence_digest,
            )
            if item.surface is IdentitySurface.GITHUB_API
            else item
            for item in live.identity_surfaces
        )
        return (
            replace(
                live,
                identity_surfaces=surfaces,
                capability_digest=report.capability_digest,
                permission_digest=report.permission_digest,
                observed_at=report.observed_at,
            ),
            report.evidence_digest,
        )

    def revalidate(
        self,
        cwd: Path,
        intent: PublicationIntent,
        preflight: RemoteTopology,
        expected_authorization: PublicationAuthorization,
        *,
        requested_capability_ids: tuple[str, ...],
        auth_context: ProcessAuthContext,
        transport_spec: GitTransportSpec | None = None,
    ) -> ReviewedPublication:
        live, preflight_evidence_digest = self._capability_authorization(
            cwd,
            intent,
            expected_authorization,
            requested_capability_ids=requested_capability_ids,
            auth_context=auth_context,
        )
        observed, push_url = self._inspect_with_push_url(cwd, intent)
        if transport_spec is not None:
            requested_ref = (
                intent.head_ref
                if intent.kind is PublicationKind.PULL_REQUEST
                else intent.destination_ref
            )
            if requested_ref is None:
                raise _publication_error(
                    ErrorCode.PUBLICATION_TARGET_MISMATCH,
                    "Publication transport proof is missing its exact destination ref.",
                )
            transport_evidence = self._transport.ls_remote(
                cwd,
                push_url,
                requested_ref,
                transport_spec,
                auth_context,
            )
            if (
                transport_evidence.profile_id != live.profile_id
                or transport_evidence.repository_id != intent.destination_repository_id
            ):
                raise _publication_error(
                    ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
                    "Live Git transport proof changed the pinned publication identity.",
                )
            remote_version = _digest(
                {
                    "repository_id": intent.destination_repository_id,
                    "destination_ref": requested_ref,
                    "remote_head": transport_evidence.observed_sha,
                }
            )
            effect_surface = (
                IdentitySurface.PULL_REQUEST
                if intent.kind is PublicationKind.PULL_REQUEST
                else IdentitySurface.GIT_PUSH
            )
            surfaces = tuple(
                replace(
                    item,
                    evidence_kind=IdentityEvidenceKind.TRANSPORT_ACCESS_PROOF,
                    observed_at=self._clock(),
                    evidence_digest=_digest(transport_evidence.safe_payload()),
                )
                if item.surface is effect_surface
                else item
                for item in live.identity_surfaces
            )
            live = replace(
                live,
                identity_surfaces=surfaces,
                remote_version=remote_version,
            )
        commit_sha, tree_sha = self._source_objects(cwd, intent.source_ref)
        evidence = PublicationEvidence(
            operation_id=intent.operation_id,
            profile_id=live.profile_id,
            actor_class=live.actor_class,
            actor_id=live.actor_id,
            installation_id=live.installation_id,
            lease=live.lease,
            identity_surfaces=live.identity_surfaces,
            preflight_topology=preflight,
            observed_topology=observed,
            observed_commit_sha=commit_sha,
            observed_tree_sha=tree_sha,
            expected_capability_digest=expected_authorization.capability_digest,
            observed_capability_digest=live.capability_digest,
            expected_permission_digest=expected_authorization.permission_digest,
            observed_permission_digest=live.permission_digest,
            expected_remote_version=expected_authorization.remote_version,
            observed_remote_version=live.remote_version,
            preflight_evidence_digest=preflight_evidence_digest,
            approved_cross_boundary_id=live.approved_cross_boundary_id,
            observed_at=live.observed_at,
        )
        return review_publication(intent, evidence)

    def _effect_topology(
        self,
        cwd: Path,
        reviewed: ReviewedPublication,
        topology: RemoteTopology,
    ) -> tuple[RemoteTopology, str]:
        intent = PublicationIntent(
            publication_id=reviewed.publication_id,
            operation_id=reviewed.operation_id,
            kind=reviewed.kind,
            source_repository_id=reviewed.source_repository_id,
            destination_repository_id=reviewed.destination_repository_id,
            remote_name=topology.remote_name,
            source_ref=topology.source_ref,
            destination_ref=topology.destination_ref,
            expected_commit_sha=reviewed.commit_sha,
            expected_tree_sha=reviewed.tree_sha,
            base_ref=topology.base.exact_ref
            if reviewed.kind is PublicationKind.PULL_REQUEST
            else None,
            head_ref=topology.head.exact_ref
            if reviewed.kind is PublicationKind.PULL_REQUEST
            else None,
            cross_boundary_approval_id=reviewed.cross_boundary_approval_id,
        )
        current, push_url = self._inspect_with_push_url(cwd, intent)
        if (
            topology.topology_digest != reviewed.topology_digest
            or current.topology_digest != reviewed.topology_digest
        ):
            raise _publication_error(
                ErrorCode.PUBLICATION_TARGET_MISMATCH,
                "Publication topology changed after write-time review.",
            )
        return current, push_url

    def publish(
        self,
        cwd: Path,
        reviewed: ReviewedPublication,
        topology: RemoteTopology,
        *,
        transport_spec: GitTransportSpec,
        auth_context: ProcessAuthContext,
        pull_request: PullRequestPublication | None = None,
    ) -> PublicationEffect:
        current, push_url = self._effect_topology(cwd, reviewed, topology)
        if reviewed.kind is PublicationKind.GIT_PUSH:
            if pull_request is not None:
                raise ValueError("Git push publication cannot include pull-request content")
            self._transport.push(
                cwd,
                push_url,
                reviewed.exact_refspec,
                transport_spec,
                auth_context,
            )
            return PublicationEffect(
                publication_id=reviewed.publication_id,
                kind=reviewed.kind,
                destination_repository_id=reviewed.destination_repository_id,
                destination_ref=current.destination_ref,
                commit_sha=reviewed.commit_sha,
                external_id=reviewed.review_digest,
                url=None,
                reconciled=False,
            )
        if reviewed.kind is PublicationKind.PULL_REQUEST:
            if pull_request is None:
                raise ValueError("Pull-request publication requires reviewed title and body")
            base_ref = current.base.exact_ref
            head_ref = current.head.exact_ref
            if base_ref is None or head_ref is None:
                raise _publication_error(
                    ErrorCode.PUBLICATION_TARGET_MISMATCH,
                    "Pull-request topology is missing exact base or head refs.",
                )
            return self._github.create_pull_request(
                cwd=cwd,
                publication_id=reviewed.publication_id,
                base_repository_id=current.base.repository_id,
                head_repository_id=current.head.repository_id,
                base_repository=current.base.canonical_name,
                head_repository=current.head.canonical_name,
                base_ref=base_ref,
                head_ref=head_ref,
                expected_commit_sha=reviewed.commit_sha,
                title=pull_request.title.strip(),
                body=pull_request.body,
                auth_context=auth_context,
            )
        raise _publication_error(
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
            "This publication adapter does not implement the requested effect kind.",
        )

    def reconcile(
        self,
        cwd: Path,
        reviewed: ReviewedPublication,
        topology: RemoteTopology,
        *,
        transport_spec: GitTransportSpec,
        auth_context: ProcessAuthContext,
    ) -> PublicationEffect | None:
        current, push_url = self._effect_topology(cwd, reviewed, topology)
        if reviewed.kind is PublicationKind.GIT_PUSH:
            evidence = self._transport.ls_remote(
                cwd,
                push_url,
                current.destination_ref,
                transport_spec,
                auth_context,
            )
            if evidence.observed_sha != reviewed.commit_sha:
                return None
            return PublicationEffect(
                publication_id=reviewed.publication_id,
                kind=reviewed.kind,
                destination_repository_id=reviewed.destination_repository_id,
                destination_ref=current.destination_ref,
                commit_sha=reviewed.commit_sha,
                external_id=reviewed.review_digest,
                url=None,
                reconciled=True,
            )
        if reviewed.kind is PublicationKind.PULL_REQUEST:
            base_ref = current.base.exact_ref
            head_ref = current.head.exact_ref
            if base_ref is None or head_ref is None:
                return None
            effect = self._github.find_pull_request(
                cwd=cwd,
                publication_id=reviewed.publication_id,
                base_repository_id=current.base.repository_id,
                head_repository_id=current.head.repository_id,
                base_repository=current.base.canonical_name,
                head_repository=current.head.canonical_name,
                base_ref=base_ref,
                head_ref=head_ref,
                expected_commit_sha=reviewed.commit_sha,
                auth_context=auth_context,
            )
            return replace(effect, reconciled=True) if effect is not None else None
        return None


__all__ = ["PublicationAdapter"]
