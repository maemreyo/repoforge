# RepoForge program completion matrix

This matrix maps the production architecture plan to executable evidence. The required workflow runs
the complete suite; individual files below identify the primary evidence rather than an exclusive test.

| Requirement area | Primary executable evidence |
| --- | --- |
| Semantic configuration deltas and approvals | `test_domain_config_generation_delta.py`, `test_phases1_2_foundation.py` |
| Deterministic proposal decisions | `test_phase3_repository_proposals.py` |
| Runtime state, drain, restart, rollback | `test_phase4_runtime_control.py` |
| Application boundaries and failure cleanup | `test_phase5_architecture.py`, `test_phase5_failure_harness.py` |
| Full workspace/Git/GitHub lifecycle | `test_phases1_5_full_lifecycle.py`, `test_phases1_4_real_git_integration.py` |
| Structured errors, idempotency, metrics, diagnostics | `test_phase6_operational_hardening.py` |
| Atomic hot reload and removed repositories | `test_phase7_atomic_hot_reload.py` |
| Darwin peer credentials and lifecycle regressions | `test_phase7_regressions.py`, `test_unix_socket_path_portability.py` |
| Fast child exit and complete log drainage | `test_phase7_regressions.py`, `test_phase6_operational_hardening.py` |
| MCP schema and protocol compatibility | `test_mcp_contract.py` where present, `test_phase5_mcp_contract.py`, frozen release contract |
| Minimal/legacy config compatibility | `test_phase8_program_completion.py`, `tests/fixtures/config/` |
| Clean build and wheel installation | `scripts/verify-wheel-install.sh`, `scripts/verify-wheel-e2e.py`, `production-gate` package job |
| Python 3.10–3.13 and macOS | `.github/workflows/production-gate.yml` |
| Documentation/implementation drift | `scripts/check_release_contracts.py`, `release-contract-v1.json` |

## Leak and fail-closed assertions

The suite includes explicit cleanup and corruption tests for worktrees, branches, config temporary
files, registry persistence, locks, runtime state, child processes, idempotency receipts, active
containers, and Unix sockets. Secrets are tested at audit, diagnostics, MCP, CLI, subprocess, and live
runtime-log boundaries.
