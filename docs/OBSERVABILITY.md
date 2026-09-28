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

Never rely on traces for audit. Never put audit-critical data only in logs. The two are linked by IDs: every span carries the relevant domain IDs (as built in Step 16). Storing the `trace_id` on every domain record is not done yet (OPEN_QUESTIONS.md N26): today the link runs from trace to record, and from a log line (which carries `trace_id`) to both.

The **Evolution Lab** reads domain records. **CloudWatch** holds observability signals.

## As Built (Step 16)

**Four kinds of record — never interchangeable:**

| | Answers | Lives | Completeness |
|---|---|---|---|
| **Audit records** (PostgreSQL: `user_event`, `research_run`/`research_step`, `decision_run`, `mutation_run`, `candidate_evaluation_run`, `experiment_analysis`, `promotion_approval`, `generation_promotion`, `generation_rollback`, …) | *what DarwinUX decided, and why* | permanent | complete, transactional |
| **Traces** (OpenTelemetry spans) | *how one operation flowed and where time went* | ephemeral | sampled, best effort |
| **Metrics** (OpenTelemetry counters/histograms/gauges) | *is the system healthy over time* | aggregated | bounded labels only |
| **Logs** (JSON lines, `trace_id`/`span_id` when a span is active) | *what happened, line by line* | days | best effort |

Nothing DarwinUX decides depends on tracing. Observability is **off by default** (`DARWIN_OTEL_ENABLED=false`); when on, every OpenTelemetry call is wrapped so a failing exporter, attribute or metric can never change an operation's result (tested: an exporter that fails on every export changes nothing; an unreachable OTLP collector leaves the API and worker fully functional).

**Code boundary.** All instrumentation goes through `darwin.observability` (`span`, `stage`, `record`, `observe_gauge`, `inject_current`/`extract`, `current_ids`). DarwinUX owns its tracer and meter providers (it never calls `trace.set_tracer_provider`), so no library can redirect them and tests can swap them. Exporters are configuration only: `console` (compact one-line spans/metrics on stderr) or `otlp` (OTLP/HTTP). Dependencies: `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` (direct, `<2`); no auto-instrumentation packages (the FastAPI and SQLAlchemy instrumentations can capture full URLs/query strings and SQL, so the HTTP middleware and all DB-level spans are written by hand).

**Service identity.** `darwin-api` (FastAPI), `darwin-worker` (telemetry worker), `darwin-cli` (every human/CLI command); `service.namespace = darwinux`.

### Span vocabulary (stable names)

| Span | Where | Safe attributes |
|---|---|---|
| `{METHOD} {route template}` (SERVER) | every API request except `/api/v1/health/*` | `http.request.method`, `http.route` (template from the OpenAPI paths, `unmatched` otherwise — never the raw path), `http.response.status_code` |
| `telemetry.ingest` | `POST /telemetry/events` handler | `darwin.event.type` (bounded; unknown types → `other`) |
| `queue.enqueue` (PRODUCER) | durable enqueue | `messaging.system`, `darwin.message.type`, `darwin.queue.result` |
| `worker.process` (CONSUMER) | one queue delivery | message type, `darwin.queue.attempt`, `darwin.queue.outcome`, `darwin.trace.context` (continued \| fresh \| invalid) |
| `telemetry.persist`, `signals.reconcile`, `experiment.record_exposure` | inside the worker | ingest result, signal counts, exposure result/reason code |
| `memory.ingest`, `memory.retrieve` | Product Memory | provider, top_k, source-type filter, document/chunk/result counts — never query or chunk text |
| `llm.generate` | the one traced LLM path (`darwin.llm.traced`) | `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens/output_tokens`, request version, status — never prompt, evidence or output |
| `hypothesis.generate` | Step 9 | run id, status, error category |
| `research.run` → `research.node` (one per node execution; loops repeat) | Step 10 | run id, graph version, node, sequence, budgets, retrieval/LLM counters, stop reason |
| `decision.run` | Step 11 | decider, version, decision, status, fail-closed flag |
| `mutation.generate` | Step 12 | generator, version, status, operation count — never values |
| `sandbox.evaluate` → `sandbox.frontend_harness` | Step 13 | evaluator/harness versions, recommendation, each category status |
| `experiment.assign`, `experiment.transition`, `experiment.analyze` | Step 14 | status, variant, fallback reason, transition, assessment — never session ids |
| `promotion.decide`, `generation.promote`, `generation.rollback` | Step 15 | decision, eligibility, page, from/to generation, first refusal code — never reviewer or reason |

### Attribute and label policy

- **Span attributes** pass an explicit allowlist (`observability/attributes.py`). Values must be identifier-shaped (`[A-Za-z0-9_.:/{}-]`, ≤ 128 chars): prose, prompts, payloads, specs and reasons cannot pass; hex tokens and UUIDs are refused except on keys ending in `.id` (research/decision/mutation/evaluation/experiment run ids — useful to jump from a trace to the audit row). A value that fails is dropped, never truncated.
- **Metric labels** are a different thing: every distinct value is a new time series. Each metric has its own label allowlist; values must be short identifiers, never UUIDs, hashes or free text; a bad label drops the measurement rather than widening it. **Never labels:** session/event/run/hypothesis/candidate/experiment ids, experiment keys, trace ids, reviewers, source keys, chunk ids.
- **Errors**: status ERROR with the exception *type* as description and `darwin.error.type`; `record_exception` is never used (its event carries the message, which can contain SQL, URLs or payload fragments). No span events at all.

### Metric vocabulary

| Metric | Kind | Labels |
|---|---|---|
| `darwin.http.server.requests` / `.duration` (ms) | counter / histogram | `http.request.method`, `http.route`, `status_class` |
| `darwin.queue.enqueued` | counter | `message_type`, `result` |
| `darwin.queue.depth` | observable gauge (worker, at export time only) | `state` (pending \| leased \| delayed \| dead) |
| `darwin.worker.messages` / `.duration` (ms) | counter / histogram | `message_type`, `outcome` (acked \| retry \| dead \| lease_lost) |
| `darwin.llm.calls` | counter | `provider`, `request_version`, `status` |
| `darwin.llm.duration` (ms) | histogram | `provider`, `status` |
| `darwin.llm.tokens` | counter | `provider`, `direction` |
| `darwin.stage.runs` / `.duration` (ms) | counter / histogram | `stage` (closed list: memory_ingest … rollback), `outcome` (the stage's own bounded status) |

One `stage` family instead of a metric per pipeline step keeps the instrument count small while every label stays bounded.

### Trace propagation through the queue

The producer injects W3C `traceparent`/`tracestate` into the queue message's own columns (`queue_message.traceparent`, `.tracestate`, migration 0012 — CHECK: W3C format), **never into the telemetry payload** (untrusted client data and part of the product record). The worker extracts it: each **delivery** is one `worker.process` span whose parent is the producer's `queue.enqueue` span, so API → queue → worker is one trace; a **retry** is a sibling span in the same trace with a higher `darwin.queue.attempt` (OpenTelemetry messaging conventions allow parenting when one message is processed per span; span links would be the alternative for batches). A missing context starts a fresh trace; a malformed one is ignored (`darwin.trace.context = invalid`) and can never fail a message. Incoming HTTP `traceparent` headers are ignored: the API is the entry point and an untrusted client must not choose DarwinUX's trace ids.

### Sampling

`ParentBased(TraceIdRatioBased(DARWIN_OTEL_SAMPLE_RATIO))`: the ratio decides for new traces at the API/CLI; the worker follows the producer's decision (carried in `traceparent`). Metrics are not sampled. Sampling never touches audit records. No tail sampling yet.

### Local workflow (no collector, no Docker)

```bash
DARWIN_OTEL_ENABLED=true DARWIN_OTEL_EXPORTER=console make api
DARWIN_OTEL_ENABLED=true DARWIN_OTEL_EXPORTER=console make worker
DARWIN_OTEL_ENABLED=true DARWIN_OTEL_EXPORTER=console make research-run
make observability-eval
```

Each span prints as one line: `[otel] span <service> <name> trace=… span=… parent=… <ms> <status> {attributes}`.

### Future AWS mapping (not deployed)

```
darwin-api ECS task ─┐   OTLP/HTTP    ┌─ ADOT / OpenTelemetry Collector (sidecar or service)
darwin-worker task ──┼──────────────► │     ├─► AWS X-Ray (traces)
darwin-cli (jobs) ───┘                └─    └─► CloudWatch (metrics, logs)
```

Only configuration changes: `DARWIN_OTEL_EXPORTER=otlp`, `DARWIN_OTEL_ENDPOINT=http://localhost:4318` (sidecar), a production sample ratio. No application code changes. Not built in Step 16: collector, Terraform, dashboards, alarms, tail sampling, browser tracing.

## Instrumentation Approach (original design)

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
| **Agents / LangGraph** (Step 10: `research_run` + `research_step` rows already carry every attribute these spans need — run id, graph version, node, sequence, outcome, counters, tokens) | one span per node; run-level span per segment | runs started/completed/failed, nodes per run, research loops, tool calls, cost per run, time awaiting human | cost per run > cap; failure rate; runs stuck |
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

## Promotion and Rollback Logs (Step 15)

Structured log lines (`darwin.generations.service`): `promotion decision recorded` (approval id, decision, page, target generation, reviewer), `generation promoted` (promotion id, approval id, page, from → to, reviewer), `generation rolled back` (rollback id, page, from → to, reviewer), `generation change refused` (page, reason codes) and `promotion refused by the database` (the constraint or trigger). Never logged: specs, analysis reports, raw events, session ids, form values, prompts, reasons typed by the reviewer, credentials. The durable audit trail is the immutable database records, not the logs.

## Alerting Philosophy

- Alert on **symptoms that need action** (DLQ has messages, spend over budget, guardrail breach), not on every metric.
- For a one-developer project, alarms → one notification channel (email via SNS is the default path for CloudWatch alarms). No paging rotation.
- Every alert names the runbook step: "DLQ > 0 → inspect message, fix, redrive."

## Local vs. AWS

| | Local | AWS |
|---|---|---|
| Traces | OTel collector → local trace viewer | ADOT collector → CloudWatch/X-Ray |
| Metrics | Collector debug output / trace viewer is enough at first | CloudWatch metrics |
| Logs | stdout of `make api` / worker processes | CloudWatch Logs with explicit retention |
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
