#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if command -v uv >/dev/null 2>&1; then
  uv sync --extra dev
  uv run ruff check .
  uv run mypy src/repoforge
  uv run pytest --cov=repoforge --cov-report=term-missing
  uv build
else
  .venv/bin/ruff check .
  .venv/bin/mypy src/repoforge
  .venv/bin/pytest --cov=repoforge --cov-report=term-missing
  .venv/bin/python -m build
fi
