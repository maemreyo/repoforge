# GitHub Capability and Enterprise Preflight Design

## Status

Approved for implementation as issue #293 in EPIC #284.

## Objective

Translate each operation's exact capability requirements into GitHub-specific permission and enterprise-policy evidence, bind that evidence to the operation-scoped auth lease and publication intent, and deny affected external writes whenever required evidence is denied, unavailable, stale, or unobservable.

## Scope boundary

Issue #51 remains authoritative for whether a stage may consume credentials or perform an external write. This design does not create a parallel capability policy. It supplies GitHub-specific evidence and enforcement for the exact capability request already admitted by the policy layer.

Issue #52 remains the provider-neutral workload identity foundation. This design uses repository-bound AuthLease and OperationIdentityContext contracts from EPIC #284 and does not introduce a second identity system.

The existing `CommandGitHubCapabilityProbe` remains a doctor/discovery probe for issue-graph capabilities. It uses the ambient GitHub CLI session and is therefore not suitable for security authorization or write-time revalidation.

## Locked constraints

- No global `gh auth switch`.
- No host-global Git configuration mutation.
- No ambient credential fallback.
- No raw token, private key, authorization header, or credential-helper output in logs, state, diagnostics, exceptions, fixtures, receipts, or model-visible output.
- No permission expansion inferred from organisation role, repository `viewerPermission`, or repository push access.
- No retry with a stronger profile or token.
- Missing or unobservable evidence broadens uncertainty and denies the affected external write.
- Preflight performs bounded read-only observation. It does not mutate provider state to test permissions.
- Revalidation must use the same operation-scoped actor, installation, repository, capability ceiling, and credential material identity.

## Architecture decision

Create a dedicated GitHub capability-preflight domain and port rather than extending the existing doctor probe or embedding all checks in the publication adapter.

The new boundary has three responsibilities:

1. Map exact RepoForge capability IDs to the minimum GitHub permission requirements and evidence probes.
2. Execute those probes with the exact operation-scoped GitHub API token in an isolated environment.
3. Produce deterministic, secret-free evidence digests that can be pinned in AuthMaterial, AuthLease, PublicationAuthorization, and durable publication receipts.

## Domain model

### Capability catalogue

`GitHubOperationCapability` distinguishes the provider operations required by RepoForge:

- `contents_read`
- `contents_write`
- `issues_read`
- `issues_write`
- `pull_requests_read`
- `pull_requests_write`
- `workflows_read`
- `workflows_write`
- `releases_read`
- `releases_write`
- `projects_read`
- `projects_write`
- `packages_read`
- `packages_write`

The catalogue is intentionally operation-oriented. It does not expose coarse aliases such as `github_api_write` as sufficient proof for all write families.

### Requirement matrix

Each capability maps to a `GitHubCapabilityRequirement` containing:

- the exact capability ID;
- the minimal GitHub App permission name and level where GitHub exposes one;
- one or more bounded read-only evidence probes;
- whether the provider can prove availability without a mutation;
- the recovery category appropriate for a failure.

Stored human accounts can have an empty provider permission list because GitHub does not expose a complete fine-grained permission manifest for every token kind. Their capability availability must still be proven through exact bounded API observations. GitHub App installations must match the explicit minimal permission set returned by the token endpoint.

### Evidence states

`GitHubCapabilityEvidenceState` has five values:

- `proven_available`: direct evidence proves the requested capability is usable for the exact actor/repository.
- `proven_denied`: provider evidence proves the requested capability is denied.
- `likely_policy_denied`: provider evidence strongly identifies an enterprise policy such as SAML SSO, fine-grained token approval, installation approval, ruleset, workflow restriction, or IP allowlist.
- `provider_unavailable`: the provider or network could not supply evidence.
- `unobservable`: the capability cannot be proven safely without mutation or required evidence is not exposed.

Only `proven_available` authorizes an affected external write.

### Preflight request

`GitHubCapabilityPreflightRequest` contains:

- host;
- actor ID;
- repository stable ID;
- installation ID when applicable;
- exact requested capability IDs;
- exact permission IDs observed for the token or installation;
- policy revision;
- config revision;
- observation timestamp;
- operation-scoped process auth context.

The request rejects duplicates, unknown capability IDs, non-stable target identifiers, and an empty capability set.

### Preflight report

`GitHubCapabilityPreflightReport` contains:

- exact actor, repository, and installation identity;
- one result per requested capability and no extra capability result;
- exact observed permission IDs;
- policy/config revisions;
- observation timestamp;
- deterministic capability digest;
- deterministic permission digest;
- deterministic evidence digest.

Digest inputs are canonical JSON projections of safe metadata only. Raw credentials and provider response bodies are excluded.

## Adapter behavior

### Isolated execution

The adapter runs `gh api` through `CommandExecutor.run_isolated` using the `ProcessAuthContext` environment and its secret redaction list. It never calls ambient `CommandExecutor.run` and never invokes `gh auth switch`.

Every request uses the exact host and stable repository ID. Canonical names may appear only as display metadata.

### Bounded evidence probes

The first implementation covers the following read-only evidence families:

- repository metadata and exact repository ID;
- issue and pull-request endpoints;
- Actions permissions and workflow metadata;
- release metadata;
- Project V2 GraphQL read access where a project target is supplied by the operation;
- package metadata where a package target is supplied by the operation;
- installation repository scope for GitHub Apps;
- branch/ruleset metadata relevant to the exact destination ref;

The adapter issues only probes needed by the requested capability set. A pull-request write preflight does not probe packages or workflows.

### Failure classification

Provider responses are classified without relying on message text alone when structured status, headers, or payload fields are available.

- SAML SSO authorization required -> `GITHUB_SSO_AUTHORIZATION_REQUIRED`.
- Fine-grained token pending or denied approval -> `GITHUB_TOKEN_APPROVAL_REQUIRED`.
- GitHub App installation approval or repository scope missing -> `GITHUB_INSTALLATION_APPROVAL_REQUIRED` or `GITHUB_API_REPOSITORY_MISMATCH`.
- Ruleset or branch-policy rejection -> `GITHUB_RULESET_POLICY_DENIED`.
- Actions workflow policy restriction -> `GITHUB_WORKFLOW_POLICY_DENIED`.
- IP allowlist, VPN, or network policy evidence -> `GITHUB_NETWORK_POLICY_DENIED`.
- Provider outage or inconclusive transport failure -> `GITHUB_PROVIDER_UNAVAILABLE`.
- Required evidence that cannot be observed safely -> `GITHUB_ENTERPRISE_EVIDENCE_UNOBSERVABLE`.
- Missing exact provider permission or endpoint access -> `GITHUB_API_PERMISSION_DENIED`.

Recovery actions are typed and narrow. They may direct the operator to authorize SSO, approve the token or installation, review the exact ruleset/network policy, or grant the exact missing permission. They must never suggest switching to a stronger profile automatically.

## Auth material and lease integration

`GitHubApiAuthProvider` performs preflight after actor/repository/installation identity verification and before returning `AuthMaterial`.

The provider stores only these safe preflight fields in `AuthMaterial.provider_metadata`:

- `github_preflight_evidence_digest`;
- `github_capability_digest`;
- `github_permission_digest`;
- `github_preflight_observed_at`;
- existing GitHub kind, host, repository ID, and installation ID fields.

`github_api_auth_lease` propagates that safe metadata unchanged into `AuthLease`.

Same-identity refresh requires the replacement material to preserve actor, installation, repository, requested capability set, and policy/config revisions. Capability or permission digest drift is not accepted as a silent refresh. The caller must re-run policy admission and create a new operation identity decision.

## Publication integration

`PublicationAuthorization` continues to carry `capability_digest` and `permission_digest`, but production construction must derive them from the operation-scoped preflight report rather than caller-supplied placeholders.

Immediately before the external effect, publication revalidation must:

1. Revalidate the operation-scoped AuthLease.
2. Re-run the exact GitHub capability preflight with the same requested capability set and process auth context.
3. Compare actor, installation, repository, policy/config revisions, capability digest, permission digest, and evidence state.
4. Reject any denied, unavailable, unobservable, or drifted evidence before the idempotency effect boundary begins.
5. Bind the fresh preflight digests into `ReviewedPublication` and the durable publication result/receipt.

No reconciliation path may bypass the original capability evidence. Reconciliation can validate the exact original PublicationIntent only.

## Error handling and safe recovery

All failures before the publication effect boundary report that no external write was started.

Typed errors include safe details such as capability IDs, repository ID, installation ID, evidence state, and policy category. They exclude raw provider payloads, headers containing credentials, tokens, user emails, and unbounded command output.

Retryability rules:

- SSO, token approval, installation approval, ruleset, workflow, and network policy denials are non-retryable until an operator changes the exact policy condition.
- Provider unavailability is retryable with the same profile and capability ceiling.
- Unobservable evidence is non-retryable for writes; a product change or explicit policy-approved alternative evidence source is required.
- Permission denial is non-retryable until the exact permission is granted.

## Testing strategy

### Domain tests

- Complete capability-to-minimal-permission matrix.
- Unknown, duplicate, and empty capability request rejection.
- Deterministic, order-independent, secret-free digests.
- One result per requested capability and no extras.
- Write authorization accepts only `proven_available`.

### Adapter tests

- Only required endpoints are probed.
- Exact token, host, actor, installation, and stable repository ID are used through isolated execution.
- SSO, token approval, installation scope, ruleset, workflow policy, network/IP policy, provider outage, and unobservable evidence fixtures.
- GitHub App permission mismatch and stored-account evidence behavior.
- Secret canaries absent from exception text, safe payloads, metadata, and receipts.

### Lease tests

- Preflight digests persist in AuthMaterial and AuthLease safe metadata.
- Same-identity refresh with unchanged capability evidence succeeds.
- Capability, permission, installation, repository, actor, or policy revision drift fails deterministically.

### Publication tests

- Exact preflight occurs after durable identity/lease validation and before the effect boundary.
- TOCTOU changes between initial preflight and publication are rejected.
- Provider unavailable or unobservable evidence blocks publication.
- Lost-response reconciliation preserves the original capability and permission digests.

### Verification

- Run focused RED/GREEN selectors for each task.
- Run the always-on safety bundle and affected identity/publication suites.
- Update test groups, coverage map, repository identity documentation, and identity surface inventory.
- Run authoritative RepoForge `verify` on the clean exact fingerprint before completion.

## Out of scope

- Implementing #51 capability admission policy itself.
- Managing SSO, organisation approval, VPN, IP allowlists, rulesets, workflow policy, or GitHub organisation settings.
- Automatically requesting broader scopes or selecting another credential profile.
- Nested submodule, LFS, package, or release identity routing owned by #294. This issue may model package/release capability IDs and preflight evidence for the current repository, but target-specific nested routing remains #294.
- CLI/MCP profile configuration and migration UX owned by #295.
