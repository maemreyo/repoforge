# Placement map

Copy these files into the RepoForge source tree:

```text
AGENTS.md
docs/testing/FULL_FLOW_TESTING.md
docs/testing/TEST_RUN_RECORD.md
scripts/e2e-preflight.sh
.github/PULL_REQUEST_TEMPLATE.md
```

Make the script executable:

```sh
chmod +x scripts/e2e-preflight.sh
```

The supplied Work Frontier configuration has two useful placements:

1. Keep the tracked example in RepoForge:

```text
config.work-frontier.toml
```

2. Enroll it into the reviewed runtime configuration. The example is a v2 *source*
   config, so it must pass through the generation pipeline rather than being copied
   over `~/.config/repoforge/config.toml` directly:

```sh
mkdir -p ~/.config/repoforge
cp config.work-frontier.toml ~/.config/repoforge/config.toml
rf repo refresh work-frontier --accept
```

To add Work Frontier to an existing multi-repository configuration instead, merge its
`[[repo]]` entry (including the `[repo.policy_patch.*]` tables) into
`~/.config/repoforge/config.toml` and run the same `rf repo refresh` command.

Do not place RepoForge's `AGENTS.md` inside Work Frontier. Work Frontier already has its own
repository-specific root `AGENTS.md`, which should remain authoritative for that codebase.
