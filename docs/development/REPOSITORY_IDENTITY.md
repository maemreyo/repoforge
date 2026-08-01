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

## Default production composition

`build_application` constructs the complete repository-bound identity stack by default: the durable binding store and observer, `GitHubApiAuthProvider`, `RepositoryAuthBroker`, `RepositoryIdentityRuntime`, `GitTransportRouter`, durable operation-identity store, and the publication adapter/coordinator/service. Overrides remain available for tests and alternate deployments, but the ordinary service path no longer requires an injected publication service or an ambient GitHub gateway. Composition itself is side-effect free: no credential reference is resolved and no provider request is made until an admitted operation opens a broker session.

Every identity-bearing tool selector reaches this runtime. Explicit selection and `auto` both resolve against the same stable repository binding, requested actor role, enabled-profile eligibility and capability ceiling. A selector is never accepted and discarded. An unbound, stale, ambiguous or role-incompatible selection fails before material acquisition; a reviewable binding proposal is never treated as write authority.

For push and draft-PR publication, `ScopedWorkspacePublicationRequestFactory` resolves the selector, opens one broker session, creates the safe lease and operation identity, and invokes the coordinator while that session is still live. The request and raw process context do not escape the callback. Session exit releases provider material and zeroises secret buffers even when validation or the external effect raises. Durable operation, publication, identity-context and lease IDs are derived deterministically from the exact idempotency key and intent, so an exact retry reproduces the same identity decision instead of creating a competing one.

## Ephemeral broker and process isolation

`RepositoryAuthBroker` resolves only the opaque reference selected by the repository binding. Material is validated against the exact profile, actor class, target and capability ceiling, then exposed for one bounded session. Missing, revoked or expired material fails closed; refresh is accepted only when profile, actor, target and capability identity remain equivalent.

`ProcessAuthContext` starts from a scrubbed environment. Ambient GitHub tokens, SSH-agent sockets, askpass/helper chains, Git configuration injection and author/committer variables are removed before the selected material is injected. `SubprocessAuthRunner` launches with that exact environment rather than merging process-global state. Raw values are blocked from argv, URLs, stdin and cwd, and are redacted before stdout/stderr, exceptions or failure artifacts cross the process boundary. Session exit zeroises the in-memory buffers and releases provider material.

## GitHub API identities

Stored GitHub accounts are selected by an explicit reviewed login using `gh auth token --hostname ... --user ...`; RepoForge never changes the machine-global active account. The returned token is verified independently against the expected API actor and stable repository ID before the broker can admit it.

Autonomous GitHub writes prefer repository-selected App installation tokens. The App JWT is supplied by an external signer, installation-token issuance requests the exact repository ID and minimal permission map, and refresh is accepted only when installation, actor, repository, capability ceiling and identity-bearing provider metadata remain identical. Renewable observation metadata such as `github_preflight_observed_at` may advance; repository, actor, installation, capability/permission evidence, configuration and policy identity may not drift. Release revokes the installation token where supported and zeroises both JWT and token buffers.

`github_api_auth_lease` copies only the token digest, actor ID, installation/repository metadata, revisions and opaque reference into `AuthLease`; raw API material never enters leases or receipts.

## GitHub capability evidence

RepoForge keeps two deliberately separate GitHub capability surfaces. The ambient
doctor/discovery probe may describe the local `gh` installation or currently
discoverable account, but is informational only: it never authorizes a write.
`CommandGitHubCapabilityPreflight` is operation-scoped authorization evidence.
It runs bounded, read-only GitHub API observations through the exact isolated
`ProcessAuthContext` selected for that operation; it does not switch the global
`gh` account, mutate provider state, or fall back to ambient credentials.

The exact catalogue is:

- `github.contents.read`, `github.contents.write`;
- `github.issues.read`, `github.issues.write`;
- `github.pull_requests.read`, `github.pull_requests.write`;
- `github.actions.read`, `github.workflows.write`;
- `github.releases.read`, `github.releases.write`;
- `github.organization_projects.read`, `github.organization_projects.write`;
- `github.packages.read`, `github.packages.write`.

Every requested capability receives one typed evidence result. Only
`proven_available` authorizes the affected external write. Missing permission,
provider unavailability, stale binding, repository mismatch, and unobservable
enterprise evidence all deny that write. Coarse repository write access,
organisation role, and a successful unrelated probe never imply a capability.

`AuthLease` and publication metadata retain only safe binding evidence:
actor ID, installation ID, repository ID, profile-ceiling digests,
credential-material identity, policy/configuration revisions, timestamps, and
evidence digest. The broker proves that the operation capability is a subset of
the reviewed profile ceiling; publication then pins operation-specific
capability and permission digests. Write-time revalidation recomputes those
operation digests and denies a changed actor, installation, repository,
capability/permission proof, policy/configuration revision, or
credential-material identity. A renewable observation timestamp may advance
only when all identity-bearing evidence remains equivalent.

Recovery is represented by typed actions such as reauthorizing the selected
profile, refreshing an equivalent lease, or asking an operator to resolve a
specific enterprise policy. No recovery asks for broader credentials, retries
with a stronger profile, or expands permissions automatically. Production
composition continues to retain the doctor probe for discovery while binding a
separate preflight gateway for operation authorization.

## Git transport identities

`GitTransportRouter` validates the operation-scoped profile, stable repository target, provider host and read/write ceiling before starting Git. SSH write authority is a structured `ReviewedSshEndpoint`, not a key pathname: it binds the raw remote host, canonical provider host, owner/repository, constrained exact `Host` block digests, public-key fingerprint and verified GitHub SSH principal. Immediately before every network effect, RepoForge re-parses the raw remote, re-reads the exact alias without `ssh -G`, reopens the key with no-follow ownership/mode checks, re-fingerprints it and re-probes the principal. It then copies the verified descriptor into operation-scoped `0600` material, rewrites the command target to the canonical `ssh://git@host:port/owner/repository.git` URL and removes the temporary material in `finally`. `GIT_SSH_COMMAND` still enforces `-F /dev/null`, `IdentitiesOnly=yes`, `IdentityAgent=none`, `BatchMode=yes` and disabled password/keyboard-interactive authentication. Ambient agents, alternate keys, wildcard `Host`, `Include`, `Match`, `ProxyCommand`, `ProxyJump`, `IdentityAgent` and hostname canonicalization cannot participate.

Legacy SSH profiles containing only `ssh_identity_file` remain readable for compatibility but are not write authority. They fail before network with `GIT_TRANSPORT_MIGRATION_REQUIRED`. Recovery is explicit: run `rf auth migrate inspect <repo-id>`, review the alias/key/principal proof, then apply that exact plan. An unsafe or unprovable SSH endpoint is blocking; migration never silently converts the profile or repository remote to HTTPS.

HTTPS transport clears the complete helper chain, enables `credential.useHttpPath`, disables terminal and credential-manager interaction, and installs one operation-scoped helper that reads the selected ephemeral token from a reviewed environment key. Tokens are never embedded in remote URLs, argv, helper text, output or receipts. A failed transport makes one attempt and never retries through an ambient personal helper.

`GitTransportEvidence` records the stable repository ID, profile ID, provider host, access level, transport kind, credential fingerprint, remote-URL digest and observed ref SHA. Successful access remains `unobservable` for the human/API actor; it never upgrades transport proof into actor proof.

## Nested resource identities

`GitNestedResourceDiscovery` inspects only repository-local `.gitmodules` and `.lfsconfig` through bounded `git config --file` reads. The reviewed request fixes the primary endpoint, recursion depth, resource count, output size and command timeout. Discovery never fetches, writes, invokes credential helpers or consults ambient credential state; canonical endpoints and digests are returned in deterministic order.

Every discovered or explicit submodule, LFS, package or release candidate must pass an explicit `NestedTargetResolver`. A public read may be represented as anonymous only when policy allows it and the resolved target proves that exact public/read-only route. Private reads and every write require an independently selected profile plus an exact target-bound child `AuthLease`; the primary repository lease is never inherited. Production constructs the Git discovery adapter by default, but intentionally supplies no ambient resolver or lease-provider fallback. Missing resolver composition denies any discovered route, while missing lease-provider composition denies credentialed routes before an effect.

`NestedIdentityCoordinator` binds the complete primary-and-child lease set and capability requests into the immutable operation identity sidecar. Retries may reproduce that same decision, but a changed endpoint, target, profile, lease, capability, configuration revision or policy revision requires a new operation. Receipts contain only safe target IDs, endpoint digests, routing status, profile/lease IDs, capabilities and source locations.

LFS/package uploads and release publication require their own reviewed nested publication intent, exact endpoint and target, active lease, capability and permission evidence, lifecycle state, payload/config/policy digests and boundary approval. Write-time revalidation must match the reviewed contract exactly. Unresolved targets, missing capabilities, cross-boundary ambiguity, expiry, revocation or TOCTOU drift fail closed with safe target evidence and typed recovery; recovery never broadens credentials or silently redirects the write.

## Exact publication effects

`PublicationIntent` is the complete authority for one external publication. It pins stable source and destination repository IDs, exact source and destination refs, the reviewed commit and tree object IDs, the remote name, publication kind and any explicit cross-boundary approval. An idempotency key can replay only this exact intent; it cannot authorize a changed repository, ref, object or approval boundary.

Before an effect, `PublicationAdapter` inspects fetch URLs, push URLs and ordered `insteadOf`/`pushInsteadOf` rewrites, resolves every effective target through the durable stable-ID binding registry and rejects ambiguous, unbound or multiple push repositories. Write-time revalidation repeats topology and authorization checks, then performs an isolated `ls-remote` for the exact destination/head ref through the selected transport context. The resulting ref SHA and transport evidence replace configured metadata as live proof immediately before the effect. Lease state, operation capability/permission digests, remote version and boundary approval must still match. Git publication emits one exact `source_ref:destination_ref` refspec only; broad, forced, mirrored, deletion and all-ref publication forms are denied.

Pull-request creation and reconciliation use explicit canonical base and head repositories, stable repository IDs, exact base/head refs and the expected head commit. `GhCliGateway` runs these publication calls through the operation-scoped isolated process environment rather than a cwd-derived repository slug or globally active `gh` account. Creation writes a bounded publication marker; reconciliation accepts a result only when the marker, both repositories, both refs and the exact commit all match. Missing proof remains unknown/manual and is never converted into a blind retry.

Workspace push and draft-PR call sites require `WorkspacePublicationService`; an unresolved service fails closed before an external effect. The coordinator creates the durable operation first, binds identity, acquires the write lease, revalidates immediately before the effect boundary and records the operation, receipt and result reference. Workspace metadata retains only safe publication IDs, reconciliation state and timestamps; PR content, credentials and process environments are not persisted.

## Durable operation identity lifecycle

`JsonOperationIdentityStore` persists one private CAS sidecar per durable operation. The record contains the immutable `OperationIdentityContext`, one capability request for every target-bound lease, the context ID/digest used by handoff references, superseded lease IDs, and lifecycle timestamps. It contains only opaque references, stable IDs, revisions, digests and safe provider metadata.

Binding is single-assignment: retrying the same decision is idempotent, while a different profile, target, capability request or context digest fails with `OPERATION_IDENTITY_MISMATCH`. `OperationIdentityReference` is propagated into TaskCapsule v3 resume projections and operation-worker bindings; records written before those additive fields still decode with no identity reference. After restart, workers re-open the referenced sidecar and must observe the same context digest and capability requests. Missing, unreadable, stale-CAS, expired or revoked identity state fails closed; no worker handoff or resume path reconstructs authority from ambient process state.

Every external write revalidates the exact operation ID, context digest, target kind/ID, requested capability, lease state and expiry. Nested repositories never inherit the primary lease. Revocation may select one lease or profile, expiry changes only elapsed active leases, and refresh is accepted only when profile, provider, repository, target, actor, opaque reference, config/policy revisions and safe provider metadata remain identical. A missing or unavailable sidecar denies writes rather than falling back to ambient identity.

## Worktree-safe commit identity

Workspace creation resolves one explicit `CommitIdentityPolicy` and stores it with a digest-only snapshot of identity-, signing-, credential-, transport-, and remote-sensitive local/worktree Git configuration. A legacy installation may import the current author and committer once during creation, but later commits never re-read ambient identity as a fallback. Missing or malformed pinned policy fails before staging.

`GitCommitIdentityGateway` runs ordinary commits, reviewed base-refresh merge commits, and evidence collection with an exact process environment. Author and committer variables are injected per operation, `user.useConfigOnly` is forced, interactive credential lookup and SSH-agent inheritance are removed, and direct signing uses only the reviewed key reference and expected fingerprint. It never writes shared or worktree Git configuration and never rewrites a remote. The generic Git repository port exposes no ambient commit or merge-commit operation, so future workflows cannot bypass this governance boundary accidentally.

Before `git add`, the gateway recomputes the configuration snapshot. Shared/local or worktree-specific drift fails closed. Multi-valued helper chains are represented by an ordered aggregate digest, so helper order and additions are detectable without persisting helper bodies. Two linked worktrees can therefore commit concurrently with different policies without changing one another's attribution, signing, or transport behaviour.

Commit evidence records author, committer, actor class, signing mode, signer fingerprint or attestation digest, and the configuration snapshot digest. Delegated-human evidence additionally retains the safe represented-actor and approval IDs. Raw key material and key references are excluded from audit payloads.

## Evidence semantics

- `verified_actor` proves the observed API or provider actor only for the named surface.
- `verified_repository` proves the canonical repository target only for the named surface.
- `transport_access_proof` proves that a transport credential accessed a target; it does not prove a human login or API actor.
- `configured_metadata` records reviewed configuration but is not live proof.
- `unobservable` states explicitly that the surface could not be proven.

Actor, Git transport, author, committer, signer, PR creator, release creator, and publication destination must be compared independently. Missing required evidence fails closed.

## Reviewed configuration

Auth profiles are declared in one root `[auth_profiles.<id>]` table, separate from the per-repository verification `profiles` table. Each declaration is secret-free: it names a `credential_reference`, a `credential_fingerprint`, and for HTTPS an environment *name*, never a value. `AppConfig` turns each declaration into the existing identity primitives — `CredentialProfile`, `CredentialProfileEligibility`, one of `StoredGhAccountSpec` or `GitHubAppInstallationSpec`, and `GitTransportSpec` — so there is no parallel representation to keep in sync. A configuration with no auth profiles loads unchanged and reports `identity_migration_required`.

```toml
[auth_profiles.personal]
provider = "github"
credential_kind = "stored_account"
credential_reference = "gh-account-personal"
actor_class = "human_operated"
expected_actor_id = "github-user-123"
enabled = true
repository_id = "987654"
repository_patterns = ["github.com/example-owner/*"]
boundary_id = "example-owner"
capability_ids = ["github.contents.read", "github.contents.write"]
github_host = "github.com"
github_login = "example-user"
transport_kind = "https"
https_token_environment = "REPOFORGE_GH_PERSONAL_TOKEN"
credential_fingerprint = "…64 lowercase hex…"
allowed_access = ["read", "write"]
lease_seconds = 300
```

Adding, enabling, or widening a profile is a **capability expansion**, so `classify_capability_delta` gates it behind operator approval. Removing, disabling, or narrowing one is a restriction. Swapping the actor, credential reference, or pinned transport behind an existing profile id is incompatible: the id would then name a different identity. Recorded import provenance (`source_ssh_alias`) is metadata only. A root section the classifier does not recognize lands in `unclassified` and is reported as incompatible, which is why every field above has a declared direction.

## Selecting an identity

The public selector is `auth_profile` (a declared id, or `auto`) plus `actor_class` (`human` or `agent`). `auto` succeeds only for exactly one deterministically eligible profile; ambiguity, a missing candidate, and a disabled candidate all fail closed. `human` accepts `HUMAN_OPERATED` and `DELEGATED_HUMAN`; `agent` accepts only `AUTONOMOUS_AGENT`. An explicit profile passes the same binding, role, capability, transport, author, signer, and publication checks as automatic selection, and can never override an exact binding.

The selector appears on the MCP inputs of the six tools whose calls can act as an identity: `workspace_create`, `workspace_commit`, `workspace_push`, `workspace_refresh`, `workspace_pr`, and `repo_issue`. Defaults are `auto` / `human`, so callers written before selectors existed keep working. Branches that perform no write reject an explicit selector rather than accepting and ignoring it — `workspace_pr watch` and the read-only `repo_issue` modes. Every `workspace_refresh` action keeps the selector, including `preview`, because it fetches the base through the pinned transport. The public tool roster stays at 28.

The contract pattern admits `_`, so a token-shaped profile id reaches the application layer; the domain `AuthProfileSelector` is the fail-closed layer for that, and it is validated before any material is acquired or any effect is admitted.

## Operator commands

```bash
rf auth profile list --enabled-only
rf auth profile inspect personal
rf auth resolve demo                      # what would be selected; writes nothing
rf auth bind demo                         # persist the proposed binding
rf auth bind demo --actor-class agent     # fill the other role slot
rf auth unbind demo --actor-class human --expected-revision 3
rf auth whoami demo --check all
rf auth doctor demo
rf auth lease inspect op-1234
rf auth lease revoke op-1234 --expected-revision 2 --profile-id personal
rf auth import gh --login example-user
rf auth import ssh github-work
rf auth migrate inspect demo
rf auth migrate inspect demo --login example-user   # pick one of several stored accounts
rf auth migrate apply demo --plan-id … --plan-hash …
rf auth migrate apply demo --login example-user --plan-id … --plan-hash …
```

Reads need no flags when one profile is eligible. Writes must name the exact state they were reviewed against: a binding revision, a lease revision, or a plan hash. `whoami` and `doctor` exit 3 when a required surface is unsatisfied or a finding blocks. Clearing the final role on a binding is refused — a binding with no profile is not representable, and dropping the binding entirely is a different decision than narrowing its actor classes.

`rf auth migrate inspect --login <login>` narrows discovery to exactly that stored `gh` account, so a machine with several accounts can adopt one without logging the others out. The plan is bound to the selected login through its proposed profile and transport changes, so `apply` must re-prove the same login; a different or missing login yields a stale plan.

`rf auth` runs outside the managed runtime, so it composes no per-surface inspectors and no durable operation identity store. Those surfaces report `unavailable`; they never answer from whatever account happens to be active.

## Independent identity surfaces

`rf auth whoami` reports these surfaces in this stable order, so two runs diff line by line:

```text
repository_binding
api
transport
commit_author
commit_committer
commit_signer
publication
```

Each is `verified`, `configured`, `unobservable`, `blocked`, or `unavailable`. A reachable transport reports `verified` with **no actor**, so it can never be read as proof of an API actor. An unsigned attestation reports `unobservable` rather than naming a signer the repository does not have. A surface with no composed inspector reports `unavailable`. Overall readiness depends on every requested required surface, not on any single one.

## Importing an existing setup

Discovery is strictly read-only and reports ambient state rather than adopting it:

- `GhCliNamedAccountDiscovery` lists stored `gh` accounts and proves one named account with `gh auth token --hostname <host> --user <login>` followed by an isolated `gh api user` under that token alone.
- `SshCommandAliasDiscovery` accepts an `ssh -G` result only when it is unambiguous: one concrete lowercase host, exactly one absolute identity file with no `%`, `$`, or `~` left in it, and no proxy command, jump host, or identity agent.
- `GitAmbientAuthConflictReader` reads `git config --show-origin --get-all <key>` and environment variable *names*.
- `GhCliRepositoryObserver` derives the local remote with `git config --local`, then reads the provider's answer under the explicitly selected named-account `ProcessAuthContext` — current name and host, then the stable numeric ID. A wrong globally active `gh` account and inherited token variables cannot influence observation; a rename still observes as the same repository.

`AuthMigrationService.inspect()` binds its plan to the exact source digest and configuration generation it saw. `apply()` re-gathers every input and distinguishes a vanished account, an ambiguous one, a stale plan, and one that still needs a human. Findings are evaluated against the transport the plan proposes: ambient GitHub token variables always block, a declared credential helper blocks only while no transport can be proposed (the pinned SSH transport never consults helpers, and the isolated HTTPS transport resets the ambient helper chain), and signing blocks only when a signing key or an enabled `commit.gpgsign` makes a signer actually active — Git's own `commit.gpgsign=false` plus `gpg.format=openpgp` defaults do not. A remote that disagrees with the observed target blocks. Ambiguous SSH configuration falls back to an explicitly proposed HTTPS transport rather than a guess.

Because adopting an identity is a capability expansion, the plan hash the operator transcribes after reading the inspection is recorded as their explicit approval of exactly that content.

## Prohibited global mutations

No code path may invoke `gh auth switch`, `git config --global`, `git config --system`, any SSH configuration write, or ambient credential-helper fallback. Discovery adapters have no method that can pass a mutating flag, and the CLI tests assert that no recorded argv contains `auth switch`, `--global`, `--system`, `--replace-all`, `--unset`, `ssh-keygen`, or `ssh-add`.

## Compatibility mapping

A single-account installation imports or selects one local profile, creates one binding for each enrolled repository ID, and resolves `auto` only when exactly one binding matches. This preserves the existing one-account workflow without relying on `gh auth switch`, global Git configuration mutation, inherited `GH_TOKEN`, or opportunistic SSH-agent selection.

## Failure and recovery contract

`RepositoryAuthFailureCode` separates resolution ambiguity, actor and transport mismatch, lease lifecycle, remote rewrite, publication mismatch, nested-resource denial, author/committer/signer mismatch, enterprise authorization, and network policy failures. Provider adapters preserve typed failures such as SSO authorization, installation approval, actor/repository mismatch and unavailable reviewed signers instead of collapsing them into a generic provider error. Recovery is represented by typed `RecoveryAction` values such as reselecting a profile, reauthorizing, refreshing an equivalent lease, reconciling a binding, reviewing a remote, or aborting. Recovery parameters contain safe identifiers only; recovery never switches the global account, broadens capability, retries with ambient credentials or silently rebuilds missing durable identity.

## Maintained call-site inventory

`REPOSITORY_IDENTITY_SURFACES.json` classifies every production adapter that invokes Git, GitHub CLI, SSH/LFS executables, or direct subprocess discovery. Its architecture test fails whenever a new identity-sensitive adapter appears without a reviewed classification and owning EPIC ticket.
