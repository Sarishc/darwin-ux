# DarwinUX — Roadmap

## How to Read This

Each step is a thin, working vertical slice with an explicit **exit criterion**. A step is done when the exit criterion is demonstrably true, not when its code exists. Later steps are deliberately less detailed; they will be refined when reached.

Mocks come first for anything whose real integration is unknown (Jev, Muse). The system is built and evaluated against those mocks and against LLM baselines, then the real providers are plugged in behind the same ports.

Operational telemetry (OpenTelemetry) is added with the first running process (Step 2a) in minimal form and deepened as components appear.

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

### Step 2a — Backend Foundations

- Package skeleton `backend/src/darwin/` matching ARCHITECTURE.md; FastAPI app with health endpoint; Pydantic settings reading `.env`.
- `compose.yaml` with Postgres + pgvector; Makefile wrapping the real commands.
- Pure domain models (Pydantic) and state-transition rules, with unit tests.
- CI: format, lint, type check, unit tests.
- Minimal OpenTelemetry on the API.

**Learn:** project structure, dependency rules, Pydantic modelling, state machines, CI basics.
**Exit:** `docker compose up` + tests green in CI; illegal state transitions fail tests.

### Step 2b — Behavioral Telemetry Pipeline + Generation 0

- Demo target app (Next.js `/demo`) rendering from a seeded **UI Spec** via a component registry — Generation 0, with deliberate friction.
- Telemetry SDK + `POST /api/v1/telemetry/events` (202) → queue emulator → telemetry worker.
- Idempotent persistence, windowed deterministic signal detectors, DLQ.
- First version of the synthetic user simulator.

**Learn:** async processing, queues, idempotency, data pipelines, first Next.js/TypeScript work.
**Exit:** simulated users produce events; rage-click/abandonment signals appear in Postgres; replaying a batch changes nothing.

### Step 3 — Product Memory (RAG)

- Ingestion worker: raw storage, hashing, parsing, normalization, chunking, embeddings, pgvector index.
- Retrieval service: vector search with SQL filters, context construction; `RetrievalRun` records.
- First golden retrieval dataset (20–30 queries) + Precision@K / Recall@K / MRR evaluation.

**Learn:** document ingestion, chunking, embeddings, vector search, retrieval evaluation, LangChain loaders/splitters where they help.
**Exit:** retrieval metrics computed on the golden set and recorded; changing chunk size shows a measurable effect.

### Step 4 — LLM Layer

- LLM provider port + one adapter; prompt templates versioned in git; `ModelCall` records.
- Hypothesis and critique as structured-output calls.
- LLM evals: schema compliance, citation check (cited ⊆ retrieved), LLM-as-judge relevance.

**Learn:** provider abstraction, structured outputs, prompt management, LLM evaluation.
**Exit:** golden hypothesis scenarios pass schema + citation checks in CI.

### Step 5 — LangGraph Investigation Workflow (with mock Jev and mock Muse)

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

### Step 10 — Real Jev and Muse Integration

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
