# Repository-Bound Identity Contracts

RepoForge treats repository authentication as a separate domain from workspace ownership, capability authorization, and provider-neutral workload identity. A successful check on one identity surface never proves another surface.

## Safe source of truth

Resolution may start from repository names or URLs, but a durable binding is pinned to the provider repository ID. `CredentialProfile` contains an `OpaqueCredentialReference`; it never contains a token, key, authorization header, helper response, or signing secret. Provider adapters resolve the reference only inside a reviewed operation boundary.

An `OperationIdentityContext` contains one or more target-bound `AuthLease` values. Nested repositories, submodules, LFS endpoints, packages, and releases may therefore use separate least-privilege profiles without inheriting the primary repository credential.

`PublicationIntent` binds the source repository ID, destination repository ID, exact source and destination refs, expected commit SHA, and any approved cross-boundary exception before an external write. `IdentityReceipt` records only safe actor/profile/lease IDs, repository and ref identities, digests, revisions, and independently collected surface evidence.

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
