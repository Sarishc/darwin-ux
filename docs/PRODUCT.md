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
4. **Hypothesize** — The LangGraph workflow (one research agent plus structured LLM calls) proposes why friction exists and what might improve it.
5. **Decide to act** — Jev gates whether the evidence is sufficient to proceed.
6. **Mutate** — Muse generates a constrained candidate UI change (a "mutation") — a structured patch to a UI specification, never source code.
7. **Evaluate** — Sandbox rendering, deterministic validation, and AI evaluation check the candidate for correctness, accessibility, safety, and alignment with design standards.
8. **Approve** — Jev gates experiment readiness; a human approves before any user sees the change.
9. **Experiment** — Expose the mutation to a controlled subset of users (A/B test) behind a feature flag. Guardrail breaches roll back automatically.
10. **Promote or discard** — A human decides, based on pre-registered metrics, whether the mutation becomes the new default.
11. **Learn** — Results (positive and negative) feed back into Product Memory, improving future proposals.

Each complete cycle produces a new **Generation** — an explicit, traceable record of what changed, why, what evidence supported it, and what happened.

## The Evolution Lab (Frontend)

The Evolution Lab is a web interface for engineering and product teams to observe and govern the evolution process. It should eventually show:

- **Generations** — A timeline of evolutionary steps with lineage.
- **Mutations** — Proposed UI changes with before/after views.
- **Experiments** — Active and completed A/B tests with results.
- **Agent Runs** — What each agent did, what tools it called, what it decided.
- **AI Decisions** — Jev's (and baseline deciders') outcomes and confidence levels.
- **Muse Candidates** — What Muse generated, including rejected attempts and why they failed.
- **Retrieved Evidence** — What Product Memory surfaced and why.
- **Evaluation Results** — Scores across all evaluation dimensions.
- **Monitoring** — System health, pipeline status, model performance.

The Evolution Lab is an observability and governance tool, not a design editor.

## Constraints

### Safety First
- AI cannot autonomously expose a change to users or promote it without human approval. (Automatic *rollback* to a previous generation is allowed.)
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

## Target Application and Users

DarwinUX needs something to evolve. Generation 0 is a small **demo target application** (for example a sign-up or checkout flow) that renders from a structured UI Spec, with deliberate, known UX friction. Because there are no real users, experiments initially run on a **synthetic user simulator**; simulated results are always labelled as such. See OPEN_QUESTIONS.md.

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
- LangGraph (stateful workflow orchestration, conditional routing, human-in-the-loop)
- Tool calling (function calling, tool design, error handling)
- AI agents (when to use them, when not to, agent evaluation)
- Jev by TypeSafe AI (decision-oriented AI: classification, scoring, gating) — behind a provider boundary
- Muse (generative mutation capability) — behind a provider boundary
- Prompt management (versioned prompts, recorded on every model call)

### Evaluation
- RAG evaluation (retrieval quality, context relevance, groundedness)
- LLM evaluation (schema compliance, faithfulness, hallucination detection)
- Agent evaluation (trajectory correctness, task success, efficiency)
- Mutation evaluation (functional correctness, accessibility, regression)

### Infrastructure & Operations
- Next.js + TypeScript (Evolution Lab and demo target app)
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
