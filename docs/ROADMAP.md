# DarwinUX — Roadmap

## How to Read This

Each step is a thin, working vertical slice with an explicit **exit criterion**. A step is done when the exit criterion is demonstrably true, not when its code exists. Later steps are deliberately less detailed; they will be refined when reached.

Mocks come first for anything whose real integration is unknown (Jev, Muse). The system is built and evaluated against those mocks and against LLM baselines, then the real providers are plugged in behind the same ports.

Operational telemetry (OpenTelemetry) is added in minimal form in Step 2b, once there is more than one process to trace, and deepened as components appear.

## Steps

### Step 0 — Architecture & Documentation ✅ (this step)

Documents in `docs/`. No code, no dependencies, no infrastructure.

**Exit:** all documents exist, are consistent, and open questions are recorded.

### Step 1 — Development Environment

- Toolchain decisions: Python 3.13, uv, Ruff, mypy, pytest, Node 24 LTS (docs/DEVELOPMENT.md).
- `backend/pyproject.toml` (metadata + tool config, no runtime dependencies), `.python-version`, `.nvmrc`.
- `.gitignore`, `.editorconfig`, `.env.example` with the environment-variable strategy.
- Local-service strategy documented, not implemented.

**Learn:** reproducible environments, lockfiles, the distinct jobs of formatter/linter/type checker/test runner, secret hygiene.
**Exit:** after the documented manual installs, `uv sync` succeeds and `.env` is provably git-ignored.

### Step 2a — Python Application Foundation ✅

- Installable `darwin` package (src layout, `uv_build`), FastAPI app factory + lifespan, `/api/v1` router.
- `GET /api/v1/health/live` and `GET /api/v1/health/ready` with explicit response models.
- `Settings` (pydantic-settings, `DARWIN_` prefix), JSON-lines logging with the standard library.
- Tests for app creation, health endpoints, settings, and logging; root Makefile (`make check`).

**Learn:** package layout, ASGI, routers, Pydantic models and settings, dependency direction, liveness vs. readiness, API tests.
**Exit:** `make check` passes; both endpoints return 200 from a running server.

### Step 2c — PostgreSQL Persistence Foundation

(Built before 2b; in the step-by-step build this is "Step 3".)

- Native Homebrew PostgreSQL 17 + pgvector (installed, not enabled) — no Docker locally.
- SQLAlchemy 2.x (sync) + psycopg 3; engine and session lifecycle tied to the FastAPI lifespan.
- Alembic with the first migration (`user_event`); readiness checks the database with `SELECT 1`.
- Unit tests stay database-free; integration tests run against a guarded `darwin_test`.

**Learn:** engine vs. session vs. transaction, connection pooling, migrations vs. `create_all`, schema constraints, unit vs. integration tests.
**Exit:** migrations upgrade/downgrade cleanly; readiness is 200 with PostgreSQL up and 503 with it down; integration tests pass.

### Step 2b — Behavioral Telemetry Pipeline + Generation 0

Prerequisites: the persistence foundation (Step 2c), CI running `make check`, and minimal OpenTelemetry. `BehaviorSignal` is introduced here, together with the behaviour that uses it.

- ✅ Demo target app (Next.js `/demo`) rendering from a validated, data-only **UI Spec** via an allowlisted component registry — Generation 0, with three documented friction points (built as "Step 7").
- ✅ `POST /api/v1/telemetry/events` (202), validated `TelemetryEvent`, idempotent synchronous persistence (built as "Step 4").
- ✅ Ingestion behind a durable queue → separate telemetry worker, at-least-once with leases, retries and dead-lettering; local PostgreSQL-backed queue behind a `MessageQueue` port (built as "Step 6"; SQS adapter later). Endpoint contract unchanged.
- ✅ Browser telemetry SDK (session-scoped anonymous id, typed payloads, fire-and-forget) and explicit backend CORS (built as "Step 7").
- ✅ Windowed deterministic signal detectors (`rage_click`, `error_burst`) with replay-safe `BehaviorSignal` persistence (built as "Step 5"). Deferred: abandonment, confusion loop.
- ✅ DLQ equivalent (`status = dead`, built with the queue in "Step 6").
- First version of the synthetic user simulator.

**Learn:** async processing, queues, idempotency, data pipelines, first Next.js/TypeScript work.
**Exit:** simulated users produce events; rage-click/abandonment signals appear in Postgres; replaying a batch changes nothing.

### Step 3 — Product Memory (RAG) ✅ foundation (built as "Step 8")

Built: allowlisted corpus, deterministic chunking, embedding port + hashing baseline provider, pgvector (exact search), SQL filters, retrieval runs, 26-query golden set with Precision@K / Recall@K / MRR across three chunking configs. Not yet: a real embedding provider, reranking, HNSW, raw-document storage in S3, an ingestion worker.


- Ingestion worker: raw storage, hashing, parsing, normalization, chunking, embeddings, pgvector index.
- Retrieval service: vector search with SQL filters, context construction; `RetrievalRun` records.
- First golden retrieval dataset (20–30 queries) + Precision@K / Recall@K / MRR evaluation.

**Learn:** document ingestion, chunking, embeddings, vector search, retrieval evaluation, LangChain loaders/splitters where they help.
**Exit:** retrieval metrics computed on the golden set and recorded; changing chunk size shows a measurable effect.

### Step 4 — LLM Layer ✅ hypothesis part (built as "Step 9")

Built: DarwinUX-owned LLM port + deterministic `FakeLLMProvider`, versioned `hypothesis.v1` request, EvidenceBundle, strict output schema, deterministic grounding checks, `hypothesis_run` / `hypothesis` (migration 0005), 18-case golden hypothesis eval. Not yet: a real provider adapter (N1), the critique call, LLM-as-judge relevance / faithfulness.


- LLM provider port + one adapter; prompt templates versioned in git; `ModelCall` records.
- Hypothesis and critique as structured-output calls.
- LLM evals: schema compliance, citation check (cited ⊆ retrieved), LLM-as-judge relevance.

**Learn:** provider abstraction, structured outputs, prompt management, LLM evaluation.
**Exit:** golden hypothesis scenarios pass schema + citation checks in CI.

### Step 5 — LangGraph Investigation Workflow (with mock Jev and mock Muse) — research part ✅ (built as "Step 10")

Built: bounded LangGraph research graph (`research_graph.v1`): deterministic query + sufficiency heuristic + one refinement, Step 9 hypothesis, one critique call, human review with CLI resume, `research_run` / `research_step` (migration 0006), 19-case golden eval with trajectory checks. Changed from the plan: DarwinUX-owned persistence instead of LangGraph Postgres checkpointing; decisions via CLI, not API; no Jev, no Muse, no mutation context yet.


- `JevProvider` port with `RulesDecider` + `LLMBaselineDecider`; `MuseProvider` port with `FixtureMuse` + `LLMBaselineGenerator`.
- Graph: triage → research agent (RAG tool) → hypothesize → critique → evidence gate → context → generate → (stub validation) → approval interrupt.
- Postgres checkpointing; human decisions via API.
- Agent evals: golden trajectories, tool-selection, loop counts, cost per run.

**Learn:** LangGraph state, conditional edges, interrupts/checkpoints, tool calling, the one real agent, agent evaluation.
**Exit:** a detected signal produces a full, traceable AgentRun ending in an approval request; golden trajectories pass.

### Step 6 — Mutation Surface, Sandbox, Evaluation Engine

- MutationSpec schema, registry-based allowlist and bounds validation, UI Spec versioning.
- Sandbox render via headless browser with automated accessibility checks; artefacts in object storage.
- Evaluation orchestrator; risk tiers; mutation golden set (known-bad specs must be rejected) blocking in CI.

**Learn:** constrained generation, defense in depth, deterministic vs. model-based evaluation.
**Exit:** every known-bad golden mutation is rejected; a valid one produces screenshots + scores.

### Step 7 — Evolution Lab

- Next.js views: generations timeline, AgentRun detail (nodes, retrievals, decisions), candidate diff with before/after screenshots, approval inbox.
- Simple single-user authentication for approvals.

**Learn:** Next.js + TypeScript application structure, typed API clients.
**Exit:** a human can approve or reject from the UI and the graph resumes.

### Step 8 — Experiments and Generation 1

- Flag table + deterministic cohort assignment; Experiment Manager worker.
- Pre-registered primary/guardrail metrics; fixed-horizon analysis; sample-ratio-mismatch check; automatic rollback.
- Human promotion → new Generation; experiment report ingested into Product Memory.

**Learn:** experimentation statistics, feature flags, reversibility, closing the loop.
**Exit:** Generation 0 → Generation 1 via a (simulated-traffic) experiment, fully traceable; a forced guardrail breach rolls back automatically.

### Step 9 — AWS + Terraform + Deployment

- Terraform modules for the `dev` environment (AWS_ARCHITECTURE.md).
- GitHub Actions: build, push to ECR, deploy to ECS via OIDC; path-filtered AI evals.
- ADOT collector, CloudWatch dashboards and alarms, budget alert.

**Learn:** Terraform, ECS/Fargate, IAM least privilege, Secrets Manager, CI/CD, CloudWatch.
**Exit:** the full loop runs in AWS from a clean `terraform apply`; `terraform destroy` removes everything.

### Step 10 — Real Jev and Muse Integration — Jev decision layer ✅ harness (built as "Step 11")

Built: DarwinUX Decider port, `rules.v1` baseline, test double, LLM-port baseline, a Jev adapter written against TypeSafe's public HTTP docs (not yet called live — no key), fail-closed policy, `decision_run` (migration 0007), 27-case per-decider evaluation with a fail-open count. Not yet: a live Jev comparison on independently labelled data; Muse.


Blocked on OPEN_QUESTIONS.md (Jev and Muse sections).

- `JevAdapter` and `MuseAdapter` behind the existing ports.
- Side-by-side evaluation vs. rules and LLM baselines on the same golden sets.

**Learn:** integrating external models behind existing ports, calibration measurement, comparative evaluation.
**Exit:** a written comparison of Jev vs. baselines and Muse vs. baseline, with numbers.

### Step 11 — Production AI Monitoring and Hardening

- Online sampling + judge re-scoring, drift dashboards, cost budgets per run and per day.
- Judge calibration against human labels; reranking if retrieval metrics justify it.

**Exit:** a documented incident drill (e.g., provider change, corpus change) detected by monitoring.

## Explicitly Not on the Roadmap (for now)

Microservices, Kubernetes, multi-tenant support, a production prod environment with Multi-AZ, auto-promotion of mutations, multi-armed bandits, AI-generated new components, arbitrary code generation. See ARCHITECTURE.md and OPEN_QUESTIONS.md (future research).

---

## What You Should Understand Before Implementation

1. **Vertical slices beat horizontal layers.** Each step ends with something that runs end to end, however small.
2. **Mocks are a design tool, not a shortcut.** Building against Jev/Muse ports with baselines forces the contract to be explicit and gives you something to compare the real providers against.
3. **Evaluation arrives with each AI component, not at the end.** Every AI step's exit criterion is a measured result.
4. **AWS comes late on purpose.** Cloud deployment of a system that doesn't work locally teaches you about cloud debugging, not AI engineering.
5. **Simulated traffic is a limitation to state, not hide.** Generation 1 on simulated users demonstrates the mechanism, not a real UX improvement.
