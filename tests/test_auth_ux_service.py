"""The identity facade: profiles, bindings, seven independent surfaces, doctor, and leases.

Binding tests run against a real JSON binding store, so what they catch is the durable state
change, not a mock call. Surface tests exist to prove the surfaces stay independent: success on
one must never be reported as success on another.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.json_repository_binding_store import (
    JsonRepositoryBindingStore,
)
from repoforge.application.auth_ux import (
    AUTH_SURFACE_ORDER,
    AuthSurface,
    AuthSurfaceState,
    AuthUxService,
)
from repoforge.config import AppConfig, AuthProfileConfig, RepositoryConfig, ServerConfig
from repoforge.domain.auth_profile import AuthProfileSelector, RequestedActorClass
from repoforge.domain.commit_identity import CommitIdentityEvidence, CommitSigningMode
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.git_transport_identity import (
    GitTransportAccess,
    GitTransportEvidence,
    GitTransportKind,
    GitTransportSpec,
)
from repoforge.domain.github_api_identity import GitHubApiIdentityProof, StoredGhAccountSpec
from repoforge.domain.github_capability_preflight import GitHubOperationCapability
from repoforge.domain.repository_identity import (
    ActorClass,
    CredentialKind,
    CredentialProfile,
    OpaqueCredentialReference,
    RepositoryIdentityBinding,
    RepositoryProvider,
)
from repoforge.domain.repository_identity_resolution import (
    CredentialProfileEligibility,
    CredentialRole,
    RepositoryIdentityObservation,
)
from repoforge.testing import FixedClock

NOW = "2026-07-30T00:00:00+00:00"
_SHA = "a" * 64
_CAPABILITIES = (
    GitHubOperationCapability.CONTENTS_READ.value,
    GitHubOperationCapability.CONTENTS_WRITE.value,
)


def _observation(
    *, repository_id: str = "987654", config_revision: str = _SHA
) -> RepositoryIdentityObservation:
    return RepositoryIdentityObservation(
        provider=RepositoryProvider.GITHUB,
        provider_host="github.com",
        repository_id=repository_id,
        canonical_name="github.com/acme/demo",
        exists=True,
        observed_at=NOW,
        config_revision=config_revision,
    )


def _profile_config(
    profile_id: str = "personal",
    *,
    actor_class: ActorClass = ActorClass.HUMAN_OPERATED,
    enabled: bool = True,
    expected_actor_id: str = "4242",
) -> AuthProfileConfig:
    profile = CredentialProfile(
        profile_id=profile_id,
        provider=RepositoryProvider.GITHUB,
        credential_kind=CredentialKind.STORED_ACCOUNT,
        credential_ref=OpaqueCredentialReference(
            scheme="repoforge", reference_id=f"gh-account-{profile_id}"
        ),
        actor_class=actor_class,
        expected_actor_id=expected_actor_id,
        capability_ids=_CAPABILITIES,
        revision=_SHA,
    )
    return AuthProfileConfig(
        profile=profile,
        eligibility=CredentialProfileEligibility(
            profile=profile,
            enabled=enabled,
            repository_patterns=("github.com/acme/*",),
            boundary_id="acme",
        ),
        api_identity=StoredGhAccountSpec(
            reference_id=f"gh-account-{profile_id}",
            profile_id=profile_id,
            host="github.com",
            login="acme-operator",
            actor_id=expected_actor_id,
            actor_class=(
                ActorClass.HUMAN_OPERATED
                if actor_class is ActorClass.AUTONOMOUS_AGENT
                else actor_class
            ),
            repository_id="987654",
            capability_ids=_CAPABILITIES,
        ),
        transport=GitTransportSpec(
            profile_id=profile_id,
            repository_id="987654",
            target_id="987654",
            provider_host="github.com",
            kind=GitTransportKind.HTTPS,
            credential_fingerprint=_SHA,
            allowed_access=(GitTransportAccess.READ, GitTransportAccess.WRITE),
            https_token_environment="REPOFORGE_GH_TOKEN",
        ),
    )


def _config(tmp_path: Path, *, profiles: dict[str, AuthProfileConfig] | None = None) -> AppConfig:
    repo_root = tmp_path / "demo"
    repo_root.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        source_path=tmp_path / "config.toml",
        server=ServerConfig(tmp_path / "workspaces", tmp_path / "state"),
        repositories={"demo": RepositoryConfig(repo_id="demo", path=repo_root)},
        auth_profiles=profiles if profiles is not None else {"personal": _profile_config()},
    )


class Api:
    def __init__(self, proof: GitHubApiIdentityProof | Exception) -> None:
        self.proof = proof
        self.calls = 0

    def inspect(self, spec: object) -> GitHubApiIdentityProof:
        self.calls += 1
        if isinstance(self.proof, Exception):
            raise self.proof
        return self.proof


class Transport:
    def __init__(self, evidence: GitTransportEvidence | Exception) -> None:
        self.evidence = evidence

    def inspect(self, cwd: Path, spec: GitTransportSpec) -> GitTransportEvidence:
        del cwd, spec
        if isinstance(self.evidence, Exception):
            raise self.evidence
        return self.evidence


class Commits:
    def __init__(self, evidence: CommitIdentityEvidence | Exception) -> None:
        self.evidence = evidence

    def inspect(self, cwd: Path) -> CommitIdentityEvidence:
        del cwd
        if isinstance(self.evidence, Exception):
            raise self.evidence
        return self.evidence


def _proof(actor_id: str = "4242", **overrides: bool) -> GitHubApiIdentityProof:
    return GitHubApiIdentityProof(
        actor_id=actor_id,
        repository_id="987654",
        capability_ids=_CAPABILITIES,
        permission_ids=("contents:write",),
        **overrides,
    )


def _transport_evidence(fingerprint: str = _SHA) -> GitTransportEvidence:
    return GitTransportEvidence(
        profile_id="personal",
        repository_id="987654",
        provider_host="github.com",
        kind=GitTransportKind.HTTPS,
        credential_fingerprint=fingerprint,
        access=GitTransportAccess.WRITE,
        remote_url_digest="b" * 64,
        requested_ref=None,
        observed_sha=None,
    )


def _commit_evidence(
    signing_mode: CommitSigningMode = CommitSigningMode.UNSIGNED_ATTESTED,
    signer_fingerprint: str | None = None,
) -> CommitIdentityEvidence:
    return CommitIdentityEvidence(
        profile_id="personal",
        actor_class=ActorClass.HUMAN_OPERATED,
        author_name="Acme Operator",
        author_email="operator@example.com",
        committer_name="Acme Operator",
        committer_email="operator@example.com",
        signing_mode=signing_mode,
        signer_fingerprint=signer_fingerprint,
        # Signed and unsigned evidence are mutually exclusive by construction: an unsigned
        # commit carries an attestation, a signed one carries a signer fingerprint.
        attestation_digest=_SHA if signing_mode is CommitSigningMode.UNSIGNED_ATTESTED else None,
        config_snapshot_digest=_SHA,
    )


def _service(
    tmp_path: Path,
    *,
    profiles: dict[str, AuthProfileConfig] | None = None,
    observation: RepositoryIdentityObservation | None = None,
    api: object = None,
    transport: object = None,
    commits: object = None,
    publication: object = None,
) -> AuthUxService:
    store = JsonRepositoryBindingStore(tmp_path / "state", FcntlLockManager(tmp_path / "locks"))
    resolved = observation or _observation()
    return AuthUxService(
        config=_config(tmp_path, profiles=profiles),
        bindings=store,
        observe=lambda repo_id, selector: resolved,
        api=api,
        transport=transport,
        commits=commits,
        publication=publication,
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def test_profile_list_filters_by_enabled_state_and_actor_role(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        profiles={
            "personal": _profile_config("personal"),
            "retired": _profile_config("retired", enabled=False),
            "automation": _profile_config("automation", actor_class=ActorClass.AUTONOMOUS_AGENT),
            "delegate": _profile_config("delegate", actor_class=ActorClass.DELEGATED_HUMAN),
        },
    )

    assert [item["profile_id"] for item in service.profile_list()] == [
        "automation",
        "delegate",
        "personal",
        "retired",
    ]
    assert [item["profile_id"] for item in service.profile_list(enabled_only=True)] == [
        "automation",
        "delegate",
        "personal",
    ]
    # `human` accepts a delegated human; `agent` accepts only an autonomous agent.
    assert [item["profile_id"] for item in service.profile_list(role=CredentialRole.HUMAN)] == [
        "delegate",
        "personal",
        "retired",
    ]
    assert [item["profile_id"] for item in service.profile_list(role=CredentialRole.AGENT)] == [
        "automation"
    ]


def test_profile_inspect_returns_safe_metadata_and_no_credential_reference(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    payload = service.profile_inspect("personal")

    assert payload["profile_id"] == "personal"
    assert payload["capability_ids"] == list(_CAPABILITIES)
    rendered = json.dumps(payload, sort_keys=True)
    assert "credential_ref" not in rendered
    assert "gh-account-personal" not in rendered
    assert "REPOFORGE_GH_TOKEN" not in rendered

    with pytest.raises(RepoForgeError) as missing:
        service.profile_inspect("absent")
    assert missing.value.code is ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# Resolution and bindings, against a real store
# ---------------------------------------------------------------------------


def test_repository_observation_receives_the_exact_selector(tmp_path: Path) -> None:
    calls: list[tuple[str, AuthProfileSelector]] = []
    store = JsonRepositoryBindingStore(tmp_path / "state", FcntlLockManager(tmp_path / "locks"))

    def observe(repo_id: str, selector: AuthProfileSelector) -> RepositoryIdentityObservation:
        calls.append((repo_id, selector))
        return _observation()

    service = AuthUxService(
        config=_config(
            tmp_path,
            profiles={
                "automation": _profile_config("automation", actor_class=ActorClass.AUTONOMOUS_AGENT)
            },
        ),
        bindings=store,
        observe=observe,
    )
    selector = AuthProfileSelector("automation", RequestedActorClass.AGENT)

    service.resolve(repo_id="demo", selector=selector)

    assert calls == [("demo", selector)]


def test_whoami_uses_the_profile_bound_for_the_requested_actor_role(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        profiles={
            "personal": _profile_config("personal"),
            "automation": _profile_config("automation", actor_class=ActorClass.AUTONOMOUS_AGENT),
        },
    )
    service._bindings.create(
        RepositoryIdentityBinding(
            provider=RepositoryProvider.GITHUB,
            repository_id="987654",
            canonical_name="github.com/acme/demo",
            human_profile_id="personal",
            agent_profile_id="automation",
            config_revision=_SHA,
        )
    )

    result = service.whoami(
        repo_id="demo",
        selector=AuthProfileSelector("automation", RequestedActorClass.AGENT),
        checks=(AuthSurface.REPOSITORY_BINDING, AuthSurface.API),
    )

    assert result.profile_id == "automation"
    assert result.surfaces[1].profile_id == "automation"


def test_resolve_reports_a_proposal_without_writing_a_binding(tmp_path: Path) -> None:
    service = _service(tmp_path)

    resolved = service.resolve(repo_id="demo", selector=AuthProfileSelector())

    assert resolved["outcome"] == "proposal_required"
    assert resolved["binding"] is None
    # Reporting a proposal must not durably bind anything.
    assert service._bindings.read("github.com", "987654") is None


def test_bind_creates_the_binding_and_repeating_it_is_unchanged(tmp_path: Path) -> None:
    service = _service(tmp_path)

    created = service.bind(repo_id="demo", selector=AuthProfileSelector())

    assert created["status"] == "created"
    stored = service._bindings.read("github.com", "987654")
    assert stored is not None
    assert stored.value.human_profile_id == "personal"
    assert stored.value.agent_profile_id is None

    repeated = service.bind(repo_id="demo", selector=AuthProfileSelector())
    assert repeated["status"] == "unchanged"
    assert service._bindings.read("github.com", "987654").revision == stored.revision


def test_bind_fills_only_the_empty_role_slot_on_an_existing_binding(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        profiles={
            "personal": _profile_config("personal"),
            "automation": _profile_config("automation", actor_class=ActorClass.AUTONOMOUS_AGENT),
        },
    )
    service.bind(repo_id="demo", selector=AuthProfileSelector())

    updated = service.bind(
        repo_id="demo",
        selector=AuthProfileSelector(actor_class=RequestedActorClass.AGENT),
    )

    assert updated["status"] == "updated"
    stored = service._bindings.read("github.com", "987654")
    assert stored.value.human_profile_id == "personal"
    assert stored.value.agent_profile_id == "automation"


def test_bind_refuses_a_stale_revision_without_changing_state(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        profiles={
            "personal": _profile_config("personal"),
            "automation": _profile_config("automation", actor_class=ActorClass.AUTONOMOUS_AGENT),
        },
    )
    service.bind(repo_id="demo", selector=AuthProfileSelector())
    before = service._bindings.read("github.com", "987654")

    with pytest.raises(RepoForgeError) as failure:
        service.bind(
            repo_id="demo",
            selector=AuthProfileSelector(actor_class=RequestedActorClass.AGENT),
            expected_binding_revision=before.revision.value + 99,
        )

    assert failure.value.code is ErrorCode.STATE_STALE
    assert service._bindings.read("github.com", "987654").value == before.value


def test_unbind_clears_one_role_and_refuses_to_clear_the_last_one(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        profiles={
            "personal": _profile_config("personal"),
            "automation": _profile_config("automation", actor_class=ActorClass.AUTONOMOUS_AGENT),
        },
    )
    service.bind(repo_id="demo", selector=AuthProfileSelector())
    service.bind(
        repo_id="demo", selector=AuthProfileSelector(actor_class=RequestedActorClass.AGENT)
    )
    current = service._bindings.read("github.com", "987654")

    cleared = service.unbind(
        repo_id="demo",
        role=CredentialRole.HUMAN,
        expected_binding_revision=current.revision.value,
    )

    assert cleared["status"] == "updated"
    remaining = service._bindings.read("github.com", "987654")
    assert remaining.value.human_profile_id is None
    assert remaining.value.agent_profile_id == "automation"

    with pytest.raises(RepoForgeError) as final:
        service.unbind(
            repo_id="demo",
            role=CredentialRole.AGENT,
            expected_binding_revision=remaining.revision.value,
        )
    assert final.value.code is ErrorCode.INPUT_REQUIRED
    # The refusal left the last role in place rather than orphaning the repository.
    assert service._bindings.read("github.com", "987654").value == remaining.value


# ---------------------------------------------------------------------------
# The seven independent surfaces
# ---------------------------------------------------------------------------


def _bound(service: AuthUxService) -> None:
    service.bind(repo_id="demo", selector=AuthProfileSelector())


def test_whoami_returns_every_surface_in_the_stable_order(tmp_path: Path) -> None:
    service = _service(tmp_path)

    result = service.whoami(repo_id="demo")

    assert [item.surface.value for item in result.surfaces] == [
        "repository_binding",
        "api",
        "transport",
        "commit_author",
        "commit_committer",
        "commit_signer",
        "publication",
    ]
    assert tuple(item.surface for item in result.surfaces) == AUTH_SURFACE_ORDER


def test_whoami_requests_a_subset_but_keeps_the_stable_order(tmp_path: Path) -> None:
    service = _service(tmp_path)

    result = service.whoami(repo_id="demo", checks=(AuthSurface.PUBLICATION, AuthSurface.API))

    assert [item.surface.value for item in result.surfaces] == ["api", "publication"]


def test_a_missing_inspector_never_becomes_an_ambient_fallback(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _bound(service)

    result = service.whoami(repo_id="demo")
    states = {item.surface: item.state for item in result.surfaces}

    assert states[AuthSurface.REPOSITORY_BINDING] is AuthSurfaceState.VERIFIED
    # Declared but not proved on this call.
    for surface in (AuthSurface.API, AuthSurface.TRANSPORT):
        assert states[surface] is AuthSurfaceState.CONFIGURED, surface
    # Nothing reviewed and nothing composed.
    for surface in (
        AuthSurface.COMMIT_AUTHOR,
        AuthSurface.COMMIT_SIGNER,
        AuthSurface.PUBLICATION,
    ):
        assert states[surface] is AuthSurfaceState.UNAVAILABLE, surface
    # Whatever the label, nothing unobserved may be treated as usable.
    assert not any(
        item.satisfied
        for item in result.surfaces
        if item.surface is not AuthSurface.REPOSITORY_BINDING
    )
    assert result.ready is False


def test_transport_success_never_upgrades_api_actor_evidence(tmp_path: Path) -> None:
    # The transport reaches the host, but no API inspector is composed.
    service = _service(tmp_path, transport=Transport(_transport_evidence()))
    _bound(service)

    surfaces = {item.surface: item for item in service.whoami(repo_id="demo").surfaces}

    assert surfaces[AuthSurface.TRANSPORT].state is AuthSurfaceState.VERIFIED
    assert surfaces[AuthSurface.TRANSPORT].actor_id is None
    # The API surface stays unproved and names no observed actor, whatever the transport did.
    assert surfaces[AuthSurface.API].state is AuthSurfaceState.CONFIGURED
    assert surfaces[AuthSurface.API].satisfied is False
    assert surfaces[AuthSurface.API].actor_id is None


def test_an_unsigned_attestation_never_claims_a_signer(tmp_path: Path) -> None:
    service = _service(tmp_path, commits=Commits(_commit_evidence()))
    _bound(service)

    surfaces = {item.surface: item for item in service.whoami(repo_id="demo").surfaces}

    assert surfaces[AuthSurface.COMMIT_AUTHOR].state is AuthSurfaceState.VERIFIED
    assert "operator@example.com" in surfaces[AuthSurface.COMMIT_AUTHOR].detail
    signer = surfaces[AuthSurface.COMMIT_SIGNER]
    assert signer.state is AuthSurfaceState.UNOBSERVABLE
    assert "no signer identity is claimed" in signer.detail


def test_a_signed_repository_reports_the_observed_signer(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        commits=Commits(
            _commit_evidence(
                CommitSigningMode.SSH, signer_fingerprint="SHA256:AbCdEfGhIjKlMnOpQrSt"
            )
        ),
    )
    _bound(service)

    surfaces = {item.surface: item for item in service.whoami(repo_id="demo").surfaces}

    assert surfaces[AuthSurface.COMMIT_SIGNER].state is AuthSurfaceState.VERIFIED
    assert "SHA256:AbCdEfGhIjKlMnOpQrSt" in surfaces[AuthSurface.COMMIT_SIGNER].detail


def test_an_actor_or_credential_mismatch_is_blocked_not_verified(tmp_path: Path) -> None:
    wrong_actor = _service(tmp_path, api=Api(_proof(actor_id="9999")))
    _bound(wrong_actor)
    revoked = _service(tmp_path / "b", api=Api(_proof(revoked=True)))
    _bound(revoked)
    wrong_credential = _service(
        tmp_path / "c", transport=Transport(_transport_evidence(fingerprint="c" * 64))
    )
    _bound(wrong_credential)

    def state(service: AuthUxService, surface: AuthSurface) -> AuthSurfaceState:
        return {item.surface: item.state for item in service.whoami(repo_id="demo").surfaces}[
            surface
        ]

    assert state(wrong_actor, AuthSurface.API) is AuthSurfaceState.BLOCKED
    assert state(revoked, AuthSurface.API) is AuthSurfaceState.BLOCKED
    assert state(wrong_credential, AuthSurface.TRANSPORT) is AuthSurfaceState.BLOCKED


def test_readiness_depends_on_every_requested_required_surface(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        api=Api(_proof()),
        transport=Transport(_transport_evidence()),
        commits=Commits(_commit_evidence()),
    )
    _bound(service)

    # The composed surfaces alone are all satisfiable.
    narrowed = service.whoami(
        repo_id="demo",
        checks=(
            AuthSurface.REPOSITORY_BINDING,
            AuthSurface.API,
            AuthSurface.TRANSPORT,
            AuthSurface.COMMIT_SIGNER,
        ),
    )
    assert narrowed.ready is True

    # Adding a surface with no inspector makes the whole report not ready.
    assert service.whoami(repo_id="demo").ready is False


def test_a_binding_from_another_configuration_revision_blocks_the_binding_surface(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _bound(service)
    drifted = AuthUxService(
        config=service._config,
        bindings=service._bindings,
        observe=lambda repo_id, selector: _observation(config_revision="d" * 64),
    )

    surfaces = {item.surface: item for item in drifted.whoami(repo_id="demo").surfaces}

    assert surfaces[AuthSurface.REPOSITORY_BINDING].state is AuthSurfaceState.BLOCKED


def test_whoami_payload_is_safe_metadata_only(tmp_path: Path) -> None:
    service = _service(tmp_path, api=Api(_proof()), transport=Transport(_transport_evidence()))
    _bound(service)

    rendered = json.dumps(service.whoami(repo_id="demo").safe_payload(), sort_keys=True)

    assert "gho_" not in rendered and "ghp_" not in rendered
    assert "REPOFORGE_GH_TOKEN" not in rendered
    assert "gh-account-personal" not in rendered


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def test_doctor_reports_migration_required_when_no_profile_is_declared(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, profiles={})

    codes = [finding.code for finding in service.doctor(repo_id="demo")]

    assert "migration_required" in codes


def test_doctor_reports_a_disabled_profile_and_the_resulting_resolution_failure(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, profiles={"personal": _profile_config(enabled=False)})

    findings = {finding.code: finding for finding in service.doctor(repo_id="demo")}

    assert "profile_disabled" in findings
    assert "profile_not_authorized" in findings
    assert findings["profile_not_authorized"].recovery_actions
    assert all(finding.safe_payload()["detail"] for finding in findings.values())


def test_doctor_reports_an_ambiguous_selection_with_typed_recovery(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        profiles={
            "personal": _profile_config("personal"),
            "second": _profile_config("second"),
        },
    )

    findings = {finding.code: finding for finding in service.doctor(repo_id="demo")}

    assert "profile_ambiguous" in findings
    assert [action.kind.value for action in findings["profile_ambiguous"].recovery_actions] == [
        "reselect_profile"
    ]


def test_doctor_reports_a_blocked_surface_it_actually_observed(tmp_path: Path) -> None:
    service = _service(tmp_path, api=Api(_proof(actor_id="9999")))
    _bound(service)

    findings = {finding.code: finding for finding in service.doctor(repo_id="demo")}

    assert "api_blocked" in findings
    assert findings["api_blocked"].severity.value == "blocking"


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------


def test_lease_inspection_is_unavailable_without_durable_identity_state(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    with pytest.raises(RepoForgeError) as failure:
        service.lease_inspect(operation_id="op-1")

    assert failure.value.code is ErrorCode.OPERATION_IDENTITY_NOT_FOUND
    assert failure.value.safe_next_action


def test_binding_state_survives_a_fresh_service_over_the_same_store(tmp_path: Path) -> None:
    # The production mutation is the durable record, so a new service must see it.
    first = _service(tmp_path)
    first.bind(repo_id="demo", selector=AuthProfileSelector())

    second = _service(tmp_path)
    resolved = second.resolve(repo_id="demo", selector=AuthProfileSelector())

    assert resolved["outcome"] == "resolved"
    assert resolved["profile_id"] == "personal"


def test_an_explicit_profile_cannot_override_an_exact_binding(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        profiles={
            "personal": _profile_config("personal"),
            "second": _profile_config("second"),
        },
    )
    store = service._bindings
    store.create(
        RepositoryIdentityBinding(
            provider=RepositoryProvider.GITHUB,
            repository_id="987654",
            canonical_name="github.com/acme/demo",
            human_profile_id="personal",
            agent_profile_id=None,
            config_revision=_SHA,
        )
    )

    resolved = service.resolve(repo_id="demo", selector=AuthProfileSelector(auth_profile="second"))

    assert resolved["outcome"] == "failed"
    assert resolved["failure"]["code"] == "PROFILE_NOT_AUTHORIZED"


def test_a_declared_but_unobserved_surface_is_configured_not_unavailable(
    tmp_path: Path,
) -> None:
    """`configured` and `unavailable` are different answers and must not collapse.

    A profile that declares an API identity and a pinned transport, inspected by a context that
    composes no inspector for them, is *declared but not observed*. Reporting `unavailable`
    there would lose the distinction from a repository that declares nothing at all -- and the
    operator's next step differs: compose the inspector, versus declare a profile.
    """

    bound = _service(tmp_path)
    bound.bind(repo_id="demo", selector=AuthProfileSelector())
    undeclared = _service(tmp_path / "none", profiles={})

    bound_states = {item.surface: item for item in bound.whoami(repo_id="demo").surfaces}
    undeclared_states = {item.surface: item for item in undeclared.whoami(repo_id="demo").surfaces}

    for surface in (AuthSurface.API, AuthSurface.TRANSPORT):
        assert bound_states[surface].state is AuthSurfaceState.CONFIGURED, surface
        assert bound_states[surface].profile_id == "personal", surface
        # Still not an observation, so it can never satisfy a required surface.
        assert bound_states[surface].satisfied is False, surface
        assert undeclared_states[surface].state is AuthSurfaceState.UNAVAILABLE, surface

    # Readiness is unchanged by the relabelling.
    assert bound.whoami(repo_id="demo").ready is False


def test_doctor_observes_the_repository_once(tmp_path: Path) -> None:
    """Each observation is two `gh` round trips against the provider.

    `doctor` reports both the resolution outcome and every surface, and both need the same
    observation. Observing twice doubles the provider calls for one command and lets the two
    halves of a single report disagree if the repository changes between them.
    """

    calls: list[str] = []
    store = JsonRepositoryBindingStore(tmp_path / "state", FcntlLockManager(tmp_path / "locks"))

    def observe(repo_id: str, selector: AuthProfileSelector) -> RepositoryIdentityObservation:
        del selector
        calls.append(repo_id)
        return _observation()

    service = AuthUxService(
        config=_config(tmp_path),
        bindings=store,
        observe=observe,
        clock=FixedClock(NOW),
    )

    service.doctor(repo_id="demo")

    assert calls == ["demo"]
