# Local repository discovery and automatic configuration

RepoForge can discover repositories under one or more **explicit local roots** and render a reviewed,
multi-repository TOML configuration. Scanning is a CLI setup operation; it is not exposed as an MCP
tool to ChatGPT.

## Preview before writing

```sh
rf scan-repos /Users/trung.ngo/Documents/zaob-dev --max-depth 2
```

Render the proposed TOML without writing it:

```sh
rf scan-repos /Users/trung.ngo/Documents/zaob-dev \
  --max-depth 2 \
  --render-config
```

The preview reports each detected repository, repository ID, ecosystem, package manager,
instruction files, profiles, commands, and warnings. Review every detected command before enabling
write-capable MCP access.

## Generate a multi-repository config

```sh
rf --config ~/.config/repoforge/config.toml init \
  --scan-root /Users/trung.ngo/Documents/zaob-dev \
  --max-depth 2
```

Repeat `--scan-root` to combine explicit roots. Existing config files are never overwritten unless
`--force` is supplied.

## Safety behavior

The scanner:

- requires explicit roots and never scans the whole machine by default;
- does not follow symlinks;
- skips hidden directories unless `--include-hidden` is explicitly provided;
- always skips `.git`, virtual environments, dependency trees, caches, and common build outputs;
- stops descending when it finds a top-level Git repository;
- bounds directory depth and repository count;
- assigns deterministic unique IDs when repositories share the same directory name;
- only reads repository metadata and manifests;
- does not modify repositories, create worktrees, push, or create pull requests.

Hidden-directory scanning does not disable the hard exclusions or symlink protection.

## Detection strategy

RepoForge prefers repository-owned canonical commands:

1. Makefile targets such as `setup`, `lint`, `typecheck`, `test`, `build`, `check`, and `verify`.
2. JavaScript package-manager scripts.
3. Python `uv`, Ruff, Mypy, Pytest, and build metadata.
4. Standard Rust and Go commands.

Detection is a starting point, not an authorization decision. Generated profiles remain allowlisted
commands, and operators should narrow path/change budgets for sensitive repositories.

## RepoForge self-configuration

A hand-reviewed self-hosting configuration is supplied at:

```text
config.repoforge.toml
```

It targets:

```text
/Users/trung.ngo/Documents/zaob-dev/repoforge
```

To use RepoForge and Work Frontier from one Plugin, generate a multi-repository config with the
scanner or manually combine the two repository tables beneath one `[server]` table.
