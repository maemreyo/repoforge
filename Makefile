# =============================================================================
# RepoForge Makefile

# ── Load .env if present (like JS dotenv) ──────────────────────────────────
ifneq ($(wildcard .env),)
    include .env
    export
endif
#
# Quick start:
#   make                # alias for make start
#   make start          # Build + install + run stable release (one step)
#   make dev-server     # Run dev server from venv (foreground, Ctrl+C)
#   make restart        # Restart already-running server
#   make status         # Check server health
#   make stop           # Stop server
#   make logs           # Tail runtime logs
#   make doctor         # Health check
#
#   make release        # Full release: bump → verify → tag → GitHub Release
#   make install        # Install pre-built wheel as system-wide `rf`
#   make build          # Build source + wheel into dist/
#   make test           # Run tests with coverage
#   make clean          # Remove build artifacts
#
# API key (like JS dotenv):
#   cp .env.example .env
#   # then edit .env and fill in CONTROL_PLANE_API_KEY
#   make start          # .env loaded automatically
#   # or inline:
#   CONTROL_PLANE_API_KEY='sk-proj-...' make start
# =============================================================================

.PHONY: default start dev-server restart status stop logs doctor
.PHONY: setup lint typecheck test build check install release
.PHONY: smoke clean

default: start

# =============================================================================
# START — build + install system-wide + run (the "just run it" command)
# =============================================================================
# Sequence:
#   1. Builds a fresh wheel from current source
#   2. Installs it as system-wide `rf` via uv tool
#   3. Kills any leftover server process (by PID file + process name)
#   4. Starts the new server (foreground or background)
#   5. Shows health summary
#
#   make start                                # foreground (Ctrl+C to stop)
#   make start BG=1                           # background daemon
#   make start WATCH=1                        # daemon + live-tail logs (implies BG=1)
# =============================================================================

define LOG
	@printf "\n\033[36m══> %s\033[0m\n" "$(1)"
endef

start: build
	$(call LOG,Building stable release)
	$(call LOG,Installing system-wide rf)
	uv tool install --reinstall dist/repoforge_mcp-*.whl -q
	$(call LOG,Killing old server if running)
	@-rf runtime stop 2>/dev/null
	@-pkill -f "rf start" 2>/dev/null; sleep 1
	$(call LOG,Starting server (v$$(rf --version)))
	BG_FLAG=$(if $(or $(BG),$(WATCH)),--background,)
	CONTROL_PLANE_API_KEY="$(or $(CONTROL_PLANE_API_KEY),$$CONTROL_PLANE_API_KEY)" \
		rf start $$BG_FLAG
	$(if $(WATCH),,@sleep 3; $(call LOG,Checking health); rf runtime status 2>/dev/null || echo "  (still warming up — run make status)")
	$(if $(WATCH),,@printf "\n\033[32m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m\n")
	$(if $(WATCH),,@printf "\033[32m  ✓ v$$(rf --version)\033[0m\n")
	$(if $(WATCH),,@printf "\033[32m  PID: $$(rf runtime status 2>/dev/null | grep -o '"pid": [0-9]*' | head -1 | grep -o '[0-9]*')\033[0m\n")
	$(if $(WATCH),,@printf "\033[32m  ───────────────────────────────────────────\033[0m\n")
	$(if $(WATCH),,@printf "\033[32m  make status   check health\033[0m\n")
	$(if $(WATCH),,@printf "\033[32m  make watch    live-tail logs\033[0m\n")
	$(if $(WATCH),,@printf "\033[32m  make stop     stop server\033[0m\n")
	$(if $(WATCH),,@printf "\033[32m  make restart  restart (keeps BG)\033[0m\n")
	$(if $(WATCH),,@printf "\033[32m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m\n")
	$(if $(WATCH),@$(MAKE) -s watch 2>/dev/null,)

# =============================================================================
# LOCAL DEV SERVER — runs from project venv (your latest code, no install)
# =============================================================================
# Run in a terminal tab, Ctrl+C to stop. Good for iterating on code.

dev-server:  # Start dev server (foreground, project venv)
	uv run rf start

# =============================================================================
# RUNTIME CONTROLS
# =============================================================================

restart:  # Gracefully restart the running server
	uv run rf runtime restart

status:  # Show server health, PID, generation, tool surface hash
	uv run rf runtime status

stop:  # Stop the running server
	uv run rf runtime stop

logs:  # Show last 20 lines of runtime log
	uv run rf runtime logs --tail 20

watch:  # Live-tail runtime logs (follow mode, Ctrl+C to stop)
	@LOG=$$(uv run rf runtime logs --tail 1 2>/dev/null | grep -o '"path":"[^"]*"' | cut -d'"' -f4); \
	if [ -n "$$LOG" ] && [ -f "$$LOG" ]; then \
		echo "Tailing $$LOG"; \
		tail -f "$$LOG"; \
	else \
		echo "No runtime log file found (server not running?)" >&2; \
		exit 1; \
	fi

doctor:  # Health check on all repos, paths, and dependencies
	uv run rf doctor

smoke:  # Quick test (set REPO_ID=xxx to filter)
	@test -n "$(REPO_ID)" || (echo "Set REPO_ID to a configured repo id" >&2; exit 2)
	uv run rf repo list

# =============================================================================
# DEV ENVIRONMENT
# =============================================================================

setup:  # Install dev dependencies (run once after clone)
	uv sync --extra dev

build:  # Build source + wheel into dist/
	uv build

lint:  # Lint all source files
	uv run ruff check .

typecheck:  # Type-check the full source tree
	uv run mypy src/repoforge

test:  # Run full test suite with coverage
	uv run pytest --cov=repoforge --cov-report=term-missing

check:  # Authoritative full verification gate for dirty development workspaces
	scripts/verify-production.sh --allow-dirty

# =============================================================================
# RELEASE & INSTALL
# =============================================================================

install: build  # Build + install wheel as system-wide rf
	uv tool install --reinstall dist/repoforge_mcp-*.whl -q

release:  # Full release: bump → verify → tag → GitHub Release
	scripts/release.sh $(BUMP)

# =============================================================================
# CLEANUP
# =============================================================================

clean:  # Remove build artifacts
	rm -rf dist/ *.egg-info __pycache__ .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
