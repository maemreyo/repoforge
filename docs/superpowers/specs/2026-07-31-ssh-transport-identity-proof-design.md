# SSH Transport Identity Proof Design

**Source:** strict independent review of merged PR #365 / issue #364  
**Status:** Approved in conversation on 2026-07-31  
**Scope:** repository-bound SSH transport identity for multi-account GitHub repositories

## Objective

Make an SSH-alias repository such as `git@github-work:cicdata-io/portal-spa.git` operational through the full RepoForge identity-bound path without changing the repository remote or the operator's SSH configuration. The selected GitHub account, provider/API host, repository identity, SSH endpoint, key material, and principal must be separate pieces of evidence that are joined only through explicit validated contracts.

The design replaces the partial behavior merged in #365. That change canonicalized an alias during repository observation and persisted `source_ssh_alias`, but the runtime still compares the raw alias host with the canonical provider host and executes SSH with ambient configuration disabled. A migration can therefore succeed while `ls-remote`, fetch, push, publication revalidation, or reconciliation remains unusable.

## Goals

1. Keep stable repository identity independent from machine-local transport configuration.
2. Make the selected reviewed auth profile authoritative for the GitHub API host.
3. Resolve SSH aliases without executing command-bearing OpenSSH configuration.
4. Pin the effective SSH endpoint, public-key fingerprint, and principal rather than only an identity-file path.
5. Execute Git through a canonical endpoint with ambient SSH configuration and agent state disabled.
6. Re-prove one immutable endpoint contract during migration apply and immediately before every network effect.
7. Preserve deterministic fail-closed behavior for ambiguity, drift, key replacement, provider mismatch, and principal mismatch.
8. Support GitHub Enterprise through profile-driven host authority rather than a `github.com` special case.
9. Preserve legacy configuration readability without treating incomplete legacy SSH metadata as write authority.

## Non-goals

- Modifying `~/.ssh/config`, global Git configuration, the active `gh` account, or repository remotes.
- Supporting arbitrary OpenSSH configuration semantics.
- Treating repository access as proof that an SSH key belongs to a selected human account.
- Adding general secret management or a second credential broker.
- Supporting deploy keys as human stored-account identities without an explicit deploy-key principal model.
- Replacing HTTPS transport, publication intent, repository bindings, capability preflight, or operation identity receipts.

## Current failure analysis

The merged path contains three incompatible representations:

1. Repository observation records `provider_host=github.com` and `transport_alias=github-work`.
2. Migration persists `github_host=github.com`, `source_ssh_alias=github-work`, and `ssh_identity_file=.../id_rsa_work`.
3. `GitTransportRouter` receives the live raw remote URL, extracts `github-work`, compares it with `GitTransportSpec.provider_host=github.com`, and rejects it. Even if the comparison were relaxed, `ssh -F /dev/null` cannot resolve `github-work`.

The current model also allows a remote/SSH alias to choose the host passed to `gh api`, performs `ssh -G` in normal observation, fingerprints only the key path, does not link the SSH principal to the selected GitHub actor, and may silently propose HTTPS after SSH discovery fails while the repository remote remains SSH.

These are not independent local bugs. They result from using one host field for provider identity, raw remote syntax, alias resolution, and transport execution.

## Approaches considered

### A. Patch the existing alias flow

Relax the router host comparison, add `HostName` to `GIT_SSH_COMMAND`, and retain `transport_alias` inside `RepositoryIdentityObservation`.

This would unblock the immediate URL mismatch with limited code, but repository identity would remain coupled to machine-local state, `ssh -G` would remain an implicit evaluator, key/principal drift would remain unproved, and inspect/apply/write would still use different evidence. Rejected.

### B. Structured transport endpoint proof — selected

Separate repository identity from transport evidence. Parse a safe subset of SSH configuration, inspect and bind the key and principal, persist a structured endpoint proof, and execute a canonical URL under the reviewed key.

This introduces new domain and adapter contracts but produces one deterministic proof that can be hashed, persisted, revalidated, and consumed by all Git and publication paths.

### C. Force migrated stored accounts to HTTPS

Use the named `gh` account token for all Git transport and treat SSH remotes as requiring manual conversion.

This is simpler and gives strong API/transport actor alignment, but it violates the product requirement to preserve existing SSH remotes and multi-key workflows. It remains a valid explicit operator-selected alternative, not an automatic fallback.

## Domain boundaries

### Stable repository identity

`RepositoryIdentityObservation` returns only provider-owned facts:

- provider;
- reviewed provider host;
- stable repository ID;
- provider-confirmed canonical owner/repository name;
- existence;
- observation time;
- configuration revision.

It contains no SSH alias, local path, key reference, port, or remote URL digest.

### Local remote evidence

A new pure domain module, `repoforge.domain.git_remote_identity`, owns secret-free values:

- `ParsedGitRemote`: transport kind, raw host, owner, repository, user, port, and raw URL digest;
- `SshAliasDefinition`: exact alias, canonical host, user, port, identity-file reference, and source config digest;
- `SshKeyProof`: canonical file path, public-key SHA256 fingerprint, file owner UID, safe mode facts, and proof time;
- `SshPrincipalProof`: provider host, principal kind, observed principal ID/login, key fingerprint, and proof time;
- `ReviewedSshEndpoint`: raw host/alias, canonical host, user, port, repository path, key proof, principal proof, and an endpoint proof digest.

All constructors enforce bounded values, lowercase canonical hosts, exact owner/repository names, absolute paths, unique fingerprints, safe ports, and digest formats. Private key bytes, public key bodies, tokens, remote userinfo, and command output never enter domain payloads.

### Transport specification

`GitTransportSpec` gains a structured SSH endpoint proof or a stable reference to one. `provider_host` continues to mean the credential/API provider boundary. The raw alias is never overloaded into that field.

An SSH transport is valid only when:

- endpoint canonical host equals `provider_host`;
- endpoint key fingerprint equals the reviewed credential fingerprint;
- principal proof is compatible with the profile actor and expected actor ID;
- endpoint repository path matches the stable repository target;
- allowed access contains the requested operation.

## Safe SSH alias resolution

### Constrained parser

Normal observation and write paths must not invoke `ssh -G`. A new adapter parses a deliberately restricted OpenSSH configuration subset from an explicit user-config path resolved through the effective OS user:

- `Host` with exact non-wildcard aliases;
- `HostName`;
- `User`;
- `Port`;
- exactly one `IdentityFile`;
- `IdentitiesOnly yes`.

The parser refuses an alias when its effective configuration cannot be decided without full OpenSSH evaluation. The following are blocking when they could affect the selected alias:

- `Match`;
- `Include`;
- wildcard or negated `Host` patterns;
- repeated matching blocks;
- multiple identity files;
- `ProxyCommand`, `ProxyJump`, or `IdentityAgent`;
- hostname canonicalization directives;
- token expansion other than a leading `~/` resolved through the effective OS-user home;
- non-`git` user for GitHub unless a provider adapter explicitly allows it;
- missing or unsafe port values.

Unrelated exact `Host` blocks may be ignored. The parser records a digest of the exact source configuration and selected block so apply/write revalidation can detect drift.

### Diagnostic compatibility

`rf auth import ssh` may retain an explicitly requested `ssh -G` diagnostic mode for operator visibility, but its result is advisory unless it also passes the constrained parser and proof pipeline. Normal repository observation, migration authority, and write admission never depend on `ssh -G`.

## Provider-host authority and repository observation

The selected named account or existing reviewed profile supplies `expected_provider_host`. The remote endpoint resolver may prove that `github-work` canonicalizes to `github.com`, but it may not choose the API host.

The ordered flow is:

1. Parse the local remote into raw transport facts without credentials.
2. Select or explicitly name a GitHub account and provider host.
3. Resolve a raw SSH alias through the constrained parser.
4. Require the resolved canonical host to equal the selected provider host.
5. Only then invoke the GitHub API at the selected provider host under the selected account context.
6. Require the provider-confirmed repository path and stable ID to match the local target.

For a legacy unbound repository, GitHub.com may remain the CLI default host. GitHub Enterprise migration requires an explicit `--host` or a reviewed repository/provider declaration; it is never inferred as credential authority from an SSH alias.

## Key proof and operation-scoped materialization

### Migration proof

`SshKeyInspector` resolves the identity file against the effective OS-user home, rejects symlinks, requires a regular file owned by the effective user, rejects group/world writable modes, and obtains a SHA256 public-key fingerprint through a bounded `ssh-keygen` adapter. The adapter exposes only the fingerprint and safe file metadata.

A path-derived digest is not a credential fingerprint. The profile credential fingerprint is the public-key fingerprint or a digest that includes it and the canonical endpoint identity.

### Write-time proof

Immediately before a transport effect, RepoForge:

1. opens the reviewed identity file with no-follow semantics;
2. validates file type, owner, and permissions from the opened descriptor;
3. re-derives the public-key fingerprint;
4. requires equality with the reviewed proof;
5. preferably passes the already-open descriptor to the child, or copies from that descriptor into an operation-scoped `0600` temporary identity file without reopening the reviewed path;
6. runs SSH only against that operation-scoped materialization;
7. closes descriptors and removes temporary paths on every exit path. Best-effort overwrite may be used, but the design does not claim reliable filesystem zeroisation.

Private key bytes are confined to the material provider and SSH child boundary. They never enter argv, environment values, domain objects, logs, exceptions, receipts, or persistent test artifacts; tests use synthetic disposable key fixtures. Using the same opened source for proof and execution closes the path-replacement race between validation and SSH consumption.

## SSH principal binding

A stored human account profile cannot treat an arbitrary repository-capable key as the selected account.

`SshPrincipalVerifier` is provider-specific. For GitHub.com it performs a bounded, non-interactive `ssh -T` probe against the canonical host using the reviewed operation-scoped key, with ambient config, agent, prompts, password, and keyboard-interactive authentication disabled. It accepts the known no-shell response only when the observed login equals the selected named account login and expected actor mapping.

A key that authenticates as another login, a repository/deploy-key principal, an unknown response, or an unavailable provider proof blocks automatic human-profile migration. GitHub Enterprise uses a provider adapter when equivalent evidence is supported; otherwise it requires an explicit durable operator attestation bound to provider host, actor ID, key fingerprint, and repository boundary.

Principal proof is revalidated during apply and before an SSH write effect. A bounded cache is permitted only when its provider host, profile, actor, key fingerprint, and proof revision all match and its policy-defined TTL has not expired. Key drift, account changes, revocation signals, configuration changes, or expiry require a fresh provider principal probe before the effect boundary.

## Canonical Git execution

RepoForge preserves the repository's raw remote for topology and drift evidence but does not pass an alias URL to isolated SSH.

Before `ls-remote`, fetch, push, publication revalidation, or reconciliation:

1. parse the live effective remote and compare its digest and repository path with the reviewed endpoint;
2. re-resolve the alias definition and key proof;
3. construct a canonical execution URL such as `ssh://git@github.com:22/cicdata-io/portal-spa.git`;
4. execute with `ssh -F /dev/null`, `IdentitiesOnly=yes`, `IdentityAgent=none`, batch/no-prompt settings, the reviewed port, and the operation-scoped identity material;
5. record evidence for both the raw remote digest and canonical execution endpoint digest.

The transport router compares the canonical execution host with `GitTransportSpec.provider_host`; it never compares the raw alias string with the provider host.

Publication topology continues to prove fetch/push URLs, rewrites, stable repository IDs, refs, and remote versions. The publication adapter passes the reviewed endpoint proof to the transport router, which performs canonical execution without changing topology authority.

## Migration behavior

`AuthMigrationService` gathers one coherent evidence snapshot:

- selected named account and provider host;
- parsed raw remote;
- resolved alias definition when present;
- repository API observation;
- key proof;
- principal proof;
- ambient Git/commit findings;
- source and accepted generation revisions.

The plan hash binds every safe field and proof digest. `apply` re-gathers the same evidence and refuses drift.

An SSH remote with unresolved, unsafe, or unproved SSH evidence is blocking. Migration never silently creates an HTTPS profile while the effective remote is SSH. HTTPS is proposed only when the effective remote is HTTPS or when a separate reviewed plan explicitly changes transport policy and remote configuration.

## Backward compatibility

Existing source configurations containing only `source_ssh_alias` and `ssh_identity_file` remain parseable. They are represented as legacy incomplete SSH transport metadata.

Read-only profile inspection reports the missing endpoint/key/principal proof. Any operation requiring SSH network access returns a typed migration-required error with a safe command to inspect and re-prove the profile. Legacy data is not automatically upgraded into write authority.

HTTPS profiles and canonical SSH profiles receive additive defaults only when their provider host, endpoint, key fingerprint, and principal evidence are already complete. Persisted operation identity records remain decodable; new operations require the current proof schema version.

## Error model

Add or refine typed errors for:

- unsafe or undecidable SSH configuration;
- alias/provider-host mismatch;
- endpoint proof stale;
- SSH key path, ownership, mode, symlink, or fingerprint mismatch;
- SSH principal mismatch or unavailable proof;
- transport kind incompatible with live remote;
- legacy SSH profile requiring migration;
- canonical execution target mismatch.

Every pre-effect failure states that no Git network write or alternate credential was attempted. Recovery never suggests switching the global `gh` account, enabling an SSH agent, accepting another key, or falling back to a stronger profile.

## Composition

Production bootstrap composes explicit ports:

- `GitRemoteParser`;
- `SshConfigAliasResolver`;
- `SshKeyInspector`;
- `SshPrincipalVerifier`;
- `RepositoryObserver` using an expected provider host;
- `GitTransportEndpointRevalidator`;
- operation-scoped SSH material provider;
- canonicalizing `GitTransportRouter`.

CLI and managed runtime use the same domain contracts and adapters. Tests may replace ports but must not bypass endpoint validation at the application or publication boundary.

## Testing strategy

Every behavior change follows RED, observed expected failure, GREEN, then refactor.

Required public-seam coverage:

1. Pure parsing and domain invariants for raw remotes, exact alias blocks, proof digests, and compatibility.
2. Alias parser cases for wildcard, negation, `Match`, `Include`, proxy, agent, canonicalization, multiple keys, user, port, and home resolution.
3. Key inspector cases for symlink, replacement, owner/mode drift, fingerprint drift, and secret-safe output.
4. Principal verification for matching human account, wrong account, deploy-key-like response, malformed response, unavailable provider, and GHES attestation.
5. Repository observation proving provider host comes from the selected profile and mismatches fail before API invocation.
6. Migration inspect/apply proving one evidence snapshot and no SSH-to-HTTPS fallback.
7. `GitTransportRouter` coverage from a raw aliased remote to canonical `ls-remote`, fetch, and push command construction.
8. Publication revalidation and reconciliation using raw topology evidence plus canonical transport execution.
9. Legacy profile parsing and write-time migration-required behavior.
10. Secret corpus scans across config, plans, logs, exceptions, receipts, subprocess captures, and temporary material cleanup.
11. Production composition tests with no adapter overrides.
12. Live acceptance for `portal-spa` after merge and activation.

A passing migration unit test is insufficient unless the generated profile can complete the real transport and publication path.

## Delivery slices

1. Domain separation, proof schema, and legacy compatibility contract.
2. Constrained SSH alias parser and effective-user home resolution.
3. Key inspection, secure operation-scoped materialization, and fingerprint revalidation.
4. Provider-authoritative repository observation and principal verification.
5. Migration snapshot/apply behavior with no silent fallback.
6. Canonical Git transport execution and publication integration.
7. Production composition, diagnostics, docs, leak scans, and live acceptance.

Each slice ends with focused RED/GREEN evidence and a commit whose production changes are limited to that slice.

## Acceptance criteria

The implementation is complete only when all of the following hold on one final commit:

1. `portal-spa` keeps `git@github-work:cicdata-io/portal-spa.git` unchanged.
2. The profile selected for `portal-spa` is `matw-ngo`, with the expected GitHub actor ID.
3. The endpoint proof binds `github-work` to `github.com`, `User git`, the reviewed port, the canonical `id_rsa_work` path, its public-key fingerprint, and a matching SSH principal.
4. API observation uses `github.com` from the selected account/profile and refuses mismatches before API execution.
5. `inspect`, `apply`, `bind`, `whoami`, and `doctor` report no blocking findings after reviewed migration.
6. `ls-remote`, fetch, push, draft-PR publication revalidation, and reconciliation execute against the canonical host with the reviewed work key and no ambient SSH configuration or agent.
7. A changed alias, remote, key file, fingerprint, principal, provider host, repository ID, or proof revision fails closed before effect.
8. Canonical GitHub Enterprise SSH remotes do not require alias resolution solely because their host differs from `github.com`.
9. Legacy incomplete SSH profiles remain readable but cannot authorize writes.
10. Targeted, affected, production, and live acceptance evidence is current on the exact final fingerprint.
