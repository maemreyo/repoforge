# Interactive onboarding UI

`rf onboard` supports a dependency-free plain terminal and an optional visual terminal experience.

## Install the optional UI packages

When RepoForge is installed as a uv tool, add the terminal packages to that tool environment:

```bash
uv tool install --force \
  --with rich \
  --with InquirerPy \
  'git+https://github.com/maemreyo/repoforge.git@main'
```

Rich provides panels, tables, highlighted configuration diffs, prompts, and confirmations. When InquirerPy is also available on the real terminal, repository and default selection use arrow-key navigation and checkbox controls. RepoForge still works without either package.

## UI selection

```bash
rf onboard /absolute/projects/root --ui auto
rf onboard /absolute/projects/root --ui rich
rf onboard /absolute/projects/root --ui plain
```

- `auto` chooses Rich on an interactive terminal when installed and otherwise falls back to plain output.
- `rich` requires Rich; InquirerPy remains optional.
- `plain` always uses the built-in text interface.
- Pipes and non-TTY environments always use the plain adapter.

## Default recommendation policy

```bash
rf onboard /absolute/projects/root --defaults ask
rf onboard /absolute/projects/root --defaults safe
rf onboard /absolute/projects/root --defaults none
```

- `ask` is the interactive default. It shows fail-closed recommendations in a multi-select list with the recommendations selected initially.
- `safe` applies all available fail-closed recommendations and proceeds to genuinely ambiguous choices.
- `none` asks every unresolved decision.

Recommendations can disable networked setup, autofix, risky commands, publishing, or writable handling of special repository features. They never guess among multiple remotes, package managers, base branches, monorepo scopes, or working directories.

## Review flow

The interactive review is presented as one batch:

1. Discovery
2. Safe defaults
3. Ambiguous decisions
4. Repository summaries and exact approvals
5. Source configuration diff
6. Apply

Repository approvals are not preselected, and the final apply confirmation defaults to no. `--plan-only` stops after the configuration diff.

## Automation

`--non-interactive` never loads Rich or InquirerPy and never infers decisions. Supply exact decisions and approvals:

```bash
rf onboard /absolute/projects/root \
  --non-interactive \
  --defaults none \
  --tunnel-id tunnel_... \
  --decision demo.dependency_install=exclude \
  --approve approve:PROPOSAL_ID
```

Passing `--defaults safe` or `--defaults ask` with `--non-interactive` is rejected so automated runs remain deterministic and auditable.
