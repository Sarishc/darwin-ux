# DarwinUX

**Software that learns how to redesign itself.**

---

## What Is DarwinUX?

DarwinUX is an experimental AI engineering platform that observes how users interact with software, identifies UX friction, retrieves relevant historical and product knowledge, reasons about possible improvements, generates constrained UI mutations, evaluates them, runs controlled experiments, and learns from the results.

It is not a chatbot wrapper. It is a closed-loop system where software evolves through evidence, evaluation, and controlled experimentation — with humans remaining in the decision loop at every critical gate.

## Status

**Step 0 — Architecture & Documentation**

No application code exists yet. This repository contains only architectural documentation, domain modeling, and design decisions that will guide implementation.

## The Evolution Loop

```
User Interaction
  → Telemetry
  → Data Pipeline
  → Behavioral Signals
  → RAG / Product Memory
  → AI Agents
  → Jev Decision Engine
  → Candidate Mutation
  → AI Evaluation
  → Experiment
  → Human Approval
  → Deployment
  → Monitoring
  → Feedback
  → Next Generation
```

## Technology Foundation

| Layer | Technology |
|---|---|
| Backend / AI Platform | Python, FastAPI, Pydantic |
| Database | PostgreSQL (RDS) |
| Frontend | Next.js, TypeScript, Tailwind CSS |
| Agent Orchestration | LangGraph |
| Decision Engine | Jev (TypeSafe AI) |
| RAG | Custom pipeline with embeddings + vector search |
| Infrastructure | AWS (ECS/Fargate, S3, SQS, RDS, ECR) |
| IaC | Terraform |
| CI/CD | GitHub Actions |
| Observability | OpenTelemetry, CloudWatch |

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

## Learning Goals

This project is intentionally designed to develop practical AI engineering knowledge across the full stack — from data pipelines and RAG to agent orchestration, evaluation, and production deployment. See [PRODUCT.md](docs/PRODUCT.md) for the complete learning objectives.

## License

Private — Not open source.
