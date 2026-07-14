.PHONY: setup lint typecheck test build check tickets doctor smoke inspector

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
	./scripts/verify-production.sh --allow-dirty

tickets:
	uv run python scripts/validate_ticket_graph.py --next --limit 7

doctor:
	uv run rf doctor

smoke:
	@test -n "$(REPO_ID)" || (echo "Set REPO_ID to a configured repository id" >&2; exit 2)
	uv run rf repo list

inspector:
	./scripts/inspect-mcp.sh
