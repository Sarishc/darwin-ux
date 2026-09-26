# DarwinUX — Observability

## Two Kinds of "Telemetry"

DarwinUX has two things that are both called telemetry. Keep them apart:

| | **Behavioral telemetry** | **Operational telemetry** |
|---|---|---|
| What | What users do in the target app | What DarwinUX itself is doing |
| Examples | clicks, errors, form submits | spans, metrics, logs of API/workers/model calls |
| Pipeline | Telemetry API → SQS → worker → Postgres (DATA_PIPELINES.md) | OpenTelemetry SDK → OTel collector → CloudWatch (this doc) |
| Retention | Part of the product's evidence; long-lived | Operational; days to weeks |
| Used by | Signal detection, experiments | Debugging, alerting, cost control |

## Two Kinds of Record

A second distinction is just as important:

- **Observability signals** (traces, metrics, logs) are **sampled, aggregated, and expire**. They answer "is the system healthy, and where is it slow or broken right now?"
- **Domain audit records** (`AgentRun`, `ModelCall`, `Decision`, `Mutation`, `EvaluationRun`, `Approval`, …) are **complete and permanent** in PostgreSQL. They answer "why did the system do that?"

Never rely on traces for audit. Never put audit-critical data only in logs. The two are linked by IDs: every span carries the relevant domain IDs, and every domain record stores its `trace_id`.

The **Evolution Lab** reads domain records. **CloudWatch** holds observability signals.

## Instrumentation Approach

- **OpenTelemetry SDK** in the FastAPI app and all workers: automatic instrumentation for HTTP, database, and HTTP clients; manual spans for DarwinUX-specific work.
- **Context propagation** across async boundaries: the trace context is placed in SQS message attributes and on DB-handoff rows, so a trace can follow an event from the API through the worker. Long-lived flows (an investigation paused for human approval) are linked with span links rather than one giant trace.
- **Collector:** locally an OTel collector container exporting to a local trace viewer (e.g., Jaeger); on AWS the AWS Distro for OpenTelemetry (ADOT) collector as a sidecar exporting to CloudWatch/X-Ray. The application code is identical; only collector config differs.
- **Attribute naming:** follow OpenTelemetry semantic conventions, including the GenAI conventions (`gen_ai.*`) for model calls where they fit; they are still evolving, so pin a version. DarwinUX-specific attributes use a `darwin.*` prefix.
- **Never put prompts, retrieved text, or user data in span attributes.** Store full payloads via `ModelCall.request_ref`/`response_ref` and put only the ID on the span.
- **Structured JSON logs** with `trace_id` and domain IDs on every line.

## What to Observe, per Component

| Component | Spans | Key metrics | Alert on |
|---|---|---|---|
| **API** | request spans (auto) | request rate, p95 latency, 4xx/5xx rate | 5xx rate; telemetry endpoint p95 > target |
| **Database** | query spans (auto) | connection pool usage, slow queries, storage, CPU | storage > 80%; connections exhausted |
| **Telemetry pipeline** | `darwin.telemetry.process_batch` | queue depth, age of oldest message, DLQ depth, events/s, duplicate rate, signals detected | oldest message age rising; DLQ > 0 |
| **Ingestion pipeline** | `darwin.ingest.document` → parse / chunk / embed | docs processed, chunks created, embed cost per doc, failures | DLQ > 0; embed failure rate |
| **RAG retrieval** | `darwin.rag.retrieve` (query → search → filter → rerank → context) | latency, top-1 score distribution, empty-result rate, context tokens | empty-result rate spike; latency |
| **Model calls (LLM / embeddings)** | `gen_ai.*` spans with provider, model, tokens, status | latency p50/p95, tokens, cost, error/timeout rate, schema-invalid rate | daily cost > budget; schema-invalid spike |
| **Jev decisions** | `darwin.jev.decide` with decision_type, outcome, confidence, adapter | outcome distribution per gate, escalation rate, confidence histogram, error/fallback rate | fallback-to-escalate rate spike (Jev unavailable) |
| **Muse generations** | `darwin.muse.generate` with attempt, adapter, validation result | valid-on-first-attempt rate, attempts per candidate, violation types, latency, cost | validity rate drops |
| **Agents / LangGraph** | one span per node; run-level span per segment | runs started/completed/failed, nodes per run, research loops, tool calls, cost per run, time awaiting human | cost per run > cap; failure rate; runs stuck |
| **Evaluation** | `darwin.eval.run` per evaluator | pass rate per evaluator, judge latency/cost, judge variance | sudden pass-rate shift (evaluator broke, or generator drifted) |
| **Experiments** | `darwin.experiment.analyze` | exposures per variant, sample-ratio mismatch, guardrail values | guardrail breach (also triggers auto-rollback); SRM detected |
| **AWS infrastructure** | — | ECS task CPU/memory/restarts, ALB health, RDS metrics, SQS metrics | unhealthy targets; task crash loops; budget alert |

## Production AI Monitoring

Beyond "is it up", AI components need quality monitoring over time:

- **Cost:** per model call, per AgentRun, per day. Hard per-run budget enforced in the workflow; alarm on daily spend.
- **Drift in inputs:** distribution of signal types and severities; retrieval score distributions (a drop in top-1 scores often means the corpus or embeddings changed).
- **Drift in outputs:** schema-invalid rate, Muse validity rate, Jev outcome mix and escalation rate, LLM-judge score trends.
- **Provider changes:** record the model/version string reported on every call; a change is an event worth annotating on dashboards and re-running calibration for.
- **Online sampling for evaluation:** a small sample of production runs is re-scored asynchronously by LLM judges and queued for occasional human review (EVALUATION_STRATEGY.md).
- **Simulated vs. real:** all metrics derived from experiments carry `traffic_source`, and dashboards never mix the two silently.

## Alerting Philosophy

- Alert on **symptoms that need action** (DLQ has messages, spend over budget, guardrail breach), not on every metric.
- For a one-developer project, alarms → one notification channel (email via SNS is the default path for CloudWatch alarms). No paging rotation.
- Every alert names the runbook step: "DLQ > 0 → inspect message, fix, redrive."

## Local vs. AWS

| | Local | AWS |
|---|---|---|
| Traces | OTel collector → local trace viewer | ADOT collector → CloudWatch/X-Ray |
| Metrics | Collector debug output / trace viewer is enough at first | CloudWatch metrics |
| Logs | stdout (docker compose logs) | CloudWatch Logs with explicit retention |
| AI/audit view | Evolution Lab (reads Postgres) | Evolution Lab (reads Postgres) |

No Grafana/Prometheus stack is planned: it would be one more system to run without teaching anything the CloudWatch path doesn't.

---

## What You Should Understand Before Implementation

1. **Behavioral telemetry is product data; operational telemetry is system health.** Different pipelines, different retention, different consumers.
2. **Traces are not an audit log.** Audit-critical facts live in Postgres; spans carry IDs that point to them.
3. **Context propagation across queues is manual.** Async boundaries break traces unless you carry the context in message attributes yourself.
4. **Never log prompts or user data into observability backends.** Store payloads under access control and reference them by ID.
5. **AI monitoring is mostly distributions over time.** Validity rates, escalation rates, score histograms, and cost — trended — tell you when a model or corpus quietly changed.
6. **Instrument from the first endpoint.** Adding OpenTelemetry at the start is cheap; retrofitting context propagation later is not.
