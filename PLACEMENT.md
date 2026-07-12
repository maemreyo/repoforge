# Placement map

Copy these files into the RepoForge source tree:

```text
AGENTS.md
docs/FULL_FLOW_TESTING.md
docs/TEST_RUN_RECORD.md
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

2. Install the runtime copy used by RepoForge:

```text
~/.config/repoforge/config.toml
```

Install it with:

```sh
mkdir -p ~/.config/repoforge
cp config.work-frontier.toml ~/.config/repoforge/config.toml
```

Do not place RepoForge's `AGENTS.md` inside Work Frontier. Work Frontier already has its own
repository-specific root `AGENTS.md`, which should remain authoritative for that codebase.
