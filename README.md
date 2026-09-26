# DarwinUX

**Software that learns how to redesign itself.**

---

## What Is DarwinUX?

DarwinUX is an experimental AI engineering platform that observes how users interact with software, identifies UX friction, retrieves relevant historical and product knowledge, reasons about possible improvements, generates constrained UI mutations, evaluates them, runs controlled experiments, and learns from the results.

It is not a chatbot wrapper. It is a closed-loop system where software evolves through evidence, evaluation, and controlled experimentation — with humans remaining in the decision loop at every critical gate.

## Status

**Step 1 — Development Environment** (Step 0 architecture complete)

No application code exists yet. The repository contains architectural documentation and the local toolchain configuration. To set up a machine, see [DEVELOPMENT.md](docs/DEVELOPMENT.md).

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
