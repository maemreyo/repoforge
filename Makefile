# =============================================================================
# RepoForge Makefile
#
#   make setup          Install dev dependencies
#   make lint           Run ruff linter
#   make typecheck      Run mypy type checking
#   make test           Run tests with coverage
#   make build          Build source + wheel dist/
#
#   make dev-server     Start local dev server (foreground, Ctrl+C to stop)
#   make restart        Restart running server (keeps background mode)
#   make status         Check server health
#   make stop           Stop server
#   make logs           Tail runtime logs
#   make doctor         Run rf doctor health check
#
#   make release        Full release: bump → verify → tag → GitHub Release
#   make install        Install built wheel as system-wide `rf`
#   make clean          Remove build artifacts
# =============================================================================

.PHONY: setup lint typecheck test build
.PHONY: dev-server restart status stop logs doctor smoke
.PHONY: release install clean

# --- Dev environment ---------------------------------------------------------

setup:  # Install dev dependencies (run once after clone)
	uv sync --extra dev

lint:  # Lint all source and test files
	uv run ruff check .

typecheck:  # Type-check the full source tree
	uv run mypy src/repoforge

test:  # Run full test suite with coverage
	uv run pytest --cov=repoforge --cov-report=term-missing

build:  # Build source distribution and wheel into dist/
	uv build

# --- Local server (development) ----------------------------------------------
# These use `uv run rf` → project venv → your latest local code.
# Run `make dev-server` in a terminal tab to see live logs.

dev-server:  # Start dev server in foreground (Ctrl+C to stop; auto-reloads on restart)
	uv run rf start

restart:  # Gracefully restart the running server (new code, same background mode)
	uv run rf runtime restart

status:  # Show server health, PID, generation, tool surface hash
	uv run rf runtime status

stop:  # Stop the running server gracefully
	uv run rf runtime stop

logs:  # Tail the managed runtime log
	uv run rf runtime logs

doctor:  # Run health checks on all repos, paths, and dependencies
	uv run rf doctor

smoke:  # Quick smoke test: list repos (set REPO_ID=xxx to filter)
	@test -n "$(REPO_ID)" || (echo "Set REPO_ID to a configured repository id" >&2; exit 2)
	uv run rf repo list

# --- Release ------------------------------------------------------------------
# Bump version, run production gate, tag, push, create GitHub Release.
# Usage:  make release BUMP=patch   (default: minor)
#         make release BUMP=minor
#         make release BUMP=major

release:  # Full release pipeline: bump → verify → tag → GitHub Release
	scripts/release.sh $(BUMP)

# --- Install stable release ---------------------------------------------------
# Installs the freshly-built wheel as the system-wide `rf` command.
# After this, plain `rf` runs the released version instead of the dev venv.
# The venv `uv run rf` still stays on your latest code — use whichever you need.

install:  # Install built wheel as system-wide rf via uv tool
	uv tool install --reinstall dist/repoforge_mcp-*.whl

# --- Cleanup ------------------------------------------------------------------

clean:  # Remove build artifacts
	rm -rf dist/ *.egg-info __pycache__ .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
