# Repository-Bound Identity Contracts

RepoForge treats repository authentication as a separate domain from workspace ownership, capability authorization, and provider-neutral workload identity. A successful check on one identity surface never proves another surface.

## Safe source of truth

Resolution may start from repository names or URLs, but a durable binding is pinned to the provider repository ID. `CredentialProfile` contains an `OpaqueCredentialReference`; it never contains a token, key, authorization header, helper response, or signing secret. Provider adapters resolve the reference only inside a reviewed operation boundary.

An `OperationIdentityContext` contains one or more target-bound `AuthLease` values. Nested repositories, submodules, LFS endpoints, packages, and releases may therefore use separate least-privilege profiles without inheriting the primary repository credential.

`PublicationIntent` binds the source repository ID, destination repository ID, exact source and destination refs, expected commit SHA, and any approved cross-boundary exception before an external write. `IdentityReceipt` records only safe actor/profile/lease IDs, repository and ref identities, digests, revisions, and independently collected surface evidence.

## Binding registry and resolver

`JsonRepositoryBindingStore` persists one CAS-protected binding under the stable key `(provider_host, repository_id)`. The encoded record contains profile IDs, canonical repository metadata, and configuration revision only; credential references and secret bodies are not persisted in the binding registry.

`resolve_repository_identity` is pure and process-global-state free. An exact stable-ID binding wins. A rename within the same provider owner boundary succeeds with a typed reconciliation event, while an owner transfer, delete/recreate identity change, duplicate stable-ID claim, or stale binding/config revision fails closed. An unbound repository produces a reviewable proposal only when exactly one enabled profile is eligible for the requested human or agent role.

Discovery patterns may name one repository or use `host/owner/*`; they may not cross an owner boundary. Resolution evidence exposes the provider host, stable repository ID, canonical name, selected profile ID, binding revision, and outcome without exposing credential references.

## Ephemeral broker and process isolation

`RepositoryAuthBroker` resolves only the opaque reference selected by the repository binding. Material is validated against the exact profile, actor class, target and capability ceiling, then exposed for one bounded session. Missing, revoked or expired material fails closed; refresh is accepted only when profile, actor, target and capability identity remain equivalent.

`ProcessAuthContext` starts from a scrubbed environment. Ambient GitHub tokens, SSH-agent sockets, askpass/helper chains, Git configuration injection and author/committer variables are removed before the selected material is injected. `SubprocessAuthRunner` launches with that exact environment rather than merging process-global state. Raw values are blocked from argv, URLs, stdin and cwd, and are redacted before stdout/stderr, exceptions or failure artifacts cross the process boundary. Session exit zeroises the in-memory buffers and releases provider material.

## GitHub API identities

Stored GitHub accounts are selected by an explicit reviewed login using `gh auth token --hostname ... --user ...`; RepoForge never changes the machine-global active account. The returned token is verified independently against the expected API actor and stable repository ID before the broker can admit it.

Autonomous GitHub writes prefer repository-selected App installation tokens. The App JWT is supplied by an external signer, installation-token issuance requests the exact repository ID and minimal permission map, and refresh is accepted only when installation, actor, repository, capability ceiling and provider metadata remain identical. Release revokes the installation token where supported and zeroises both JWT and token buffers.

`github_api_auth_lease` copies only the token digest, actor ID, installation/repository metadata, revisions and opaque reference into `AuthLease`; raw API material never enters leases or receipts.

## Git transport identities

`GitTransportRouter` validates the operation-scoped profile, stable repository target, provider host and read/write ceiling before starting Git. SSH transport supplies one absolute identity-file reference through an exact `GIT_SSH_COMMAND` with `IdentitiesOnly=yes`, `IdentityAgent=none`, `BatchMode=yes` and no user SSH config. Ambient agents and alternate keys cannot participate.

HTTPS transport clears the complete helper chain, enables `credential.useHttpPath`, disables terminal and credential-manager interaction, and installs one operation-scoped helper that reads the selected ephemeral token from a reviewed environment key. Tokens are never embedded in remote URLs, argv, helper text, output or receipts. A failed transport makes one attempt and never retries through an ambient personal helper.

`GitTransportEvidence` records the stable repository ID, profile ID, provider host, access level, transport kind, credential fingerprint, remote-URL digest and observed ref SHA. Successful access remains `unobservable` for the human/API actor; it never upgrades transport proof into actor proof.

## Durable operation identity lifecycle

`JsonOperationIdentityStore` persists one private CAS sidecar per durable operation. The record contains the immutable `OperationIdentityContext`, one capability request for every target-bound lease, the context ID/digest used by handoff references, superseded lease IDs, and lifecycle timestamps. It contains only opaque references, stable IDs, revisions, digests and safe provider metadata.

Binding is single-assignment: retrying the same decision is idempotent, while a different profile, target, capability request or context digest fails with `OPERATION_IDENTITY_MISMATCH`. `OperationIdentityReference` is propagated into TaskCapsule v3 resume projections and operation-worker bindings; records written before those additive fields still decode with no identity reference.

Every external write revalidates the exact operation ID, context digest, target kind/ID, requested capability, lease state and expiry. Nested repositories never inherit the primary lease. Revocation may select one lease or profile, expiry changes only elapsed active leases, and refresh is accepted only when profile, provider, repository, target, actor, opaque reference, config/policy revisions and safe provider metadata remain identical. A missing or unavailable sidecar denies writes rather than falling back to ambient identity.

## Evidence semantics

- `verified_actor` proves the observed API or provider actor only for the named surface.
- `verified_repository` proves the canonical repository target only for the named surface.
- `transport_access_proof` proves that a transport credential accessed a target; it does not prove a human login or API actor.
- `configured_metadata` records reviewed configuration but is not live proof.
- `unobservable` states explicitly that the surface could not be proven.

Actor, Git transport, author, committer, signer, PR creator, release creator, and publication destination must be compared independently. Missing required evidence fails closed.

## Compatibility mapping

A single-account installation imports or selects one local profile, creates one binding for each enrolled repository ID, and resolves `auto` only when exactly one binding matches. This preserves the existing one-account workflow without relying on `gh auth switch`, global Git configuration mutation, inherited `GH_TOKEN`, or opportunistic SSH-agent selection.

## Failure and recovery contract

`RepositoryAuthFailureCode` separates resolution ambiguity, actor and transport mismatch, lease lifecycle, remote rewrite, publication mismatch, nested-resource denial, author/committer/signer mismatch, enterprise authorization, and network policy failures. Recovery is represented by typed `RecoveryAction` values such as reselecting a profile, reauthorizing, refreshing a lease, reconciling a binding, reviewing a remote, or aborting. Recovery parameters contain safe identifiers only.

## Maintained call-site inventory

`REPOSITORY_IDENTITY_SURFACES.json` classifies every production adapter that invokes Git, GitHub CLI, SSH/LFS executables, or direct subprocess discovery. Its architecture test fails whenever a new identity-sensitive adapter appears without a reviewed classification and owning EPIC ticket.
