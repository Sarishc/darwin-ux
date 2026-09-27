# DarwinUX developer commands. Thin wrappers around uv — uv owns dependencies.
# Every target here works today; targets are added only when they do.

BACKEND := cd backend &&
# Pass the root .env to the API only if it exists (uv errors on a missing file).
ENV_FILE := $(if $(wildcard .env),--env-file ../.env,)

# Local PostgreSQL 17 from Homebrew. Called by absolute path because other
# PostgreSQL versions (14/16) and libpq 18 tools are also on this machine.
PG_FORMULA := postgresql@17
PG_BIN = $(shell brew --prefix $(PG_FORMULA))/bin
PG_LOG = $(shell brew --prefix)/var/log/$(PG_FORMULA).log

.PHONY: help sync api worker web web-install web-check test lint format typecheck check \
	db-start db-stop db-status db-logs db-setup \
	migrate migration-status migrate-sql queue-status test-integration

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-17s %s\n", $$1, $$2}'

sync: ## Install locked dependencies into backend/.venv
	$(BACKEND) uv sync

api: ## Run the API (producer) with auto-reload on http://127.0.0.1:8000
	$(BACKEND) uv run $(ENV_FILE) uvicorn darwin.main:app --reload

worker: ## Run the telemetry worker (consumer); Ctrl-C stops it gracefully
	$(BACKEND) uv run $(ENV_FILE) python -m darwin.worker

web-install: ## Install locked frontend dependencies (npm ci)
	cd frontend && npm ci

web: ## Run the Next.js frontend (demo at http://localhost:3000/demo)
	cd frontend && npm run dev

web-check: ## Frontend gate: lint, type check, tests, production build
	cd frontend && npm run lint && npm run typecheck && npm test && npm run build

test: ## Run the unit tests (no database needed)
	$(BACKEND) uv run pytest

lint: ## Lint (Ruff)
	$(BACKEND) uv run ruff check .

format: ## Format code in place (Ruff)
	$(BACKEND) uv run ruff format .

typecheck: ## Type check (mypy, strict)
	$(BACKEND) uv run mypy src tests

check: ## Fast gate, no database: format check, lint, type check, unit tests
	$(BACKEND) uv run ruff format --check .
	$(BACKEND) uv run ruff check .
	$(BACKEND) uv run mypy src tests
	$(BACKEND) uv run pytest

# ---- Local PostgreSQL 17 (Homebrew) ----------------------------------------

db-start: ## Start local PostgreSQL 17 (does not register it to start at login)
	brew services run $(PG_FORMULA)

db-stop: ## Stop local PostgreSQL 17 (data is kept)
	brew services stop $(PG_FORMULA)

db-status: ## Is PostgreSQL 17 up? Which version? Is pgvector available?
	$(PG_BIN)/pg_isready -h localhost -p 5432
	$(PG_BIN)/psql -h localhost -d postgres -Atc "SELECT 'server ' || current_setting('server_version'); SELECT 'pgvector available: ' || coalesce(max(default_version), 'no') FROM pg_available_extensions WHERE name = 'vector';"

db-logs: ## Follow the PostgreSQL 17 server log
	tail -n 50 -f $(PG_LOG)

db-setup: ## One-time, idempotent: create role darwin + darwin_dev/darwin_test databases
	$(PG_BIN)/psql -h localhost -d postgres -f backend/scripts/local_db_setup.sql

# ---- Migrations (Alembic) --------------------------------------------------

migrate: ## Apply all migrations to the database in DARWIN_DATABASE_URL (darwin_dev)
	$(BACKEND) uv run $(ENV_FILE) alembic upgrade head

migration-status: ## Show the current migration revision of that database
	$(BACKEND) uv run $(ENV_FILE) alembic current --verbose

migrate-sql: ## Print the SQL the migrations would run (no database needed)
	$(BACKEND) uv run alembic upgrade head --sql

queue-status: ## Read-only queue counts (never message bodies)
	$(BACKEND) uv run $(ENV_FILE) python -m darwin.queue.status

test-integration: ## Integration tests against local darwin_test (needs PostgreSQL 17)
	$(BACKEND) uv run $(ENV_FILE) pytest -m integration
