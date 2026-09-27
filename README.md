# DarwinUX

**Software that learns how to redesign itself.**

---

## What Is DarwinUX?

DarwinUX is an experimental AI engineering platform that observes how users interact with software, identifies UX friction, retrieves relevant historical and product knowledge, reasons about possible improvements, generates constrained UI mutations, evaluates them, runs controlled experiments, and learns from the results.

It is not a chatbot wrapper. It is a closed-loop system where software evolves through evidence, evaluation, and controlled experimentation — with humans remaining in the decision loop at every critical gate.

## Status

**Step 13 — Candidate Sandbox + Mutation Evaluation** (Steps 0–12 complete)

Safe is not the same as useful. Every candidate UI Spec can now be evaluated in a sandbox: the frontend's real Zod schema, component registry and `SpecPage` render it in jsdom (telemetry captured in memory, never sent), and a deterministic `candidate_eval.v1` policy scores seven categories separately — schema, render, functional (CTA reveals, telemetry ids, form behaviour, no typed values leaked), accessibility (axe-core + semantic checks, only NEW issues count), regression, UX-intent alignment with the observed problem (confirmed by measured behaviour) and modest structural performance bounds. The result is pass | human_review | reject, stored immutably; `pass` only means eligible for future human approval. The Step 12 harmful-but-safe candidate (`feedback: immediate → delayed`) is rejected. A 29-case golden set reports a fail-open count of zero. No LLM judge, no browser, no deployment.

Step 12 — A `proceed` decision can now produce a **candidate** UI Spec — data only. DarwinUX re-checks the decision's provenance (a stale decision is refused before any generator runs), sends a bounded MutationRequest to a MutationGenerator (a deterministic fixture and an LLM-port baseline; Muse is an explicit unimplemented seam — no documented interface), and accepts only a strict MutationSpec: `replace` on semantic `{component_id, property}` targets from an explicit allowlist, with closed-enum or bounded plain-text values. The change is applied in memory, an independent diff proves nothing protected moved, and the candidate is stored as an immutable, content-addressed `ui_spec_version` that the frontend's real Zod schema accepts. Nothing is written to the repository, rendered or deployed. A 28-case evaluation per generator reports zero unsafe candidates.

Step 11 — After a finished research run, one bounded decision — `proceed` (eligible for a *future* mutation stage, nothing more), `human_review` or `reject` — through a DarwinUX-owned Decider port: a deterministic `rules.v1` baseline, a test double, an LLM-port baseline, and a Jev adapter written against TypeSafe AI's public HTTP docs but never called live (no API key). A fail-closed policy outside every model turns any decider failure or invalid output into `human_review` and downgrades `proceed` when hard preconditions fail; `decision_run` rows and database CHECKs record it. A 27-case evaluation scores each decider separately, with a fail-open count. No Muse, no mutations, no experiments.

Step 10 — A bounded LangGraph graph (`research_graph.v1`) orchestrates the existing services for one signal: deterministic Product Memory retrieval with a sufficiency heuristic and at most one refinement, the Step 9 grounded hypothesis, one strict critique call, then accept, pause for human review (resumed from the CLI with an allowlisted decision), reject, or stop. Hard budgets (2 retrievals, 2 LLM calls, 12 steps) are enforced in code and by database CHECKs; every run and step is persisted (`research_run`, `research_step`). A 19-case golden set checks outcomes **and** trajectories. Still only a deterministic `FakeLLMProvider` — no Jev, no Muse, no mutations, no experiments.

Step 9 — hypothesis generation: one bounded, structured LLM call per signal: a BehaviorSignal is turned into a deterministic retrieval query, Product Memory evidence is bundled (marked untrusted), a versioned `hypothesis.v1` request goes through a DarwinUX-owned provider port, and the output must pass a strict schema and deterministic grounding checks (cited ids ⊆ supplied excerpts, allowed component) before it becomes a Hypothesis. Every attempt is an audited `hypothesis_run`. An 18-case golden set measures that control layer.

Product Memory (Step 8, retrieval only): an allowlisted corpus of DarwinUX docs and the Generation 0 UI Spec, chunked deterministically, embedded through a provider port (a deterministic hashing baseline — no real model yet), stored in PostgreSQL + pgvector, retrieved by exact cosine search with SQL filters, and measured on a 26-query golden set (Precision@K, Recall@K, MRR) across chunking configs.

A Next.js demo app (`/demo`) rendered from a validated, data-only **Generation 0 UI Spec** through an allowlisted component registry, with deliberate UX friction and a small browser telemetry SDK. Behind it: a FastAPI producer that validates telemetry and durably queues it (`POST /api/v1/telemetry/events` → 202), a separate worker process that stores events idempotently and reconciles deterministic behaviour signals (`rage_click`, `error_burst`), a PostgreSQL-backed local queue with leases, retries and dead-lettering, settings, structured logging, and a PostgreSQL 17 persistence layer (SQLAlchemy + Alembic). Local PostgreSQL runs natively via Homebrew — no Docker. To set up a machine and run it, see [DEVELOPMENT.md](docs/DEVELOPMENT.md).

```bash
make sync   # install locked dependencies
make api    # terminal 1 — http://127.0.0.1:8000/api/v1/health/live
make worker # terminal 2 — processes queued telemetry
make web    # terminal 3 — http://localhost:3000/demo (first: make web-install)
make web-check  # frontend lint, type check, tests, production build
make memory-ingest && make memory-eval   # Product Memory: ingest corpus, evaluate retrieval
make hypothesis-generate && make hypothesis-eval   # hypothesis for the latest signal; golden eval
make research-run && make research-eval            # research workflow for the latest signal; golden eval
make decision-run && make decision-eval            # decide the latest finished research run; per-decider eval
make ui-spec-import && make mutation-generate && make mutation-eval   # candidate UI Spec from the latest proceed
make candidate-eval && make sandbox-eval             # evaluate the latest candidate; golden sandbox set
make check  # format check, lint, type check, unit tests
make db-start && make db-setup && make migrate   # local PostgreSQL 17
make test-integration
```

## The Evolution Loop

```
User Interaction
  → Behavioral Telemetry → Data Pipeline → Behavioral Signals   (deterministic)
  → RAG / Product Memory                                         (evidence)
  → LangGraph Investigation Workflow                              (reasoning)
  → Jev Decision Gate → Hypothesis                                (decision)
  → Muse → Candidate Mutation (structured UI Spec patch)          (generation)
  → Sandbox → Deterministic Validation → AI Evaluation            (quality / safety)
  → Jev Experiment Gate → Human Approval
  → Controlled Experiment (feature flag) → Monitoring / auto-rollback
  → Human Promotion Decision → New Generation (or discard)
  → Experiment Results → Product Memory → Next Generation
```

AI never edits source code, never exposes a change to users without human approval, and never touches auth, secrets, infrastructure, or CI/CD. See [MUTATION_SAFETY.md](docs/MUTATION_SAFETY.md).

## Technology Foundation

| Layer | Technology |
|---|---|
| Backend / AI Platform | Python, FastAPI, Pydantic |
| Database | PostgreSQL (RDS) |
| Frontend | Next.js, TypeScript (Evolution Lab + demo target app) |
| Workflow Orchestration | LangGraph (LangChain only where useful: loaders, splitters) |
| Decision Layer | Jev by TypeSafe AI — behind a provider boundary |
| Generative Mutation Layer | Muse — behind a provider boundary |
| RAG / Product Memory | Custom pipeline, embeddings + pgvector |
| Evaluation | Deterministic checks, statistical metrics, LLM-as-judge, human review, golden datasets |
| Async processing | SQS queue + Python workers |
| Containers | Docker |
| Infrastructure | AWS (ECS/Fargate, ECR, RDS, S3, SQS, IAM, Secrets Manager, CloudWatch) — not provisioned yet |
| IaC | Terraform |
| CI/CD | GitHub Actions |
| Observability | OpenTelemetry, CloudWatch |

Jev and Muse integration details (APIs, SDKs, model identifiers, credentials) are **not yet known** and are deliberately not invented here; see [OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md).

**Shape:** a modular monolith — one Next.js app, one FastAPI app, and Python workers sharing the same codebase and image.

## Documentation

All architectural documentation lives in [`docs/`](docs/):

| Document | Purpose |
|---|---|
| [PRODUCT.md](docs/PRODUCT.md) | Product vision, goals, and constraints |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | High-level system architecture with diagrams |
| [DOMAIN_MODEL.md](docs/DOMAIN_MODEL.md) | Core entities, relationships, and lifecycles |
| [RAG_ARCHITECTURE.md](docs/RAG_ARCHITECTURE.md) | Product Memory and retrieval system design |
| [AGENT_ARCHITECTURE.md](docs/AGENT_ARCHITECTURE.md) | Agent responsibilities and what should NOT be an agent |
| [EVALUATION_STRATEGY.md](docs/EVALUATION_STRATEGY.md) | Four-level AI evaluation framework |
| [DATA_PIPELINES.md](docs/DATA_PIPELINES.md) | Telemetry and RAG ingestion pipelines |
| [MUTATION_SAFETY.md](docs/MUTATION_SAFETY.md) | Constrained mutation surface and safety boundaries |
| [AWS_ARCHITECTURE.md](docs/AWS_ARCHITECTURE.md) | Cloud deployment architecture |
| [OBSERVABILITY.md](docs/OBSERVABILITY.md) | Multi-level observability strategy |
| [ROADMAP.md](docs/ROADMAP.md) | Phased implementation plan |
| [OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md) | Blocking, non-blocking, and research questions |
| [DEVELOPMENT.md](docs/DEVELOPMENT.md) | Local toolchain, environment variables, commands |

## Learning Goals

This project is intentionally designed to develop practical AI engineering knowledge across the full stack — from data pipelines and RAG to agent orchestration, evaluation, and production deployment. See [PRODUCT.md](docs/PRODUCT.md) for the complete learning objectives.

## License

Private — Not open source.
