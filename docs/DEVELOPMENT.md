# DarwinUX — Development Environment

> **Status (Step 1):** toolchain decisions and configuration only. There is no application code, no runtime dependency, and no local service yet.

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

**Project shape:** `backend/pyproject.toml` sets `[tool.uv] package = false` because no importable package exists yet. When `backend/src/darwin/` is created (Step 2), a build backend is added and this flag is removed.

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
- When the backend exists, settings will be loaded and validated by a Pydantic settings class; unknown or missing required values fail at startup, not at first use.

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

**Status:** configured in `backend/pyproject.toml` and declared in the `dev` dependency group; **not installed**. There is no Python code to check yet. They are installed by the first `uv sync`.

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

There is **no Makefile yet**, on purpose: in Step 1 the only real commands are a handful of one-liners, and a Makefile containing targets for things that don't exist would lie about the project's state. A Makefile will be added in Step 2, when there are commands worth wrapping (start services, run the API, run all checks).

Commands that work today (once uv is installed):

```bash
# from backend/
uv python find 3.13          # confirm uv sees a Python 3.13 interpreter
uv sync                      # create backend/.venv, install dev tools, write uv.lock
uv run ruff format --check . # formatter (nothing to format yet)
uv run ruff check .          # linter (nothing to lint yet)
```

```bash
# from the repository root
nvm use                      # switch to Node 24 per .nvmrc
cp .env.example .env         # create local env file (git-ignored)
```

`mypy` and `pytest` are configured but have nothing to run on; `pytest` exits with "no tests collected" until Step 2.

Planned Makefile targets (Step 2+, not created): `setup`, `services-up`, `services-down`, `fmt`, `lint`, `typecheck`, `test`, `check` (= fmt-check + lint + typecheck + test, the same thing CI will run).

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
| FastAPI, Pydantic, SQLAlchemy, Alembic, any runtime dependency | Step 2+ | No code uses them |
| `backend/src/darwin/` package, tests | Step 2 | Application code is out of scope |
| `compose.yaml`, Postgres, queue emulator | Step 2 | Nothing connects to them |
| Dockerfile | Step 2+ | No app to containerise |
| Makefile | Step 2 | No commands worth wrapping yet |
| Next.js, `package.json`, `node_modules` | Step 2 (demo app) / 7 | Frontend not started |
| GitHub Actions | Step 2+ | Nothing to build or test |
| Terraform, AWS | Step 9 | Local-first |
| OpenTelemetry SDK | Step 2+ | No process to instrument |
| pre-commit hooks | Future option | `make check` + CI will enforce the same checks without another tool; revisit if checks are forgotten in practice |
| devcontainers, Nix, Bazel, monorepo frameworks | Not planned | Overhead with no benefit for one developer and two apps |
| Large IDE configs (`.vscode/`, `.idea/`) | Not planned | `.editorconfig` + `pyproject.toml` are tool-neutral |

---

## What You Should Understand Before Step 2

1. **Reproducibility comes from lockfiles, not version ranges.** `pyproject.toml` says what is *allowed*; `uv.lock` records exactly what was *installed*. Commit the lockfile and install with `--frozen` in CI and Docker.
2. **The virtual environment is disposable; the lockfile is not.** You can delete `.venv` any time; `uv sync` recreates it identically.
3. **Formatter, linter, type checker, and tests catch different classes of problems.** Passing one says nothing about the others.
4. **`.env.example` is documentation; `.env` is local state.** Secrets live only in `.env` locally and in Secrets Manager in AWS — never in git.
5. **Local services run in Compose; the application runs on the host at first.** That keeps the edit–run–debug loop fast while dependencies stay reproducible.
