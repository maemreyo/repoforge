# Testing strategy

RepoForge can mutate local repositories and publish controlled GitHub changes, so verification is a
control-plane product rather than one undifferentiated pytest command. Each intent has a separate
scope, latency budget, evidence artifact, and authority boundary.

## Verification intents

Use the narrowest command that answers the current question:

```bash
make test       # affected tests plus the safety bundle; no coverage
make test-full  # complete behavioral suite; no coverage
make coverage   # one canonical full branch-coverage observation
make verify     # contracts, metadata, gates, format, lint, types, affected tests
make gate       # clean-tree authoritative production gate
```

`make test` is the default developer and model loop. It maps changes from
`REPOFORGE_TEST_AFFECTED_BASE` (default `main`) through the authoritative selector and widens to the
full suite when blast radius is unknown. `make test-full` is for broad behavioral confidence without
paying coverage instrumentation cost. `make coverage` is the only local command whose purpose is the
complete branch-coverage observation and 80% floor.

`make verify` is the pre-commit change gate. It does not duplicate canonical coverage, packaging, or
installed-wheel smoke. `make gate` is operator/CI authority and runs the production verifier,
including full coverage, release contracts, build, installed-wheel lifecycle, and clean-tree checks.
Do not run `make coverage` immediately before `make gate`; the latter already records that evidence.

## Selection metadata and shadow catalog

`tests/test-groups.toml` remains the execution authority during migration. Every test file has exactly
one group owner, and `parallel = false` is a correctness declaration rather than a performance knob.
Missing, invalid, or incomplete metadata fails before execution; it can never silently make more tests
parallel.

`tests/catalog.toml` is the declarative shadow model. It records capability ownership, source-to-test
edges, isolation/resource classes, platforms, Python versions, and cost classes. Validate it with:

```bash
uv run python scripts/test_catalog.py --check tests/catalog.toml
uv run python scripts/select_affected_tests.py \
  --path src/repoforge/domain/verification.py \
  --shadow-catalog tests/catalog.toml
```

The catalog is intentionally **shadow-only**. The old selector continues to choose executed tests.
Cutover requires an evidence window with zero shadow false negatives; a mismatch is reported rather
than hidden by merging the two selections.

## Evidence artifacts

Affected, full, and coverage runs emit bounded canonical JSON under `.cache/verification` by default:

- `.cache/verification/affected.json`;
- `.cache/verification/full.json`;
- `.cache/verification/coverage.json`;
- `.cache/verification/coverage/.coverage` for canonical coverage observations.

The JSON records intent, exact HEAD, selected count, widening reason, lane durations, and return codes.
It is atomically written and capped at 64 KB. Override the JSON destination with
`REPOFORGE_VERIFICATION_REPORT`; override the coverage directory with `REPOFORGE_COVERAGE_DIR`.

## CI topology

Pull requests run static contracts and one affected-test job. Protected branch pushes run **one
canonical** coverage job, a no-coverage compatibility matrix for Python 3.10–3.13 plus macOS, live
activation, package build, and installed-wheel smoke. Coverage-map validation reuses the canonical
`.coverage` file through `make test-map-check-existing`; it does not launch another pytest suite.

The compatibility matrix normally runs a reviewed portability subset. Set the repository variable
`REPOFORGE_CI_FULL_MATRIX=1` to roll it back to full no-coverage behavioral execution in every matrix
cell. This switch restores scope without introducing duplicate coverage jobs.

## Test layers

1. **Unit/config/security:** validation, denied paths, path escape, patch parsing, and symlink/submodule rejection.
2. **Repository discovery/CLI:** multi-language detection, onboarding, doctor, runtime, audit, and error handling.
3. **Local Git integration:** bare remote, clone, worktree, mutation, verification receipt, commit, and push.
4. **Fake provider integration:** deterministic issue/PR reads and writes without operator credentials.
5. **MCP protocol:** the fixed 28-tool `forge_v2` surface, schemas, structured output, and typed errors.
6. **Durable operations:** admission, identity binding, progress, cancellation, recovery, retention, and replay.
7. **Verification control plane:** affected selection, catalog shadow comparison, lane isolation, snapshot preflight, artifacts, and profile policy.
8. **Release effects:** live activation sandbox, package build, installed-wheel behavior, and rollback.

## Live checks still required on the operator machine

Automated tests do not use operator GitHub credentials or an OpenAI tunnel. Before the first real
coding task, select a configured `REPO_ID` and run:

```bash
rf config path
rf doctor
rf repo list
./scripts/inspect-mcp.sh
```

Then run the prompts in `docs/testing/PLUGIN_TEST_CASES.md` against the actual client. Confirm that
write actions request approval and that the final pull request remains draft.
