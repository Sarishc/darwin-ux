# DarwinUX developer commands. Thin wrappers around uv — uv owns dependencies.
# Every target here works today; targets are added only when they do.

BACKEND := cd backend &&
# Pass the root .env to the API only if it exists (uv errors on a missing file).
ENV_FILE := $(if $(wildcard .env),--env-file ../.env,)

.PHONY: help sync api test lint format typecheck check

help: ## List targets
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'

sync: ## Install locked dependencies into backend/.venv
	$(BACKEND) uv sync

api: ## Run the API with auto-reload on http://127.0.0.1:8000
	$(BACKEND) uv run $(ENV_FILE) uvicorn darwin.main:app --reload

test: ## Run the test suite
	$(BACKEND) uv run pytest

lint: ## Lint (Ruff)
	$(BACKEND) uv run ruff check .

format: ## Format code in place (Ruff)
	$(BACKEND) uv run ruff format .

typecheck: ## Type check (mypy, strict)
	$(BACKEND) uv run mypy src tests

check: ## Everything CI will run: format check, lint, type check, tests
	$(BACKEND) uv run ruff format --check .
	$(BACKEND) uv run ruff check .
	$(BACKEND) uv run mypy src tests
	$(BACKEND) uv run pytest
