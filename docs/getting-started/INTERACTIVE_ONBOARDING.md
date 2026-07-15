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

- `safe` is the interactive default. It applies all available fail-closed recommendations and asks only genuinely ambiguous choices.
- `ask` is the explicit opt-in for the previous question-by-question behavior. It shows fail-closed recommendations in a multi-select list with the recommendations selected initially.
- `none` asks every unresolved decision.

Recommendations can disable networked setup, autofix, risky commands, publishing, or writable handling of special repository features. They never guess among multiple remotes, package managers, base branches, monorepo scopes, or working directories.

## Review flow

The default interactive review is one batch:

1. Discovery
2. Safe defaults
3. Any genuinely ambiguous decision
4. One consolidated review of every repository policy, decision, reason, and source-config diff

At the review prompt, press Enter to accept the whole batch, `e` to change one selected decision through its existing bounded prompt, or `q` to abort. Aborting a newly started session writes no configuration generation or runtime state and removes its provisional session and lock records. Aborting a resumed session preserves it for later use. `--plan-only` stops after rendering this review.

## Automation

`--yes` is the zero-prompt counterpart to accepting the default interactive review. It applies only fail-closed recommendations and exact proposal approvals; if a decision remains ambiguous, it returns exit code `3` without writing configuration or runtime state:

```bash
rf onboard /absolute/projects/root --yes --tunnel-id tunnel_...
```

`--non-interactive` remains available for fully specified automation and never loads Rich or InquirerPy. Supply exact decisions and approvals when not using `--yes`:

```bash
rf onboard /absolute/projects/root \
  --non-interactive \
  --defaults none \
  --tunnel-id tunnel_... \
  --decision demo.dependency_install=exclude \
  --approve approve:PROPOSAL_ID
```

`--defaults ask` remains interactive-only. `--defaults safe` is accepted with `--non-interactive` only for compatibility; it does not infer decisions unless `--yes` is also supplied.
