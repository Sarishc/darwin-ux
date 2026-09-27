# DarwinUX — Data Pipelines

## Two Pipelines, Two Problems

DarwinUX has two fundamentally different data flows:

1. **Telemetry Pipeline** — High-volume user events → behavioral signals (real-time-ish)
2. **RAG Ingestion Pipeline** — Documents → chunks → embeddings → vector store (batch)

A third, smaller flow — **experiment data** — reuses the telemetry pipeline (see the end of this document).

**Terminology:** in this document "telemetry" always means **behavioral telemetry** (what users do in the target app). System instrumentation (traces, metrics, logs about DarwinUX itself) is **operational telemetry** and is covered in OBSERVABILITY.md. They use different pipelines.

These are separate pipelines because they have different volume characteristics, different latency requirements, different failure modes, and different downstream consumers. Combining them would force one pipeline to compromise for the other's constraints.

---

## Pipeline 1: Telemetry Pipeline

### Purpose

Transform raw user interaction events into behavioral signals that can trigger the evolution loop.

### Current Implementation vs. Target Design

The rest of this section describes the **target** pipeline. What exists today is smaller, and there is **no queue yet**:

```
CURRENT (Step 4) — synchronous, one event per request

Client ──POST /api/v1/telemetry/events──> FastAPI route (api/telemetry.py)
                                             │ Pydantic: TelemetryEvent (422 if invalid)
                                             ▼
                                          telemetry.service.ingest_event(session, event)
                                             │ one transaction:
                                             │ INSERT ... ON CONFLICT (event_id) DO NOTHING RETURNING id
                                             ▼
                                          PostgreSQL user_event ──> 202 {event_id, status: accepted | duplicate}


FUTURE — asynchronous

Client ──POST /api/v1/telemetry/events──> FastAPI route (same)
                                             │ Pydantic: TelemetryEvent (same)
                                             ▼
                                          ingest_event(...) now enqueues ──> SQS ──> worker
                                                                                      │ same idempotent INSERT
                                                                                      ▼
                                                                                   user_event ──> signal detection
                                          202 {event_id, status: accepted}
```

What stays the same when the queue arrives: the URL, the `TelemetryEvent` request schema, the `202` status, and the response shape. What changes: `ingest_event` publishes instead of inserting, the idempotent insert moves into the worker, and the API can no longer know synchronously that an event is a duplicate, so it will generally answer `accepted`. That is why the contract tells clients to treat `accepted` and `duplicate` identically.

Not built yet: batching, the queue, workers, signal detection, and the SDK.

### Flow

```mermaid
graph LR
    subgraph Capture["1. Capture"]
        APP["Target Application"]
        SDK["Telemetry SDK (JS)"]
    end

    subgraph Ingest["2. Ingest"]
        API["Telemetry API (FastAPI)"]
        VALIDATE["Validate & Enrich"]
    end

    subgraph Buffer["3. Buffer"]
        SQS["SQS Queue"]
    end

    subgraph Process["4. Process"]
        WORKER["Pipeline Worker"]
        AGG["Event Aggregation"]
        DETECT["Signal Detection"]
    end

    subgraph Store["5. Store"]
        EVENTS["PostgreSQL: user_events"]
        SIGNAL["PostgreSQL: behavior_signals"]
    end

    subgraph Trigger["6. Hand-off"]
        INV["Investigation worker picks up detected signals"]
        TRIAGE["LangGraph: signal_triage (Jev)"]
    end

    DLQ["Dead-letter queue"]

    APP --> SDK --> API --> VALIDATE --> SQS
    SQS --> WORKER
    SQS -.->|after N failures| DLQ
    WORKER -->|idempotent insert| EVENTS
    EVENTS --> AGG --> DETECT
    DETECT --> SIGNAL
    SIGNAL --> INV --> TRIAGE
```

### Stage-by-Stage Design

#### Stage 1: Capture (Frontend — Synchronous)

The target application includes a lightweight JavaScript telemetry SDK that captures user interaction events: clicks, scrolls, navigation, form submissions, errors, timing data.

**Why synchronous:** Event capture must happen in the user's browser at the moment of interaction. This is inherently synchronous with the user's actions. The SDK should be small and non-blocking — it fires events and moves on.

**What the SDK captures:**
- Event ID (client-generated UUID — the idempotency key)
- Event type (click, scroll, navigate, error, etc.)
- Target (stable component ID from the component registry; CSS selector only as fallback)
- UI Spec version / experiment variant the user is seeing
- Timestamp
- Session ID
- Page context (URL, viewport, component tree position)
- Event-specific metadata (click coordinates, scroll depth, error message)

**What the SDK does NOT capture:**
- Personally identifiable information
- Form field values
- Authentication tokens
- Anything that would require consent beyond basic analytics

Free-text fields that can leak personal data (error messages, URLs with query strings) are scrubbed server-side before persistence. For the demo target app all users are synthetic, but the pipeline should be built as if they were not.

#### Stage 2: Ingest (API — Synchronous)

The Telemetry API receives events from the SDK via HTTP POST. (Today: one event per request; batching is a later addition.)

**Why synchronous:** The API must accept or reject the request immediately so the SDK knows whether to retry. This is a thin layer: validate the event schema, add server-owned fields (`received_at`), and hand the event on. No IP address or user agent is stored. Response time target: < 50ms.

**What happens here (target design):**
```
POST /api/v1/telemetry/events
Body: one TelemetryEvent (later possibly a batch)

1. Validate event schema (Pydantic)
2. Reject malformed events with 422
3. Server-owned fields are set by the database/worker (received_at, id)
4. Push to SQS
5. Return 202 Accepted
```

**Why 202, not 200:** The events are accepted for processing, not processed yet. The distinction matters — the client knows the events were received, not that they've been analyzed.

#### Stage 3: Buffer (SQS — Asynchronous)

Events sit in an SQS queue until a worker picks them up.

**Why asynchronous:** This is the critical decoupling point. User-facing telemetry ingestion and computationally expensive signal detection must not be in the same synchronous request path. Reasons:

1. **Back-pressure protection.** If signal detection is slow or failing, the API continues accepting events. Without the queue, API latency would spike and the target application's telemetry would fail.
2. **Rate smoothing.** User events arrive in bursts (page loads, peak hours). The queue absorbs bursts and lets workers process at a sustainable rate.
3. **Retry semantics.** If a worker crashes while processing a batch, SQS re-delivers the messages. Without a queue, those events would be lost.
4. **Independent scaling.** The API and workers can scale independently. More traffic → more API instances. More processing backlog → more workers.

**SQS configuration considerations:**
- Visibility timeout: Long enough for a worker to process a batch (e.g., 5 minutes).
- Dead letter queue: After N failed processing attempts, move messages to a DLQ for inspection.
- Message retention: 4 days (default), enough to survive extended outages.
- Batch size: Process 10 messages at a time for efficiency.

#### Stage 4: Process (Worker — Asynchronous)

Workers poll SQS, process event batches, and detect behavioral signals.

**Why asynchronous:** Processing involves aggregation (grouping events by session, component, time window), statistical computation (click frequency, time-on-task), and pattern matching (rage click detection, abandonment detection). These operations are too expensive for real-time and benefit from batching.

**Processing steps:**
```
1. Poll SQS batch
2. Deserialize events
3. Persist raw events FIRST, idempotently (INSERT ... ON CONFLICT (event_id) DO NOTHING)
4. For each affected (session, component), run detectors over a time window
   of persisted events — not just the events in this batch:
   a. Rage click: 3+ clicks on same element within 2 seconds
   b. Abandonment: Form started but not submitted within session
   c. Repeated error: Same error 3+ times within session
   d. Confusion loop: User navigates away and back 3+ times
   e. Slow completion: Task takes >2σ above mean completion time
5. For each detected pattern:
   a. Upsert BehaviorSignal (same type + component → increment evidence_count, update last_seen)
   b. Status = detected
6. Commit transaction
7. Delete SQS messages (acknowledge processing)
```

**Two properties that make this correct:**

- **Idempotency.** SQS delivers *at least once*. A crashed worker means the same batch is processed twice. Keying events by `event_id` and upserting signals makes a replay harmless.
- **Windowed detection over stored events.** Abandonment or confusion loops span many requests and therefore many SQS messages. A detector that only looks at the current batch would miss them. Detectors query the last N minutes of persisted events for the affected sessions.

**The telemetry worker never calls Jev or an LLM.** It stays cheap, fast, and fully deterministic. Deciding whether to investigate happens in the investigation workflow.

**Signal detection is deterministic.** This is critical. Rage click detection is a counting problem, not a language understanding problem. Deterministic detection means deterministic testing, which means high reliability.

#### Stage 5: Store (PostgreSQL — Synchronous within worker)

Processed events and detected signals are persisted to PostgreSQL.

**Why synchronous (within the worker):** Database writes within the worker are synchronous because the worker needs to confirm persistence before acknowledging the SQS message. If the write fails, the message should be retried.

**Two storage concerns:**
1. **Raw events** — For audit, replay, and future analysis. Append-only, potentially high volume. Consider partitioning by time.
2. **Behavioral signals** — The meaningful output. Lower volume, actively queried by the agent system.

#### Stage 6: Hand-off to Investigation (Asynchronous)

Detected signals that pass a deterministic threshold (severity, evidence count, not already under investigation) are picked up by the **investigation worker**, which starts an AgentRun. The first graph node, `signal_triage`, asks the Jev port whether to proceed.

**Why asynchronous:** Agent runs are expensive (multiple LLM calls, RAG retrieval, evaluation) and may take minutes. They absolutely cannot be in the telemetry processing path.

**Triggering mechanism options:**
- Fire-and-forget async task from the telemetry worker — **rejected**: if the process dies, the investigation is silently lost, and it couples two workloads with very different cost profiles.
- Second SQS queue for investigation requests — resilient, but another queue to operate.
- **The signal row is the trigger** — the investigation worker periodically selects `detected` signals above threshold (`SELECT ... FOR UPDATE SKIP LOCKED`) and marks them `investigating`.

**Recommendation:** Use the database as the hand-off in v1. It is durable, transactional, needs no extra infrastructure, and the extra latency (seconds to a minute) is irrelevant for an investigation that takes minutes. Add a dedicated queue only if polling becomes a measured problem.

---

## Pipeline 2: RAG Ingestion Pipeline

### Purpose

Transform documents and artifacts into searchable, embeddable knowledge chunks in Product Memory.

### Flow

```mermaid
graph LR
    subgraph Source["1. Source"]
        UPLOAD["Manual Upload"]
        SYNC["Automated Sync"]
        SYSTEM["System-Generated"]
    end

    subgraph Parse["2. Parse"]
        LOADER["Document Loader"]
        EXTRACT["Text Extraction"]
        META["Metadata Extraction"]
    end

    subgraph Transform["3. Transform"]
        NORM["Normalize"]
        CHUNK["Chunk"]
        ENRICH["Enrich Metadata"]
    end

    subgraph Embed["4. Embed"]
        EMBED["Embedding Model"]
        BATCH["Batch Processing"]
    end

    subgraph Index["5. Index"]
        PG["PostgreSQL + pgvector: chunks"]
        REG["PostgreSQL: KnowledgeDocument registry"]
    end

    RAW["S3: raw document (immutable)"]
    HASH{"Content hash already indexed?"}
    SKIP(["Skip"])

    UPLOAD --> RAW
    SYNC --> RAW
    SYSTEM --> RAW
    RAW --> HASH
    HASH -->|yes| SKIP
    HASH -->|no| LOADER
    LOADER --> EXTRACT --> META
    META --> NORM --> CHUNK --> ENRICH
    ENRICH --> EMBED --> BATCH
    BATCH --> PG
    BATCH --> REG
```

### Stage-by-Stage Design

#### Stage 1: Source Acquisition (Mixed)

Documents enter through three paths:

| Path | Trigger | Example |
|---|---|---|
| Manual upload | Human action (API call or UI) | Engineer uploads new design system docs |
| Automated sync | Scheduled job (cron) | Pull latest component docs from design system repo |
| System-generated | Internal event | DarwinUX creates an experiment report after an experiment concludes |

**Synchronous for manual upload:** The user clicks "upload" and expects immediate feedback (accepted/rejected). Parsing and embedding happen asynchronously after acceptance.

**Asynchronous for automated sync and system-generated:** These run on schedules or internal triggers. No human is waiting for immediate feedback.

#### Stage 2: Parse & Extract (Asynchronous)

**Why asynchronous:** Parsing can be slow (PDF text extraction, HTML rendering, large documents). It should not block the upload response.

**Processing:**
```
1. Detect document format (markdown, PDF, HTML, JSON, etc.)
2. Select appropriate parser
3. Extract text with structure preservation:
   - Heading hierarchy
   - Section boundaries
   - Table structure
   - Code blocks
4. Extract metadata:
   - Title
   - Author (if available)
   - Date
   - Document type classification
5. Store raw document to S3 (for re-processing if parser improves)
6. Create KnowledgeDocument entity in PostgreSQL
```

**LangChain usage:** LangChain's document loaders (PyPDFLoader, UnstructuredMarkdownLoader, etc.) are well-tested for this stage and provide genuine value. This is an area where the library saves meaningful development time.

#### Stage 3: Transform (Asynchronous)

**Why asynchronous:** Transformation is a batch operation that may process many chunks from a single document. It has no real-time requirement.

**Normalization:**
- Unicode normalization (NFC)
- Whitespace standardization
- Encoding verification

**Chunking:**
```
1. Attempt section-based chunking (split on headings)
2. For sections > target size (500 tokens):
   a. Apply recursive character splitting
   b. Maintain 50-token overlap between chunks
3. For sections < minimum size (100 tokens):
   a. Merge with adjacent section
4. Assign chunk metadata:
   - Parent document ID
   - Section heading
   - Chunk index within document
   - Token count
```

**Metadata enrichment:**
- Document type tag
- Component reference (if the document is about a specific UI component)
- Freshness timestamp
- Source credibility tag (official docs > informal notes)

#### Stage 4: Embed (Asynchronous, Batch)

**Why asynchronous and batched:** Embedding API calls have rate limits and per-token costs. Batching chunks (e.g., 100 at a time) is more efficient than embedding one at a time. This stage is the most expensive per-token operation in the pipeline.

**Processing:**
```
1. Collect chunks into batches (batch size = 100 or provider limit)
2. Call embedding provider for each batch
3. Receive vectors
4. Associate each vector with its chunk
5. Record embedding model version (for future re-embedding)
```

**Failure handling:** If an embedding batch fails, retry the batch. If a specific chunk consistently fails (e.g., too long), log it and skip. Never let one bad chunk block the entire pipeline.

#### Stage 5: Index (Asynchronous)

**Why asynchronous:** Index updates can be batched and don't need to be visible immediately.

**Processing:**
```
1. Insert chunk + embedding into pgvector
2. Update KnowledgeDocument status to "indexed"
3. Update document registry (metadata, chunk count, last indexed)
4. If replacing an existing document version:
   a. Soft-delete old chunks
   b. Index new chunks
   c. Update document content hash
```

**Deduplication:** The content hash is checked **before parsing** (see diagram). If a document with the same hash already exists and is already indexed, skip re-processing. This prevents waste when automated sync pulls unchanged documents.

---

## Pipeline Comparison

| Characteristic | Telemetry Pipeline | RAG Ingestion Pipeline |
|---|---|---|
| **Volume** | High (thousands of events/minute at scale) | Low (tens of documents/day) |
| **Latency requirement** | Minutes (signal detection can lag) | Hours (indexing is not time-critical) |
| **Processing cost** | Low (counting, pattern matching) | High (parsing, embedding API calls) |
| **Failure impact** | Missed signals (recoverable) | Missing knowledge (noticeable but not critical) |
| **Trigger** | Continuous (user activity) | Event-driven (uploads, syncs) |
| **Queue** | SQS (essential for decoupling) | SQS in AWS; low volume means a simple job table or the same local queue emulator is fine in development |
| **Downstream consumer** | Agent system (via Jev gate) | RAG retrieval (via vector search) |

### Why Not One Pipeline?

It might seem simpler to have a single "data pipeline" that handles both telemetry and documents. Here's why that's a bad idea:

1. **Different SLAs.** Telemetry must be processed within minutes. Documents can wait hours. Combining them forces the slow path to meet fast-path SLAs or lets the fast path be slowed by the slow path.
2. **Different scaling.** Telemetry scales with user count. Document ingestion scales with content creation rate. These are unrelated.
3. **Different failure handling.** A telemetry processing failure should be retried quickly (events are time-sensitive). A document parsing failure can wait for manual review.
4. **Different testing.** Telemetry pipeline tests are about counting and pattern matching. RAG pipeline tests are about chunking quality and embedding correctness. Separate pipelines have clearer test boundaries.

---

## Experiment Data Flow

Experiments do not need a third pipeline. They reuse the telemetry pipeline:

1. The Experiment Manager (deterministic) assigns each session to control or treatment by hashing `session_id` with the experiment's `flag_key` — stable, stateless assignment.
2. The target app asks the flag service which UI Spec version to render and includes `ui_spec_version_id` on every event.
3. Events flow through the normal telemetry pipeline.
4. A scheduled analysis job (deterministic) computes the pre-registered primary metric and guardrail metrics per variant from `user_events`.
5. On a guardrail breach, the job flips the flag back to control (automatic rollback). Otherwise, when the planned sample size is reached, the Experiment moves to `analyzing` and a human decides on promotion.

**Traffic source.** A portfolio project has no real users. Experiments will initially run on a **synthetic user simulator** (scripted personas driving the demo app in a headless browser, with friction deliberately built into Generation 0). Simulated results are labelled `traffic_source = simulated` everywhere and must never be presented as real UX evidence. See OPEN_QUESTIONS.md.

---

## Local vs. AWS

| Concern | Local development | AWS (later) |
|---|---|---|
| Queue | To be decided when the pipeline is built — without Docker (e.g., a native SQS-compatible emulator or a Postgres-backed queue behind the same port) | SQS + DLQ |
| Database | Native Homebrew PostgreSQL 17 + pgvector | RDS for PostgreSQL 17 with pgvector |
| Raw documents | Local directory or S3-compatible emulator | S3 |
| Workers | `python -m darwin.workers <name>` processes | ECS/Fargate services from the same image |

The code talks to a queue port and a storage port, so switching is configuration, not a rewrite.

---

## What You Should Understand Before Implementation

1. **The SQS queue in the telemetry pipeline exists for resilience, not performance.** Even if you could process events synchronously in time, the queue protects the user-facing application from pipeline failures and enables independent scaling.
2. **Signal detection is deterministic by design.** An LLM cannot reliably count "3 clicks in 2 seconds." Rules-based detection is faster, cheaper, testable, and more reliable. Jev triages after detection, in the investigation workflow; it never performs detection and the telemetry worker never calls it.
3. **At-least-once delivery means every consumer must be idempotent.** Persist first, key by `event_id`, upsert signals. A replayed batch must change nothing.
4. **The RAG pipeline's most expensive step is embedding, not parsing.** Budget API costs and rate limits when planning batch sizes. Hash documents before parsing and record the embedding model on every chunk so unchanged content is never reprocessed.
5. **These pipelines will be the first things you build.** They are prerequisites for the agent system (which needs behavioral signals) and for RAG retrieval (which needs indexed chunks). Get them right and tested before building the AI layer.
6. **Synchronous vs. asynchronous is a question of "who is waiting?"** If a human or a user-facing system is waiting for the result, it must be fast (synchronous or fast-async). If nothing is waiting, batch it for efficiency.
