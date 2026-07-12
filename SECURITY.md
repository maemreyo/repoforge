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
- tool and command activity is written to a local JSONL audit log.

## Important limitations

This is a personal developer tool, not a hardened multi-tenant service. Run it only on a machine
and OpenAI tunnel that you control. Review diffs before committing or pushing. Keep write-tool
confirmations enabled in ChatGPT.

Do not add secrets to verification command arguments or to the MCP configuration. Subprocesses
receive only a small allowlist of environment variables, but commands can still access files that
the local OS account can access.
