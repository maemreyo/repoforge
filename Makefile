.PHONY: setup lint typecheck test build check production-check doctor smoke inspector install-hooks

setup:
	uv sync --extra dev

lint:
	uv run ruff check .

typecheck:
	uv run mypy src/repoforge

test:
	uv run pytest --cov=repoforge --cov-report=term-missing

build:
	uv build

check:
	sh ./scripts/verify-agent.sh

production-check:
	./scripts/verify-production.sh --allow-dirty

doctor:
	uv run rf doctor

smoke:
	@test -n "$(REPO_ID)" || (echo "Set REPO_ID to a configured repository id" >&2; exit 2)
	uv run rf repo list

inspector:
	./scripts/inspect-mcp.sh

install-hooks:
	@echo "Installing pre-push hook..."
	@cp scripts/pre-push.sh .git/hooks/pre-push
	@chmod +x .git/hooks/pre-push
	@echo "Installed .git/hooks/pre-push"
