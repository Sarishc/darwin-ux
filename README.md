# DarwinUX

**Software that learns how to redesign itself.**

---

## What Is DarwinUX?

DarwinUX is an experimental AI engineering platform that observes how users interact with software, identifies UX friction, retrieves relevant historical and product knowledge, reasons about possible improvements, generates constrained UI mutations, evaluates them, runs controlled experiments, and learns from the results.

It is not a chatbot wrapper. It is a closed-loop system where software evolves through evidence, evaluation, and controlled experimentation — with humans remaining in the decision loop at every critical gate.

## Status

**Step 7 — Generation 0 Demo App + Telemetry SDK** (Steps 0–6 complete)

A Next.js demo app (`/demo`) rendered from a validated, data-only **Generation 0 UI Spec** through an allowlisted component registry, with deliberate UX friction and a small browser telemetry SDK. Behind it: a FastAPI producer that validates telemetry and durably queues it (`POST /api/v1/telemetry/events` → 202), a separate worker process that stores events idempotently and reconciles deterministic behaviour signals (`rage_click`, `error_burst`), a PostgreSQL-backed local queue with leases, retries and dead-lettering, settings, structured logging, and a PostgreSQL 17 persistence layer (SQLAlchemy + Alembic). Local PostgreSQL runs natively via Homebrew — no Docker. No AI components yet. To set up a machine and run it, see [DEVELOPMENT.md](docs/DEVELOPMENT.md).

```bash
make sync   # install locked dependencies
make api    # terminal 1 — http://127.0.0.1:8000/api/v1/health/live
make worker # terminal 2 — processes queued telemetry
make web    # terminal 3 — http://localhost:3000/demo (first: make web-install)
make web-check  # frontend lint, type check, tests, production build
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
