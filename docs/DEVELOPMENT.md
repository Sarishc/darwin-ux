# DarwinUX — Development Environment

> **Status (Step 4):** FastAPI application with health endpoints, a telemetry ingestion endpoint (`POST /api/v1/telemetry/events`, synchronous and idempotent), settings, structured logging, a PostgreSQL 17 persistence layer (SQLAlchemy + Alembic, one `user_event` table), unit and integration tests. No queue and no AI components yet.

## 1. Prerequisites

| Tool | Required version | Why | Check |
|---|---|---|---|
| macOS or Linux | — | Development is documented for macOS (Apple Silicon); Linux works the same way | `uname -m` |
| Git | 2.40+ | Version control | `git --version` |
| Python | **3.13.x** | Backend, workers, AI subsystems | `python3.13 --version` |
| uv | recent release | Python versions, virtual env, dependencies, lockfile | `uv --version` |
| Node.js | **24.x (LTS)** | Future Next.js frontend | `node --version` |
| npm | ships with Node 24 | Future frontend dependencies | `npm --version` |
| Homebrew | recent | Installs PostgreSQL 17 + pgvector | `brew --version` |
| PostgreSQL | **17.x** (Homebrew `postgresql@17`) + `pgvector` | Local database (native, no containers) | `make db-status` |

**Docker is not used for local development.** PostgreSQL runs natively via Homebrew. Containers may still appear later for *deployment* (the ECS image), which is separate from how you develop locally.

## 2. Python Version: 3.13

**Choice:** Python 3.13, declared in `backend/.python-version` and `requires-python = ">=3.13,<3.14"` in `backend/pyproject.toml`.

**Why 3.13 and not the newest (3.14):**

- The project depends on a wide stack (FastAPI, Pydantic, SQLAlchemy, Alembic, LangChain, LangGraph, OpenTelemetry, AWS SDKs, numerical/ML libraries). Several of these ship compiled extensions. A Python release that has been out for a couple of years has prebuilt wheels across the whole stack; the newest release sometimes lags, especially for ML-adjacent packages.
- 3.13 is well inside its support window (security fixes until 2029), so there is no pressure to move soon.
- The upper bound `<3.14` is deliberate: an accidental jump to a new minor version is exactly the kind of drift that breaks reproducibility. Moving to 3.14 later is a one-line, deliberate change once the stack is confirmed to support it.

**Why not 3.12:** also a sound choice, but 3.13 is already installed locally and fully supported by the ecosystem; there is no benefit to installing an older interpreter.

## 3. Node Version: 24 LTS

**Choice:** Node 24, declared in `.nvmrc` (major version only).

- Node 24 is the current Active LTS line, the one Next.js targets and the longest-supported option today. Node 20 has reached end of life; Node 22 is in maintenance.
- Only the **major** version is pinned. Minor/patch updates within an LTS line are backwards-compatible, and exact frontend dependency versions will be pinned by `package-lock.json` (Step 7).
- When the frontend is created, `package.json` will also declare `"engines": { "node": ">=24 <25" }` so the constraint travels with the frontend itself.

With nvm: `nvm use` in the repo reads `.nvmrc`.

## 4. Python Package Management: uv

**Choice:** [uv](https://docs.astral.sh/uv/) for interpreter management, virtual environments, dependency resolution, and locking.

| | pip + requirements.txt | Poetry | **uv** |
|---|---|---|---|
| Standard `pyproject.toml` metadata | No (separate file) | Partly (historically its own `[tool.poetry]` format) | Yes (PEP 621 + PEP 735 dependency groups) |
| Lockfile with hashes, cross-platform | Only with extra tooling (pip-tools) | Yes | Yes (`uv.lock`) |
| Manages Python versions | No | No | Yes |
| Speed | Slow resolver | Moderate | Very fast |
| Tools needed | pip + venv + pip-tools | Poetry + something to install Python | One binary |

The main reason is **one tool with standard metadata**: `pyproject.toml` stays portable (any PEP 621 tool can read it), and `uv.lock` makes installs reproducible between the laptop, CI, and (later) the deployment image. The same `uv sync --frozen` command will be used in all three.

**Project shape:** `backend/` is an installable package using the **src layout** (`backend/src/darwin/`). The build backend is **`uv_build`** — uv's own, pure-Python backend: it needs no plugins, understands the src layout by default, and avoids adding a second tool (hatchling/setuptools) for a job uv already does. The distribution is named `darwin-ux-backend`; the import name is `darwin` (`[tool.uv.build-backend] module-name`). `uv sync` installs it into `.venv` in editable mode, so code changes take effect without reinstalling.

Why the src layout: tests import `darwin` the way users of the installed package would, instead of accidentally importing whatever happens to be in the current directory.

`uv.lock` is created by the first `uv sync` and **must be committed**.

## 5. Virtual Environments

- One virtual environment, at `backend/.venv/`, created and managed by uv. It is git-ignored.
- You do not need to activate it: `uv run <command>` runs inside it. Activation (`source backend/.venv/bin/activate`) is optional, for convenience.
- Editors: point the Python interpreter at `backend/.venv/bin/python`.
- The venv is disposable. If it breaks: `rm -rf backend/.venv && uv sync`.

## 6. Environment Variables

**Mechanism:**

- `.env.example` (committed): every variable name the project uses, with empty values or safe defaults and a comment. It is the documentation of configuration.
- `.env` (git-ignored): your local values. Created with `cp .env.example .env`.
- In AWS, values come from Secrets Manager / ECS task definitions — never from a `.env` file baked into an image.
- Settings are loaded and validated by `darwin.config.Settings` (pydantic-settings). Invalid values fail at startup, not at first use.
- **Settings read process environment variables only — never a `.env` file directly.** Loading `.env` is the launcher's job: `make api` passes it with `uv run --env-file ../.env` when it exists. This keeps tests independent of whatever is in your local `.env`, and matches AWS, where ECS injects real environment variables.

**Conventions:**

- DarwinUX's own settings are prefixed `DARWIN_` to avoid collisions.
- Standard names are kept where libraries already read them (`AWS_PROFILE`, `AWS_REGION`, `OTEL_*`).
- Categories in the template: application, database, LLM, embeddings, Jev, Muse, AWS, observability.
- **Jev and Muse connection variables are deliberately absent.** Their official access methods and credential names are unverified (OPEN_QUESTIONS.md B1/B2). The template only contains DarwinUX-owned *adapter selectors* (`DARWIN_DECIDER_ADAPTER`, `DARWIN_MUTATION_GENERATOR_ADAPTER`) whose values are DarwinUX's own baselines.
- Never put real keys in `.env.example`, in code, in tests, in docs, or in Terraform. For AWS, prefer SSO/named profiles over access keys.

## 7. Quality Tools

Each tool answers a different question:

| Job | Tool | Question it answers | Changes code? |
|---|---|---|---|
| **Formatter** | `ruff format` | "Is the code laid out consistently?" (whitespace, quotes, wrapping) | Yes, automatically |
| **Linter** | `ruff check` | "Does the code contain likely bugs or bad patterns?" (unused imports, undefined names, mutable defaults, unsorted imports) | Only safe auto-fixes with `--fix` |
| **Type checker** | `mypy` | "Are values used consistently with their declared types?" — without running the code | No |
| **Test runner** | `pytest` | "Does the code *behave* correctly when run?" | No |

A formatter makes style a non-topic; a linter catches mistakes a formatter can't see; a type checker catches mistakes a linter can't see (wrong types across function boundaries); only tests check behaviour. None replaces another.

**Why Ruff for two jobs:** it replaces Black (formatting), isort (imports), and Flake8 + plugins (linting) with one fast tool and one config block.

**Why mypy `strict`:** strictness is cheap to adopt on an empty codebase and very expensive to retrofit. Pydantic's mypy plugin will be enabled when Pydantic is added.

**Status:** installed via the `dev` dependency group and run by `make check`. mypy uses the Pydantic plugin so model/settings constructors are type-checked.

## 8. Local Services: Native PostgreSQL 17

DarwinUX's only local service today is **PostgreSQL 17, installed with Homebrew** and run as a background service — no Docker, Compose, or VM.

| Service | Local | AWS equivalent |
|---|---|---|
| PostgreSQL 17 (+ pgvector files, extension not enabled) | Homebrew `postgresql@17` on `localhost:5432` | RDS for PostgreSQL 17 |
| Queue (later) | Decided when the telemetry pipeline needs one (OPEN_QUESTIONS.md N4) | SQS |
| Trace viewer (later) | Decided when OpenTelemetry is added | CloudWatch / X-Ray |

Principles:

- The application runs on the host with `uv run` (fast reloads, normal debugger). PostgreSQL runs beside it as a native service.
- SQLAlchemy and Alembic only see a connection URL, so the same code runs against Homebrew PostgreSQL locally and RDS in production.
- `make db-start` uses `brew services run`, which starts PostgreSQL **without** registering it to start at login.

**This Mac also has PostgreSQL 14 and 16 installed (stopped).** All three default to port 5432, and `/opt/homebrew/bin/postgres` is the **14** server while `psql`/`pg_ctl`/`initdb` come from libpq **18**. Therefore:

- Keep 14 and 16 stopped. Only one server can own port 5432.
- Never rely on bare `postgres`/`pg_ctl` commands for DarwinUX. The Makefile calls PostgreSQL 17 by absolute path (`$(brew --prefix postgresql@17)/bin/...`).
- `make db-status`, `make db-setup`, and an integration test all verify the server is **17**, so a wrong server on 5432 fails loudly.

## 9. Commands

A small root `Makefile` wraps the real commands. It only delegates to uv — uv still owns dependencies — and every target works today. Run `make help` to list them.

| Command | What it runs (in `backend/`) | Use it to |
|---|---|---|
| `make sync` | `uv sync` | Install/refresh dependencies from `uv.lock` |
| `make api` | `uv run [--env-file ../.env] uvicorn darwin.main:app --reload` | Start the API on http://127.0.0.1:8000 with auto-reload |
| `make test` | `uv run pytest` | Run the unit tests (no database) |
| `make lint` | `uv run ruff check .` | Lint |
| `make format` | `uv run ruff format .` | Format in place |
| `make typecheck` | `uv run mypy src tests` | Type check |
| `make check` | format check + lint + typecheck + unit tests | Fast gate before every commit; **never needs PostgreSQL** |
| `make db-start` / `make db-stop` | `brew services run` / `stop postgresql@17` | Start / stop local PostgreSQL 17 (data is kept) |
| `make db-status` | `pg_isready` + server version + pgvector availability | Check the right server is up |
| `make db-logs` | `tail -f` the PostgreSQL 17 log | Debug the server |
| `make db-setup` | `psql -f backend/scripts/local_db_setup.sql` | One-time, idempotent: role `darwin`, databases `darwin_dev`, `darwin_test` |
| `make migrate` | `alembic upgrade head` | Apply migrations to `darwin_dev` |
| `make migration-status` | `alembic current --verbose` | Which revision is `darwin_dev` at? |
| `make migrate-sql` | `alembic upgrade head --sql` | Print the DDL without a database |
| `make test-integration` | `pytest -m integration` | Integration tests against `darwin_test` (needs PostgreSQL 17) |

Without make, run the same commands from `backend/`, e.g.:

```bash
uv run uvicorn darwin.main:app --reload
```

Adding a dependency: `uv add <package>` (runtime) or `uv add --dev <package>` (tooling), from `backend/`. Both update `pyproject.toml` **and** `uv.lock`; commit both.

With the API running:

- http://127.0.0.1:8000/api/v1/health/live
- http://127.0.0.1:8000/api/v1/health/ready
- http://127.0.0.1:8000/docs — interactive OpenAPI docs generated by FastAPI from the response models

Other checks:

```bash
nvm use                      # switch to Node 24 per .nvmrc
cp .env.example .env         # create local env file (git-ignored)
```

## Manual Setup (one-time)

System-level installs are left to you; none were performed automatically.

```bash
brew install uv
```

PostgreSQL 17 and pgvector (Homebrew; `postgresql@17` is keg-only, so it does not replace the other versions' commands):

```bash
brew install postgresql@17 pgvector
```

Then, from the repository root:

```bash
make db-start
```

```bash
make db-setup
```

```bash
make migrate
```

Optionally align nvm with the project (your nvm default is Node 20, which is end-of-life):

```bash
nvm install 24
```

```bash
nvm alias default 24
```

## 10. Intentionally Not Installed / Configured Yet

| Item | Arrives | Why not now |
|---|---|---|
| Docker / Compose for local development | Not planned | Native Homebrew PostgreSQL is simpler for one developer |
| Queue, trace viewer | Later steps | Nothing uses them yet |
| `CREATE EXTENSION vector`, vector columns | RAG step | pgvector is installed but not enabled until retrieval exists |
| Dockerfile for the app | Later step | Running on the host is faster to iterate on |
| Next.js, `package.json`, `node_modules` | Step 2 (demo app) / 7 | Frontend not started |
| GitHub Actions | Later step | `make check` is the same gate, run locally |
| Terraform, AWS | Step 9 | Local-first |
| OpenTelemetry SDK | Later step | Structured logs are enough for one process |
| LangChain, LangGraph, LLM/embedding SDKs, Jev, Muse | Later steps | No AI component exists yet |
| pre-commit hooks | Future option | `make check` (and later CI) enforces the same checks without another tool; revisit if checks are forgotten in practice |
| devcontainers, Nix, Bazel, monorepo frameworks | Not planned | Overhead with no benefit for one developer and two apps |
| Large IDE configs (`.vscode/`, `.idea/`) | Not planned | `.editorconfig` + `pyproject.toml` are tool-neutral |

## 11. The Backend Application

### Package layout

```
backend/src/darwin/
├── __init__.py        # __version__, read from the installed package metadata
├── main.py            # create_app() + lifespan; `app` for Uvicorn
├── config.py          # Settings (pydantic-settings)
├── logging_config.py  # JSON log formatter + configure_logging()
├── api/
│   ├── router.py      # the /api/v1 router; includes every v1 router
│   ├── health.py      # /health/live, /health/ready + their response models
│   ├── telemetry.py   # POST /telemetry/events — parse, delegate, respond
│   └── errors.py      # 422 responses without echoed input values
├── telemetry/
│   ├── schemas.py     # TelemetryEvent (request), IngestionResult (response)
│   └── service.py     # ingest_event(): the transaction + idempotent insert
└── db/
    ├── base.py        # DeclarativeBase + constraint naming convention
    ├── engine.py      # create_db_engine(), database_is_available() (SELECT 1)
    ├── session.py     # session factory + request-scoped DbSession dependency
    ├── safety.py      # guard for destructive operations (tests)
    └── models/
        └── user_event.py
```

Outside the package: `backend/alembic/` (migrations), `backend/alembic.ini`, `backend/scripts/local_db_setup.sql`.

Modules are added when there is code for them — no empty `rag/`, `agents/`, or `providers/` directories. There is also no `core/` package: generic names like "core" or "utils" become dumping grounds with no clear place in the dependency rule.

**Dependency direction:** `main` → `api`, `db`, `config`, `logging_config`; `api` → `telemetry`, `db`; `telemetry` → `db`; `logging_config` → `config`. Nothing below `api` imports FastAPI routing, and nothing imports `main`. Nothing imports `main`. This is the start of the layering in ARCHITECTURE.md: the entrypoint wires pieces together; the pieces don't know about the entrypoint.

### How a request becomes a response

```
curl GET /api/v1/health/live
  → Uvicorn (ASGI server): owns the socket, parses HTTP, calls app(scope, receive, send)
  → FastAPI app (ASGI application): matches path + method against registered routes
  → api_v1_router (prefix /api/v1) → health.router (prefix /health) → live()
  → handler returns LivenessResponse (a Pydantic model)
  → FastAPI validates it against the declared return type and serialises it to JSON
  → Uvicorn writes: HTTP/1.1 200 OK, content-type: application/json, {"status":"alive"}
```

**ASGI** is the interface between a Python web server and a Python web application: the server calls the application with a description of the request and two async callables to receive the body and send the response. Uvicorn is the server; FastAPI (built on Starlette) is the application framework. They can be swapped independently.

**A router** is a group of routes with a shared prefix and tags. Routers keep each area of the API in its own module and let `/api/v1` be applied in one place.

**Response models** are declared as the handler's return type. FastAPI uses them to validate output (a typo in a field fails loudly instead of reaching clients), to serialise JSON, and to generate the OpenAPI schema at `/docs`.

### Application factory and lifespan

`create_app(settings)` builds a new, independent app each time it is called. Tests call it with explicit `Settings`; `main.py` also creates one module-level `app` so `uvicorn darwin.main:app` can find it.

Importing the module does not open connections. Anything that talks to the outside world (database pools, HTTP clients) will be opened in the **lifespan** function, which runs once at server startup and once at shutdown.

### Logging

Standard-library `logging` with a small JSON formatter: one JSON object per line with `timestamp`, `level`, `logger`, `message`, and optional structured `context`:

```json
{"timestamp": "2026-09-26T22:59:24.146023+00:00", "level": "INFO", "logger": "darwin.main", "message": "application started", "context": {"app_name": "DarwinUX", "version": "0.0.0", "env": "local", "log_level": "INFO"}}
```

Log with `logger.info("message", extra={"context": {...}})`. Only the `darwin.*` logger tree uses this format; Uvicorn's own server/access lines keep their default format. Never log secrets — the startup line lists only non-sensitive settings, explicitly.

### Liveness vs. readiness

| | `GET /api/v1/health/live` | `GET /api/v1/health/ready` |
|---|---|---|
| Question | Is the process alive and able to answer HTTP? | Can this instance do its real work right now? |
| Checks | Nothing — if the handler runs, the answer is yes | Startup completed, and PostgreSQL answers `SELECT 1` |
| Failure means | The process is stuck → **restart** it | Not ready yet or a dependency is down → **stop sending traffic**, don't restart |
| Response | `200 {"status": "alive"}` | `200 {"status": "ready", "checks": [...]}` or `503 {"status": "not_ready", "checks": [...]}` |

Why they must differ: if liveness also checked the database, a database outage would make the orchestrator restart every healthy API instance in a loop — making the outage worse. Readiness failing only removes instances from the load balancer until the dependency recovers.

Readiness has two checks. `startup_complete` is true between lifespan startup and shutdown. `database` runs `SELECT 1` through the engine's pool (2-second connect timeout, so a stopped server gives a fast `503`, not a hang). It never runs migrations or real queries. On failure its detail is just `"unavailable"`, because driver errors can contain hostnames and usernames. The `database` check was added without changing the response shape — exactly what the Step 2 contract was designed for.

| PostgreSQL | `/live` | `/ready` |
|---|---|---|
| running | 200 | 200 |
| stopped | 200 | 503 (`database: false`) |

### Tests

| File | Covers |
|---|---|
| `tests/test_app.py` | App factory, metadata, independent instances, `/api/v1` versioning |
| `tests/test_health.py` | Liveness/readiness responses, 503 when the DB is unreachable, before startup and after shutdown |
| `tests/test_db_unit.py` | Timezone-aware timestamps, per-request sessions closed, test-database guard |
| `tests/test_telemetry_schemas.py` | The event contract: UUIDs, timezones, event_type rules, payload shape/size/depth, forbidden fields |
| `tests/test_telemetry_api.py` | 422 before persistence, no echoed input, deep payloads, DB failure → 500 without leaking payload |
| `tests/integration/` | Real PostgreSQL behaviour — see "12. Database" and "13. Telemetry Ingestion" |
| `tests/test_config.py` | Defaults, `DARWIN_` prefix, case-insensitive log level, fail-fast validation, ignoring future variables |
| `tests/test_logging.py` | JSON output, structured context, robustness, idempotent configuration |

API tests use FastAPI's `TestClient`, which calls the ASGI app in-process — no server or network needed. Using it as a context manager (`with TestClient(app)`) runs the lifespan, exactly like Uvicorn.

---

## 12. Database

### The layers

```
FastAPI handler ──> Session (unit of work) ──> Engine (pool of connections) ──> psycopg 3 ──> PostgreSQL 17
                    darwin/db/session.py       darwin/db/engine.py               driver          server
```

- **SQLAlchemy is not PostgreSQL.** SQLAlchemy is a Python library that builds SQL and maps rows to objects. psycopg 3 is the driver that speaks PostgreSQL's wire protocol. PostgreSQL is the server that actually stores data.
- **Engine** (`create_db_engine`): created **once** at app startup (lifespan), disposed of at shutdown. It owns the **connection pool**: opening a PostgreSQL connection is slow (network + authentication), so the pool keeps a few open and lends them out. Creating an engine does not connect, which is why the API starts even when PostgreSQL is down.
- **Session** (`get_session` → `DbSession`): one per request, always closed afterwards. It tracks objects you add/load and turns them into SQL when flushed. Never share one Session globally: it is not thread-safe and would mix different requests' work.
- **Transaction**: an all-or-nothing boundary. Code that changes data owns it explicitly with `with session.begin():` — commit on success, rollback on exception. Nothing is committed implicitly.

**Synchronous SQLAlchemy** (not async): FastAPI runs sync handlers in a thread pool, the workload is small, future workers are ordinary scripts, and sync code is easier to read and debug. Switching later is contained in `darwin/db/`.

### Configuration

- One setting: `DARWIN_DATABASE_URL`, validated as a PostgreSQL URL that must use the `postgresql+psycopg://` scheme.
- Default (no `.env`): `postgresql+psycopg://darwin@localhost:5432/darwin_dev` — local, no password in code.
- `.env.example` contains the LOCAL ONLY values created by `make db-setup` (`darwin` / `darwin_local_only`). They are not secrets. Homebrew trusts local connections, so locally the password is not actually checked; it is there so URLs look like production.
- An empty `DARWIN_DATABASE_URL=` counts as unset.
- Production: RDS, credentials from Secrets Manager, TLS required. Never this file.

### Local databases

| Database | Used by | Written by |
|---|---|---|
| `darwin_dev` | `make api`, `make migrate` | You, while developing |
| `darwin_test` | `make test-integration` only | Tests (transactions are rolled back; one test downgrades and re-upgrades the schema) |

Both are owned by role `darwin`, which cannot create roles or databases and is not a superuser.

### Migrations (Alembic)

```
Python model (darwin/db/models/user_event.py)
      │  describes the table
      ▼
Migration (alembic/versions/0001_create_user_event.py)
      │  explicit, reviewed, versioned change
      ▼
SQL DDL (CREATE TABLE user_event ...)      ← see it with `make migrate-sql`
      │
      ▼
PostgreSQL schema (+ alembic_version table recording "0001")
```

- **Why migrations:** the database outlives every deployment. `create_all()` can only create missing tables — it cannot rename a column, add a constraint to existing data, or undo anything. Migrations are ordered, reviewable, reversible steps, and the `alembic_version` table records which ones a database has.
- **The app never creates tables.** `Base.metadata.create_all()` is not called anywhere; startup only creates an engine. An integration test proves startup leaves an empty database empty.
- `alembic/env.py` reads the URL from `darwin.config.Settings`, the same setting the app uses. `alembic.ini` contains no URL.
- **upgrade** applies migrations forward (`alembic upgrade head`); **downgrade** runs their `downgrade()` functions backwards (`alembic downgrade -1`, or `base` for everything). Downgrades that drop tables destroy data — use them on local databases only.

Creating a future migration (from `backend/`, with PostgreSQL running and `darwin_dev` at head):

```bash
uv run --env-file ../.env alembic revision --autogenerate -m "describe the change"
```

Then **read the generated file** (autogenerate misses some changes and sometimes guesses wrong), run `make migrate`, and run `make test-integration` — `test_migration_matches_the_orm_models` fails if the model and migrations disagree.

### Unit vs. integration tests

| | Unit (`make test`, `make check`) | Integration (`make test-integration`) |
|---|---|---|
| Needs PostgreSQL | No — the unit settings point at a closed port | Yes — local `darwin_test` |
| Examples | settings validation, readiness `503` when the DB is down, sessions closed per request, naive timestamps rejected | insert/query, uniqueness, idempotent `ON CONFLICT DO NOTHING`, check constraints, timestamps, schema shape, migrations, readiness `200` |
| Speed | Well under a second | A few seconds |

**Safety guard:** integration tests read `DARWIN_TEST_DATABASE_URL` (default `…@localhost:5432/darwin_test`), never `DARWIN_DATABASE_URL`, and `darwin.db.safety.require_local_test_database` refuses any host other than localhost and any database name not ending in `_test` — before a connection is attempted.

### Resetting the local database (LOCAL DEVELOPMENT ONLY)

Destructive: this deletes **all** data in `darwin_dev`.

```bash
psql -h localhost -d postgres -c "DROP DATABASE darwin_dev WITH (FORCE);"
```

```bash
make db-setup
```

```bash
make migrate
```

A lighter option that keeps the database but drops DarwinUX's tables: `cd backend && uv run --env-file ../.env alembic downgrade base && uv run --env-file ../.env alembic upgrade head`.

Deleting the PostgreSQL 17 data directory (`$(brew --prefix)/var/postgresql@17`) would destroy every database on that server, for every project — never do this to reset DarwinUX.

### pgvector (later)

`pgvector` is installed for PostgreSQL 17 but **not enabled**: no `CREATE EXTENSION`, no vector columns. `make db-status` shows it is available. The RAG step will enable it in a migration.

---

## 13. Telemetry Ingestion

### Endpoint

`POST /api/v1/telemetry/events` — one event per request. Documented at http://127.0.0.1:8000/docs.

The path is namespaced under `/telemetry` because this is a distinct ingestion surface (SDK-facing, high volume, later its own limits and queue), not a generic "events" resource.

### Request

```json
{
  "event_id": "5b2f0c8e-4c1a-4a5e-9d6f-0a1b2c3d4e5f",
  "event_type": "button_click",
  "session_id": "9c8b7a6f-5e4d-4c3b-8a29-1f0e9d8c7b6a",
  "occurred_at": "2026-09-26T17:00:00Z",
  "payload": {"component": "signup_submit"}
}
```

| Field | Rule | Why |
|---|---|---|
| `event_id` | UUID, required | Client-generated **idempotency key** (see below) |
| `event_type` | snake_case (`^[a-z][a-z0-9_]*$`), 1–64 chars | Matches the column; consistent names. Not lower-cased for you — `ButtonClick` is rejected, so one event never ends up under two spellings |
| `session_id` | UUID, required | Random, anonymous per-visit id. Not a user id |
| `occurred_at` | ISO 8601 **with timezone** | A time without a zone is ambiguous; rejected |
| `payload` | JSON **object**, optional (default `{}`), ≤ 8 KiB compact JSON, ≤ 5 levels deep | Shape and size are bounded; content is still untrusted |

Anything else — including `id`, `received_at`, `user_id`, `email`, `ip_address`, `user_agent` — is rejected with **422** (`extra="forbid"`). The server owns `id` and `received_at` (PostgreSQL `now()`).

### Responses

| Situation | HTTP | Body |
|---|---|---|
| New event | **202** | `{"event_id": "5b2f0c8e-…", "status": "accepted"}` |
| Same `event_id` again | **202** | `{"event_id": "5b2f0c8e-…", "status": "duplicate"}` |
| Invalid event | **422** | FastAPI's error list, **without** the rejected values echoed back |
| Database unavailable / unexpected error | **500** | `Internal Server Error` (nothing stored; safe to retry) |

**Why 202 for both:** a duplicate is not an error — it means idempotency worked. One success code keeps clients simple ("2xx = done, stop retrying"). `202 Accepted` rather than `201 Created` because the contract promises *acceptance*, not that processing has finished; that stays true when a queue is added. Clients must not branch on `status` — it is informational, and a future queued pipeline may report `accepted` for a duplicate.

No database id is returned: clients have no use for it, and it would couple them to storage.

### Idempotency

- The **client** generates `event_id` once, when the event happens, and reuses it on every retry. Only the client can do this: if the server generated the id, a retry after a lost response would look like a brand-new event.
- The service runs one statement in one transaction: `INSERT … ON CONFLICT (event_id) DO NOTHING RETURNING id`. A returned id means `accepted`; no row means `duplicate`.
- **Race-safe:** there is no "SELECT, then INSERT". If two deliveries of one event arrive at the same moment, PostgreSQL makes the second wait on the first's row and then skips it. `UNIQUE(event_id)` is the final guarantee — an integration test holds one transaction open and proves the second delivery waits and becomes a duplicate.
- **First delivery wins.** A duplicate with a different payload does not overwrite the stored event.

### Privacy rules

- Nothing in the contract identifies a person. There are no fields for email, name, account id, IP address, user agent, or device fingerprint, and unknown fields are rejected.
- `payload` is free-form and **untrusted**: a client could still put personal data in it by mistake. Step 4 bounds its shape and size; it does not try to detect PII. Stricter per-event-type schemas can come later.
- Payloads are **never logged**, never echoed in 422 errors, and never included in database error messages (the engine uses `hide_parameters=True`).
- `session_id` is not logged either.

### Logging

One line per ingested event, at INFO:

```json
{"level": "INFO", "logger": "darwin.telemetry.service", "message": "telemetry event ingested", "context": {"event_id": "5b2f0c8e-…", "event_type": "button_click", "status": "accepted"}}
```

Logged: `event_id`, `event_type`, `status`. Not logged: `payload`, `session_id`, `occurred_at`.

### Where a queue will attach

`darwin.telemetry.service.ingest_event` is the boundary. The route calls it and knows nothing about SQL. When SQS arrives, `ingest_event` publishes the validated event instead of inserting it, and a worker runs the same idempotent insert. The URL, request schema, status code, and response shape do not change. See DATA_PIPELINES.md, "Current Implementation vs. Target Design".

### Trying it

```bash
make api
```

In another terminal (fresh UUIDs each time):

```bash
curl -s -i -X POST http://127.0.0.1:8000/api/v1/telemetry/events -H 'content-type: application/json' -d "{\"event_id\": \"$(uuidgen)\", \"event_type\": \"button_click\", \"session_id\": \"$(uuidgen)\", \"occurred_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"payload\": {\"component\": \"signup_submit\"}}"
```

Send the exact same body twice to see `accepted` then `duplicate`. Integration tests for ingestion: `make test-integration` (needs PostgreSQL 17).

### Known limits (Step 4)

- One event per request; no batching.
- The payload limit is enforced after the body is parsed, so a very large request body is still read into memory before it is rejected. A request-body cap belongs at the edge (load balancer / server config) when the service is exposed publicly.
- No authentication or rate limiting on the endpoint yet.
- No bound on how far in the past or future `occurred_at` may be.

---

## What You Should Understand Before Step 2

1. **Reproducibility comes from lockfiles, not version ranges.** `pyproject.toml` says what is *allowed*; `uv.lock` records exactly what was *installed*. Commit the lockfile and install with `--frozen` in CI and Docker.
2. **The virtual environment is disposable; the lockfile is not.** You can delete `.venv` any time; `uv sync` recreates it identically.
3. **Formatter, linter, type checker, and tests catch different classes of problems.** Passing one says nothing about the others.
4. **`.env.example` is documentation; `.env` is local state.** Secrets live only in `.env` locally and in Secrets Manager in AWS — never in git.
5. **Local services run natively beside the application.** The app runs with `uv run`; PostgreSQL 17 runs as a Homebrew service. Both are reachable on localhost, which keeps the edit–run–debug loop fast.
