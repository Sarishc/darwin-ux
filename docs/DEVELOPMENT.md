# DarwinUX — Development Environment

> **Status (Step 2):** a minimal FastAPI application with health endpoints, settings, structured logging, and tests. No database, no local services, no AI components yet.

## 1. Prerequisites

| Tool | Required version | Why | Check |
|---|---|---|---|
| macOS or Linux | — | Development is documented for macOS (Apple Silicon); Linux works the same way | `uname -m` |
| Git | 2.40+ | Version control | `git --version` |
| Python | **3.13.x** | Backend, workers, AI subsystems | `python3.13 --version` |
| uv | recent release | Python versions, virtual env, dependencies, lockfile | `uv --version` |
| Node.js | **24.x (LTS)** | Future Next.js frontend | `node --version` |
| npm | ships with Node 24 | Future frontend dependencies | `npm --version` |
| Docker engine + Compose v2 | recent | Future local Postgres / queue emulator | `docker compose version` |

Docker is **not needed until Step 2**. On macOS any Docker engine works (Docker Desktop, Colima, OrbStack); the project assumes only `docker` and `docker compose`.

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

The main reason is **one tool with standard metadata**: `pyproject.toml` stays portable (any PEP 621 tool can read it), and `uv.lock` makes installs reproducible between the laptop, CI, and the Docker image. The same `uv sync --frozen` command will be used in all three.

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

## 8. Local-Service Strategy (not implemented)

Docker Compose is the right tool: DarwinUX needs a few long-lived local dependencies, not an orchestrator.

Planned, introduced only when a step needs them:

| Service | Introduced | Local image | AWS equivalent |
|---|---|---|---|
| PostgreSQL + pgvector | Step 2 | an official pgvector Postgres image | RDS for PostgreSQL |
| SQS-compatible queue emulator | Step 2 | ElasticMQ or LocalStack (OPEN_QUESTIONS.md N4) | SQS |
| OpenTelemetry collector + trace viewer | Step 2+ | OTel collector + Jaeger | ADOT collector → CloudWatch |
| S3-compatible storage | Step 3, only if a local folder is insufficient | — | S3 |

Principles:

- **Only dependencies run in Compose; the application does not** (at first). The API and workers run on the host with `uv run`, which gives fast reloads and a normal debugger. A containerised app service is added when the Dockerfile is written.
- A single `compose.yaml` at the repository root, so `docker compose up` works without `-f` flags.
- Named volumes for data; `docker compose down -v` is the documented reset.
- Services bind to `localhost` only.

**Local machine note:** this Mac has the Docker CLI and Colima (the Docker engine VM) installed via Homebrew, plus the standalone `docker-compose` binary. Colima must be started (`colima start`) before Docker can be used, and the Compose plugin must be made visible to `docker` (see "Manual setup" below).

## 9. Commands

A small root `Makefile` wraps the real commands. It only delegates to uv — uv still owns dependencies — and every target works today. Run `make help` to list them.

| Command | What it runs (in `backend/`) | Use it to |
|---|---|---|
| `make sync` | `uv sync` | Install/refresh dependencies from `uv.lock` |
| `make api` | `uv run [--env-file ../.env] uvicorn darwin.main:app --reload` | Start the API on http://127.0.0.1:8000 with auto-reload |
| `make test` | `uv run pytest` | Run the tests |
| `make lint` | `uv run ruff check .` | Lint |
| `make format` | `uv run ruff format .` | Format in place |
| `make typecheck` | `uv run mypy src tests` | Type check |
| `make check` | format check + lint + typecheck + tests | Run everything CI will run, before every commit |

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

Then make the Homebrew Compose plugin visible to `docker` by adding this key to `~/.docker/config.json` (keep the existing keys):

```json
"cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]
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
| SQLAlchemy, Alembic, database drivers | Later step | No database yet |
| `compose.yaml`, Postgres, queue emulator | Later step | Nothing connects to them |
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
└── api/
    ├── router.py      # the /api/v1 router; includes every v1 router
    └── health.py      # /health/live, /health/ready + their response models
```

Modules are added when there is code for them — no empty `rag/`, `agents/`, or `providers/` directories. There is also no `core/` package: generic names like "core" or "utils" become dumping grounds with no clear place in the dependency rule.

**Dependency direction:** `main` → `api`, `config`, `logging_config`; `logging_config` → `config`; `config` and `api/health` import nothing else from `darwin`. Nothing imports `main`. This is the start of the layering in ARCHITECTURE.md: the entrypoint wires pieces together; the pieces don't know about the entrypoint.

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
| Checks | Nothing — if the handler runs, the answer is yes | Application state now; dependencies (database, queue) later |
| Failure means | The process is stuck → **restart** it | Not ready yet or a dependency is down → **stop sending traffic**, don't restart |
| Response | `200 {"status": "alive"}` | `200 {"status": "ready", "checks": [...]}` or `503 {"status": "not_ready", "checks": [...]}` |

Why they must differ: if liveness also checked the database, a database outage would make the orchestrator restart every healthy API instance in a loop — making the outage worse. Readiness failing only removes instances from the load balancer until the dependency recovers.

Today readiness has one real check, `startup_complete`: true after the lifespan startup has run, false before startup and after shutdown. When a database arrives, a `database` check is appended to the `checks` list; the response shape and status codes stay the same, so nothing that calls the endpoint has to change.

### Tests

| File | Covers |
|---|---|
| `tests/test_app.py` | App factory, metadata, independent instances, `/api/v1` versioning |
| `tests/test_health.py` | Liveness/readiness responses, schemas, 503 before startup and after shutdown, method handling |
| `tests/test_config.py` | Defaults, `DARWIN_` prefix, case-insensitive log level, fail-fast validation, ignoring future variables |
| `tests/test_logging.py` | JSON output, structured context, robustness, idempotent configuration |

API tests use FastAPI's `TestClient`, which calls the ASGI app in-process — no server or network needed. Using it as a context manager (`with TestClient(app)`) runs the lifespan, exactly like Uvicorn.

---

## What You Should Understand Before Step 2

1. **Reproducibility comes from lockfiles, not version ranges.** `pyproject.toml` says what is *allowed*; `uv.lock` records exactly what was *installed*. Commit the lockfile and install with `--frozen` in CI and Docker.
2. **The virtual environment is disposable; the lockfile is not.** You can delete `.venv` any time; `uv sync` recreates it identically.
3. **Formatter, linter, type checker, and tests catch different classes of problems.** Passing one says nothing about the others.
4. **`.env.example` is documentation; `.env` is local state.** Secrets live only in `.env` locally and in Secrets Manager in AWS — never in git.
5. **Local services run in Compose; the application runs on the host at first.** That keeps the edit–run–debug loop fast while dependencies stay reproducible.
