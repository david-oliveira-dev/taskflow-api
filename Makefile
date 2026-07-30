.PHONY: help install up down check lint type test test-unit run clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Sync dependencies
	uv sync

up:  ## Start Postgres and Redis
	docker compose up -d

down:  ## Stop dependencies (keeps the data volume)
	docker compose down

check: lint type test  ## Everything CI runs

lint:  ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

type:  ## mypy, strict
	uv run mypy

test:  ## Full suite (needs Docker for the integration tests)
	uv run pytest

test-unit:  ## Unit tests only — offline, no Docker
	uv run pytest -m "not integration"

run:  ## Run the API with reload
	uv run uvicorn taskflow_api.main:create_app --factory --reload

clean:  ## Remove caches and coverage output
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov coverage.xml .coverage
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
