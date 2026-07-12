# Configuration

RepoForge accepts multiple repositories in one configuration file. The smallest useful repository
entry contains only a path plus explicit command allowlists:

```toml
[repositories.repoforge]
path = "/Users/you/src/repoforge"

[repositories.repoforge.actions]
setup = ["make", "setup"]

[repositories.repoforge.checks]
quick = [["make", "lint"], ["make", "typecheck"]]
test = ["make", "test"]
full = ["make", "check"]
```

`actions` are runnable profiles that do not create a verification receipt. `checks` are verification
profiles and may satisfy the verified-commit gate. When `full` exists it becomes the default
verification profile; when there is exactly one check, that check becomes the default.

Commands remain argv arrays, not shell strings. A single command uses a flat array; a profile with
multiple commands uses an array of arrays.

Add repositories without editing TOML manually:

```bash
rf init --repo /Users/you/src/repoforge --repo-id repoforge
rf init --repo /Users/you/src/work-frontier --repo-id work-frontier
```

The first command creates the config. Later commands append repositories while preserving existing
server settings and repositories. Duplicate repository IDs are rejected. `--force` intentionally
replaces the entire file.

All previous configuration remains valid. Use the advanced profile form when a profile needs a
description, a timeout, or explicit verification behavior:

```toml
[repositories.work-frontier.profiles.recertify]
description = "Run exact-revision recertification"
verification = true
timeout_seconds = 3600
commands = [["make", "recertify-foundation"]]
```

Secure defaults are applied when server and repository policy fields are omitted. Override them only
when the repository needs a narrower path policy, different change limits, a different branch base,
or additional environment allowlisting.
