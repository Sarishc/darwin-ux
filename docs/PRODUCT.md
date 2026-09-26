# DarwinUX — Product Definition

## Vision

DarwinUX is software that learns how to redesign itself.

It is an experimental AI engineering platform that closes the loop between user behavior and interface evolution. Rather than relying solely on human designers to interpret analytics dashboards and manually propose changes, DarwinUX uses AI to observe, reason, propose, evaluate, and learn — while keeping humans as the final decision-makers.

## What DarwinUX Is NOT

- **Not a design tool.** It does not replace Figma or design thinking. It proposes constrained mutations within an existing design system.
- **Not an autonomous code generator.** It does not have unrestricted access to rewrite application code. It operates within a tightly constrained mutation surface.
- **Not a chatbot.** There is no conversational UI at the center. The core is a closed-loop system that runs mostly in the background.
- **Not a dashboard.** It produces dashboards (the Evolution Lab), but the value is the loop, not the visualization.

## Core Concept

The fundamental idea is biological evolution applied to user interfaces:

1. **Observe** — Collect telemetry about how users actually interact with software.
2. **Detect** — Identify behavioral signals that suggest UX friction (rage clicks, abandonment, repeated errors, slow task completion).
3. **Research** — Retrieve relevant product knowledge, design guidelines, past experiments, and historical evidence from Product Memory (RAG).
4. **Hypothesize** — AI agents propose why friction exists and what might improve it.
5. **Mutate** — Generate a constrained UI change (a "mutation") that could address the hypothesis.
6. **Evaluate** — Run AI evaluation to check the mutation for correctness, accessibility, safety, and alignment with design standards.
7. **Experiment** — Deploy the mutation to a controlled subset of users (A/B test or similar).
8. **Decide** — A combination of Jev (decision engine), AI evaluation, and human approval determines whether the mutation is promoted.
9. **Learn** — Results feed back into Product Memory, improving future proposals.

Each complete cycle produces a new **Generation** — an explicit, traceable record of what changed, why, what evidence supported it, and what happened.

## The Evolution Lab (Frontend)

The Evolution Lab is a web interface for engineering and product teams to observe and govern the evolution process. It should eventually show:

- **Generations** — A timeline of evolutionary steps with lineage.
- **Mutations** — Proposed UI changes with before/after views.
- **Experiments** — Active and completed A/B tests with results.
- **Agent Runs** — What each agent did, what tools it called, what it decided.
- **AI Decisions** — Jev's classifications, scores, and confidence levels.
- **Retrieved Evidence** — What Product Memory surfaced and why.
- **Evaluation Results** — Scores across all evaluation dimensions.
- **Monitoring** — System health, pipeline status, model performance.

The Evolution Lab is an observability and governance tool, not a design editor.

## Constraints

### Safety First
- AI cannot autonomously deploy to production without human approval.
- AI cannot modify authentication, authorization, secrets, infrastructure, or CI/CD.
- All mutations operate within a constrained surface (design tokens, feature flags, component configurations).

### Evidence-Based
- No mutation proceeds without behavioral evidence.
- No experiment runs without evaluation.
- No promotion happens without measurable results.

### Traceable
- Every generation records its full lineage: evidence → hypothesis → mutation → evaluation → experiment → decision.
- The system must be auditable.

### Incremental
- Mutations are small, constrained changes — not wholesale redesigns.
- The system evolves through many small improvements, not dramatic rewrites.

## Primary Learning Goals

This project is being built to develop hands-on AI engineering knowledge. The architecture is designed to give meaningful experience with:

### Core Engineering
- Python (application architecture, async, testing)
- FastAPI (REST APIs, dependency injection, middleware)
- Pydantic (data validation, structured output, settings)
- PostgreSQL + SQL (schema design, queries, migrations)
- Docker (containerization, multi-stage builds)

### Data & Pipelines
- Asynchronous processing (queues, workers, event-driven architecture)
- Data pipeline design (ingestion, transformation, persistence)
- OpenTelemetry (instrumentation, tracing, metrics)

### AI / ML Engineering
- RAG (ingestion, chunking, embeddings, retrieval, reranking, evaluation)
- LLMs (prompt engineering, structured generation, provider abstraction)
- LangChain (where genuinely useful — document loading, text splitting, chains)
- LangGraph (stateful agent orchestration, conditional routing, human-in-the-loop)
- Tool calling (function calling, tool design, error handling)
- AI agents (when to use them, when not to, agent evaluation)
- Jev (decision-oriented AI, classification, scoring, gating)

### Evaluation
- RAG evaluation (retrieval quality, context relevance, groundedness)
- LLM evaluation (schema compliance, faithfulness, hallucination detection)
- Agent evaluation (trajectory correctness, task success, efficiency)
- Mutation evaluation (functional correctness, accessibility, regression)

### Infrastructure & Operations
- AWS (ECS/Fargate, RDS, S3, SQS, ECR, IAM, Secrets Manager)
- Terraform (infrastructure as code)
- GitHub Actions (CI/CD pipelines)
- CloudWatch (logging, metrics, alarms)
- Production AI monitoring (model drift, latency, cost tracking)

### What Should NOT Be Forced

Not every technology above needs to appear in version 1. The architecture should support them, but implementation should add them when they solve a real problem — not to pad a resume.

---

## What You Should Understand Before Implementation

1. **The evolution loop is the product.** Every architectural decision should be evaluated against whether it supports the observe → reason → mutate → evaluate → learn cycle.
2. **Safety constraints are not optional features.** The mutation safety model is a core architectural requirement, not something to add later.
3. **Evaluation is not testing.** Testing verifies that code works. Evaluation measures whether AI systems produce good results. DarwinUX needs both.
4. **RAG is not "vector search."** It is a complete lifecycle from document ingestion through retrieval quality measurement. Product Memory is the persistent knowledge that makes the system intelligent.
5. **Not everything should be an agent.** The hardest architectural skill is knowing when deterministic code is better than an LLM call. This project should teach that judgment.
