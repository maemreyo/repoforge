# Root Module Cleanup

RepoForge's package root is intentionally limited to active process-composition concerns:

- `__init__.py` — package metadata;
- `__main__.py` — `python -m repoforge` entry point;
- `bootstrap.py` — the sole production composition root;
- `config.py` — the active configuration model and parser.

All business behavior lives below stable dependency boundaries:

- `domain/` for pure policy and state models;
- `ports/` for abstract boundaries;
- `application/` for use cases and `CodingService`;
- `adapters/` for operating-system, Git, GitHub, persistence, and runtime implementations;
- `interfaces/` for CLI, MCP, and long-lived runtime process entry points.

## Removed modules

The cleanup removes compatibility re-export modules (`audit`, `cli`, `config_delta`, `errors`,
`runner`, `runtime`, `security`, `server`, and `state`), superseded repository onboarding modules
(`discovery`, `onboarding`, `proposal`, and `user_config`), and duplicate root workspace use cases.
Consumers must import the canonical module directly.

`service.py` moves to `application/service.py`. `runtime_worker.py` moves to
`interfaces/runtime/worker.py`. Console scripts point directly to `interfaces.cli.main:main`.

## Compatibility policy

There is no silent fallback to removed modules. A static architecture test rejects any reintroduced
root facade or import. This keeps one implementation per capability and prevents old tests from
forcing obsolete dependency directions back into production code.
