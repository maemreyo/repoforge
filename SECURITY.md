# Security model

RepoForge is intentionally not a general shell or filesystem MCP server.

Its main controls are:

- repositories must be explicitly allowlisted in `config.toml`;
- all modifications happen inside isolated Git worktrees;
- write branches must use the configured prefix, normally `ai/`;
- protected branches are rejected;
- file paths are canonicalized and cannot escape the worktree;
- sensitive path patterns are denied by default;
- changed symlinks and submodule/gitlink entries are rejected;
- commands exposed to the model are predefined profiles, never arbitrary shell text;
- `git push --force`, merge, secret management, repository administration, and workflow edits
  are not implemented;
- pull requests are always created as drafts;
- optional verification gating binds a successful check to the exact working-tree fingerprint;
- exact idempotency keys are bound to the normalized mutation payload and reviewed state, so a lost response can be replayed without applying the write twice;
- deterministic verification-failure reuse is evidence only, never creates a success receipt, and is disabled for timeout, cancellation, network, corrupt, incomplete, or retryable outcomes;
- GitHub issues, native sub-issues, and blocked-by relationships are authoritative; Project consistency is read-only and the legacy apply mode is retired;
- code-intelligence providers receive only bounded policy-visible paths and their results are rejected when the workspace identity changes during collection;
- runtime health returns normalized capability facts and hashes, not raw MCP initialize payloads;
- tool and command activity is written to a local JSONL audit log;
- model-requested configuration changes (`repo_policy_apply`) pass through the same immutable
  generation pipeline as the CLI and are gated by capability delta: restrictions and
  metadata-only edits apply immediately, while any capability expansion (new commands, broader
  paths) is only stored as a pending change the operator must approve out of band with
  `rf config approve`; the approval token never passes through the model conversation.

## Important limitations

This is a personal developer tool, not a hardened multi-tenant service. Run it only on a machine
and OpenAI tunnel that you control. Review diffs before committing or pushing. Keep write-tool
confirmations enabled in ChatGPT.

Do not add secrets to verification command arguments or to the MCP configuration. Subprocesses
receive only a small allowlist of environment variables, but commands can still access files that
the local OS account can access.

The built-in syntax/import code-intelligence adapter is advisory and incomplete by design. It does
not resolve runtime dispatch, reflection, package-manager aliases, generated code, or every language.
Low coverage, malformed input, provider failure, and stale snapshots must remain explicit and must not
reduce the configured final verification gate.

TaskCapsule and approval persistence are foundations rather than a complete multi-user authorization
system. Capability expansion still requires the existing out-of-band operator approval flow.

## Optional GitHub webhook ingress

The webhook listener is disabled by default and binds to `127.0.0.1` by default. It accepts only
`issues`, `sub_issues`, `issue_dependencies`, and `projects_v2_item`, verifies
`X-Hub-Signature-256` before parsing JSON, bounds body and delivery IDs, and can only invalidate
repository-scoped graph cache entries. It cannot run commands or write to GitHub. Keep the secret in the
configured environment variable and expose the endpoint only through a trusted TLS proxy or tunnel.
