# =============================================================================
# RepoForge Makefile
# =============================================================================

.DEFAULT_GOAL := help

# Load a local operator environment without committing it. Values are exported to
# child processes, but the file remains denied by RepoForge path policy.
ifneq ($(wildcard .env),)
include .env
export
endif

.PHONY: default start dev-server restart status stop logs doctor
.PHONY: setup schemas lint typecheck test test-fast test-affected test-groups-check test-map
.PHONY: v2-gates build check install release
.PHONY: smoke clean
.PHONY: help production-check tickets install-hooks inspector clean-dist watch

# Keep this list explicit: config.repoforge.toml profiles and operator docs rely on
# stable target names, and tests reject drift between reviewed profiles and Make.
help:  # Show available commands without changing local or runtime state
	@printf '%s\n' \
	  'RepoForge development and operator commands:' \
	  '' \
	  '  make setup             Sync locked development dependencies' \
	  '  make schemas           Regenerate reviewed Forge v2 contract goldens' \
	  '  make lint              Run Ruff lint' \
	  '  make typecheck         Run strict Mypy' \
	  '  make test              Run tests with coverage' \
	  '  make test-fast         Run tests in parallel (3 workers), no coverage' \
	  '  make test-affected     Run only test groups affected by changed paths (fails closed to full suite)' \
	  '  make test-groups-check Verify every test file is mapped by tests/test-groups.toml' \
	  '  make test-map          Regenerate the coverage map that powers precise test-affected selection' \
	  '  make v2-gates          Run frozen Forge v2 release corpora' \
	  '  make check             Run the full dirty-tree production gate' \
	  '  make production-check  Run the clean-tree production gate' \
	  '  make tickets           Run deterministic ticket-governance tests' \
	  '  make build             Build exactly one wheel and one sdist' \
	  '  make install           Install the freshly built wheel as rf' \
	  '  make start             Build, install, and start in foreground' \
	  '  make start BG=1        Build, install, and start in background' \
	  '  make start WATCH=1     Start in background and follow runtime log' \
	  '  make status|logs|stop  Inspect or stop the managed runtime' \
	  '  make release BUMP=patch|minor|major'

# =============================================================================
# Development and verification
# =============================================================================

setup:  # Synchronize the locked RepoForge development environment
	uv sync --extra dev

schemas:  # Regenerate reviewed Forge v2 schemas and compact release contract
	uv run --extra dev python scripts/generate_tool_schemas.py --write
	uv run --extra dev python scripts/check_release_contracts.py --write

lint:  # Lint all source, tests, and scripts
	uv run --extra dev ruff check .

typecheck:  # Type-check the full source tree
	uv run --extra dev mypy src/repoforge

test:  # Run the complete suite with the repository coverage policy
	uv run --extra dev pytest --cov=repoforge --cov-report=term-missing

test-fast:  # Run the complete suite in parallel without coverage, for fast local iteration.
	# -n 3 was measured against -n 2 and -n 4 on this repo: -n 3 is both the
	# fastest and the only worker count that stayed stable (-n 4 crashed a
	# worker and produced contention-flaky failures in shared-cache tests).
	# Runs test-groups.toml's serial lane (parallel = false groups) alone
	# first, then the rest under -n 3 -- mixing subprocess/git-heavy serial-lane
	# tests into the same xdist run is what produced 60s pytest-timeout
	# failures under load even at -n 3.
	uv run --extra dev python scripts/select_affected_tests.py --full --run

test-affected:  # Run only the tests affected by changed paths (coverage-map precise, group fallback) vs REPOFORGE_TEST_AFFECTED_BASE (default: main); fails closed to the full suite when any changed path is unmapped
	uv run --extra dev python scripts/select_affected_tests.py --run --base "$${REPOFORGE_TEST_AFFECTED_BASE:-main}"

test-groups-check:  # Verify tests/test-groups.toml maps every test file to exactly one group
	uv run --extra dev python scripts/select_affected_tests.py --check-completeness

test-map:  # Regenerate tests/coverage-map.json (source-file -> covering-test-file) for precise test-affected selection
	uv run --extra dev python scripts/build_coverage_map.py

v2-gates:  # Execute frozen mutation, patch, bug-routing, read, and provider-recall corpora
	@set -eu; \
		report_dir=$$(mktemp -d "$${TMPDIR:-/tmp}/repoforge-v2-gates.XXXXXX"); \
		trap 'rm -rf "$$report_dir"' EXIT INT TERM; \
		uv run --extra dev python scripts/run_v2_release_gates.py \
			--executor v2_release_reference:execute_case \
			--report-dir "$$report_dir"

check:  # Authoritative full verification gate for dirty development workspaces
	scripts/verify-production.sh --allow-dirty

production-check:  # Run the authoritative production gate on a clean committed tree
	scripts/verify-production.sh

tickets:  # Validate GitHub-native ticket graph contracts deterministically
	uv run --extra dev pytest -q \
		tests/test_ticket_graph.py \
		tests/test_ticket_readiness.py \
		tests/test_repo_issue_graph_tools.py \
		tests/test_github_ticket_graph_adapter.py

clean-dist:
	rm -rf dist
	mkdir -p dist

build: clean-dist  # Build source and wheel into a clean artifact directory
	uv build --out-dir dist
	@set -eu; \
		set -- $$(find dist -maxdepth 1 -type f -name '*.whl' -print); \
		[ "$$#" -eq 1 ] || { echo "Expected exactly one wheel in dist, found $$#" >&2; exit 1; }; \
		set -- $$(find dist -maxdepth 1 -type f -name '*.tar.gz' -print); \
		[ "$$#" -eq 1 ] || { echo "Expected exactly one sdist in dist, found $$#" >&2; exit 1; }

install: build  # Install the exact freshly built wheel as the system-wide rf tool
	@set -eu; \
		set -- $$(find dist -maxdepth 1 -type f -name '*.whl' -print); \
		[ "$$#" -eq 1 ] || { echo "Expected exactly one wheel in dist, found $$#" >&2; exit 1; }; \
		uv tool install --reinstall "$$1" -q

install-hooks:  # Install the reviewed pre-push contract hook
	@set -eu; \
		hooks_dir="$$(git rev-parse --git-common-dir)/hooks"; \
		echo "Installing pre-push hook..."; \
		mkdir -p "$$hooks_dir"; \
		cp scripts/pre-push.sh "$$hooks_dir/pre-push"; \
		chmod +x "$$hooks_dir/pre-push"; \
		echo "Installed $$hooks_dir/pre-push"

inspector:  # Launch the MCP Inspector workflow
	./scripts/inspect-mcp.sh

# =============================================================================
# Runtime lifecycle
# =============================================================================

start: install  # Build, install, stop the managed old process, and start this release
	@set -eu; \
		printf '\n\033[36m══> Stopping managed runtime\033[0m\n'; \
		rf runtime stop >/dev/null 2>&1 || true; \
		flags=''; \
		if [ -n "$(BG)$(WATCH)" ]; then flags='--background'; fi; \
		printf '\n\033[36m══> Starting %s %s\033[0m\n' "$$(rf --version)" "$$flags"; \
		CONTROL_PLANE_API_KEY="$${CONTROL_PLANE_API_KEY:-}" rf start $$flags; \
		if [ -n "$$flags" ]; then \
			sleep 2; \
			rf runtime status; \
		fi; \
		if [ -n "$(WATCH)" ]; then $(MAKE) -s watch; fi

dev-server:  # Run current source in foreground without installing a wheel
	uv run --extra dev rf start

restart:  # Gracefully restart the managed installed runtime
	rf runtime restart

status:  # Show process, generation, and tool-surface state
	rf runtime status

stop:  # Stop only the process tracked by the runtime state store
	rf runtime stop

logs:  # Show the last 20 managed runtime log lines
	rf runtime logs --tail 20

watch:  # Follow the managed runtime log without scraping pretty-JSON spacing
	@set -eu; \
		log_path=$$(rf runtime logs --tail 1 2>/dev/null | python3 -c 'import json, sys; value=json.load(sys.stdin); print(value.get("path", ""))'); \
		[ -n "$$log_path" ] && [ -f "$$log_path" ] || { echo "No runtime log file found" >&2; exit 1; }; \
		echo "Tailing $$log_path"; \
		tail -f "$$log_path"

doctor:  # Inspect repositories, runtime paths, tools, and configuration state
	uv run --extra dev rf doctor

smoke:  # Run a bounded repository-list smoke check
	@test -n "$(REPO_ID)" || { echo "Set REPO_ID to a configured repository id" >&2; exit 2; }
	uv run --extra dev rf repo list

# =============================================================================
# Release and cleanup
# =============================================================================

release:  # Verify, tag, publish, and create a GitHub release
	@test -n "$(BUMP)" || { echo "Set BUMP=patch, BUMP=minor, or BUMP=major" >&2; exit 2; }
	scripts/release.sh "$(BUMP)"

clean:  # Remove generated development and distribution artifacts
	rm -rf dist/ *.egg-info __pycache__ .ruff_cache .mypy_cache .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
