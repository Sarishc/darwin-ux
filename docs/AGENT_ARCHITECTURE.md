# DarwinUX — Agent Architecture

## Design Principle: Not Everything Should Be an Agent

The most common mistake in AI systems is making everything an LLM-powered agent. An "agent" implies autonomous decision-making, tool use, and iterative reasoning — capabilities that come with latency, cost, unpredictability, and evaluation difficulty.

**The right question is not "what agents do we need?" but "what responsibilities exist, and what is the simplest correct implementation for each?"**

The spectrum of implementation options, from simplest to most complex:

| Implementation | Characteristics | When to Use |
|---|---|---|
| **Deterministic Python** | Fast, testable, predictable, free | Logic with clear rules and no ambiguity |
| **Template + rules** | Configurable, auditable | Structured output from structured input |
| **Single LLM call** | Flexible text understanding/generation | One-shot tasks requiring language understanding |
| **Jev decision** | Classification, scoring, confidence-aware gating | Decision points requiring confidence-aware gating |
| **Muse generation** | Structured creative output within constraints | Producing candidate mutations from approved hypotheses |
| **LangGraph node** | Stateful, conditional routing, retries | Steps that depend on previous results |
| **LLM agent with tools** | Autonomous multi-step reasoning | Tasks requiring iterative investigation |

## Current Implementation (Step 9)

Everything else in this document is design. This is what exists today: **one bounded, structured LLM call** that turns one BehaviorSignal into at most one validated Hypothesis. No LangGraph, no agent loop, no tools, no Jev, no Muse, no mutations.

```
BehaviorSignal (canonical only)
  → deterministic retrieval query            darwin/hypotheses/queries.py
  → Product Memory retrieval, top_k = 5      darwin/memory/retrieval.py (Step 8, read-only)
  → EvidenceBundle (safe signal facts +      darwin/hypotheses/evidence.py
      excerpts ≥ relevance floor, untrusted)
      └─ no excerpts → run "insufficient_evidence", the model is NOT called
  → hypothesis.v1 request                    darwin/hypotheses/prompt.py
      trusted instructions | untrusted evidence (separate fields, hash-tagged delimiters)
  → LLMProvider.generate_structured (once)   darwin/llm/port.py — FakeLLMProvider only
  → parse → strict schema → grounding        darwin/hypotheses/schema.py
  → HypothesisRun (always) + Hypothesis (only if valid), one transaction
```

**Provider.** DarwinUX owns the port (`StructuredGenerationRequest`, `StructuredGenerationResult`, `Usage`, three error types). The only implementation is `FakeLLMProvider`, deterministic, with one mode per failure it must prove is caught (malformed output, unknown fields, fake probabilities, overlong text, prose, hallucinated evidence ids, invalid components, obeying an injection, failure, timeout, unavailable). No real provider was added: the provider choice (OPEN_QUESTIONS.md N1) should be made against this evaluation harness, and nothing in Step 9 needs a real model to prove the control layer. A real adapter will live beside `fake.py`, use the vendor's official SDK, keep instructions and evidence in separate message roles, treat missing credentials as `ProviderUnavailableError` (feature unavailable, not a startup failure), and return raw text that DarwinUX validates exactly as it validates the fake's.

**Output contract (`HypothesisDraft`).** `statement` (one line, 20–400 chars), `rationale` (40–1500), `affected_component` (an allowed component id, or null), `confidence` (`low | medium | high` — an **uncalibrated** qualitative judgment, never a probability), `evidence_chunk_ids` (1–5 unique ids), `limitations` (1–5). Unknown fields are forbidden (so there is nowhere to put code or a mutation), types are strict, whitespace is stripped before length checks, code fences and HTML tags are rejected.

**Grounding (deterministic).** Every cited id must be an excerpt id from this run's EvidenceBundle; the component must be the signal's own component (rage_click) or a component declared in the Generation 0 UI Spec (error_burst, whose signal names none). This is citation *validity*, not truthfulness: a hypothesis can cite a real excerpt and still misread it. Semantic faithfulness needs a judge (see EVALUATION_STRATEGY.md) and is not claimed.

**Failure semantics** (`hypothesis_run.status` / `error_type`): `insufficient_evidence` (`no_context`, `low_relevance` — a controlled terminal outcome, not an error: the model is never asked to invent), `provider_unavailable`, `provider_error` (`failure`, `timeout`, `unexpected:<Class>`), `invalid_output` (`not_json`, `missing`, `extra_forbidden`, `literal_error`, `string_too_long`, …), `grounding_failed` (`unknown_evidence_reference`, `invalid_component`). Every outcome is a persisted run; only `succeeded` has a Hypothesis. An unknown or superseded signal raises `SignalNotFoundError` and records nothing (there is no signal to attach it to).

**Repeat calls.** Every explicit generation is a new run. Model calls are neither deterministic nor free, so nothing pretends to deduplicate them; the fake is deterministic only so tests can be exact.

**Still future:** the Research agent (LLM-written queries, retrieval loops), the Critic, LangGraph orchestration, Jev gates, Muse mutation generation, LLM-as-judge evaluation, a real provider.

## Responsibility Analysis

### 1. Signal Detection (Observer)

**Proposed name:** Observer Agent

**What it actually does:**
- Receives processed telemetry data
- Identifies behavioral patterns (rage clicks, abandonment, repeated errors)
- Classifies signal severity
- Decides whether to trigger investigation

**Should it be an agent?** **No.**

**Recommended implementation:** Deterministic Python + Jev classification.

**Reasoning:**
- Pattern detection (e.g., "3+ clicks on same element within 2 seconds") is purely rule-based. An LLM cannot reliably count events or compute time deltas.
- Signal triage (is this signal worth investigating?) is a confidence-aware decision — Jev's intended role — but does not require multi-step reasoning. Whether Jev's confidence is calibrated must be measured, not assumed.
- This runs on every batch of processed events. Latency and cost must be minimal.

**Implementation sketch:**
```
Telemetry events
  → Deterministic pattern matching (Python, telemetry worker)
  → BehaviorSignal created (status: detected)
  → Deterministic threshold: severity/evidence_count high enough? (else stays logged)
  → Investigation worker starts AgentRun
  → Graph node signal_triage: JevProvider.decide(signal_triage) → proceed | stop | escalate
```

**What you learn:** Signal detection rules, threshold tuning, designing a decision port before the real provider exists.

---

### 2. Evidence Research (Research Agent)

**Proposed name:** Research Agent

**What it actually does:**
- Receives a behavioral signal
- Formulates retrieval queries
- Searches Product Memory
- Assesses whether retrieved evidence is sufficient
- Summarizes relevant findings

**Should it be an agent?** **Yes — but a constrained one.**

**Recommended implementation:** LangGraph node with RAG tools.

**Reasoning:**
- Query formulation benefits from language understanding ("rage_click on checkout" → "checkout button design guidelines, error states, past experiments with checkout").
- Evidence assessment ("is this enough to form a hypothesis?") requires judgment that rules cannot easily encode.
- However, the agent should not have access to arbitrary tools. Its tools are read-only: `search_product_memory` and `get_document_details`. (Summarizing evidence is the agent's own final LLM output, not a tool.) No tool can write, deploy, or call Muse.

**Why LangGraph:** The research agent may need to iterate — if the first retrieval query returns insufficient results, it should reformulate and try again. LangGraph's conditional edges support this loop with explicit bounds (max 3 retrieval attempts).

**Implementation sketch:**
```
LangGraph node: research_agent
  Tools: [search_product_memory, get_document_details]   # read-only, deterministic
  Input: BehaviorSignal
  Output: EvidencePackage { chunks: [...], summary: str, sufficient: bool }
  Max iterations: 3
  On insufficient evidence after max iterations: escalate to human
```

**What you learn:** RAG query formulation, tool calling, LangGraph iteration loops, retrieval evaluation.

---

### 3. Hypothesis Formation (Hypothesis Agent)

> **Implemented (Step 9)** as a single structured call — see "Current Implementation (Step 9)" above. The sketch below used `confidence: float`; the implementation uses a qualitative `low | medium | high` enum instead, because a model-invented probability looks calibrated and is not.

**Proposed name:** Hypothesis Agent

**What it actually does:**
- Receives a behavioral signal + evidence package
- Proposes an explanation for the observed friction
- Cites specific evidence supporting the hypothesis
- Assesses confidence

**Should it be an agent?** **No — a single structured LLM call is sufficient.**

**Recommended implementation:** Single LLM call with structured output.

**Reasoning:**
- Hypothesis formation is a single reasoning step: "Given this signal and this evidence, what explains the friction?" This does not require tools, iteration, or multi-step planning.
- Structured output (Pydantic model) ensures the hypothesis has the required fields: description, evidence citations, confidence, proposed area of improvement.
- Making this an agent adds latency and cost without adding capability.

**Implementation sketch:**
```python
# Conceptual — not production code
class HypothesisOutput(BaseModel):
    description: str
    evidence_citations: list[str]
    confidence: float
    target_component: str
    proposed_improvement_area: str

hypothesis = await llm.generate_structured(
    prompt=hypothesis_prompt(signal, evidence),
    schema=HypothesisOutput
)
```

**What you learn:** Structured generation, Pydantic output schemas, prompt design for reasoning tasks.

---

### 4. Hypothesis Critique (Critic)

**Proposed name:** Critic Agent

**What it actually does:**
- Reviews a hypothesis for logical consistency
- Checks whether evidence actually supports the claims
- Identifies alternative explanations
- Flags potential issues

**Should it be an agent?** **No — a single LLM call with a different prompt persona.**

**Recommended implementation:** Single LLM call (different model or prompt from hypothesis generation).

**Reasoning:**
- Critique is a single evaluation step, not a multi-step investigation.
- Using a different model (or the same model with a critique-focused prompt) provides genuine value — it catches reasoning errors, unsupported claims, and overlooked alternatives.
- The value is in the separation of concerns (generator vs. critic), not in agent machinery.

**Design note:** Consider using a different LLM provider for the critic than for hypothesis generation. This provides genuine diversity of reasoning, not just prompt-level role-playing.

**Implementation sketch:**
```
LangGraph node: critique_hypothesis
  Input: Hypothesis + EvidencePackage
  Output: CritiqueResult { approved: bool, concerns: list[str], alternative_explanations: list[str] }
  Implementation: Single LLM call, structured output
```

**What you learn:** LLM-as-judge patterns, generator-critic architectures, cross-model evaluation.

---

### 5. Mutation Generation (Muse)

**Proposed name:** ~~Mutation Agent~~ → **Muse** (generative mutation layer)

**What it actually does:**
- Receives an approved, evidence-backed hypothesis
- Receives product context, design system constraints, and component constraints
- Receives the allowed mutation boundary (what properties can change, valid ranges)
- Generates a structured candidate mutation specification

**Should it be an agent?** **No — Muse is a dedicated generative model behind a provider boundary.**

**Recommended implementation:** Muse provider call + deterministic validation.

**Reasoning:**

Muse is treated as a distinct subsystem — the **generative mutation layer** — alongside Jev (decision layer), RAG (evidence layer), LangGraph agents (reasoning/orchestration layer), and the Evaluation Engine (quality/safety layer).

The critical distinction from the original "Mutation Agent" concept:

| Aspect | Generic LLM "Mutation Agent" | Muse as Generative Layer |
|---|---|---|
| **Model** | Whichever general-purpose LLM is configured | Muse (provider and model details: see OPEN_QUESTIONS.md) |
| **Input** | Freeform prompt with hypothesis | Structured input: hypothesis + context + constraints |
| **Output** | Raw text requiring parsing | Structured candidate mutation |
| **Boundary** | Part of the agent orchestration | Its own provider with its own abstraction |
| **Evaluation** | Evaluated as "did the agent do a good job?" | Evaluated as "is this candidate mutation valid, safe, and good?" |

**Why a provider boundary, not an agent:** Muse's provider, API surface, input format, and capabilities are not documented in this repository. Nothing here should be read as a claim about how Muse works internally. Wrapping it in a provider boundary means:
- The rest of the system can develop and test against a mock Muse provider.
- When real integration details become available, only the provider implementation changes.
- Muse can be independently evaluated, versioned, and monitored.

**Muse's output is always a CANDIDATE.** It never directly deploys. The pipeline after Muse:

```
Muse generates candidate
  → Constrained mutation representation (structured spec)
  → Sandbox rendering (visual verification)
  → Deterministic validation (schema, allowlist, value ranges)
  → AI evaluation (accessibility, design consistency, regression)
  → Jev decision gate (proceed to experiment?)
  → Human approval
  → Experiment
```

**Implementation sketch:**
```
Deterministic step: build_mutation_context   # "mutation planning" is NOT an LLM step
  - Load active UI Spec version for the target screen
  - Look up allowed props / token ranges in the component registry
  - Attach retrieved evidence + hypothesis
  → MutationContext + MutationConstraints

LangGraph node: generate_mutation_via_muse
  Input: ApprovedHypothesis + MutationContext + MutationConstraints
  Call: muse_provider.generate_mutation(hypothesis, context, constraints)
  Output: CandidateMutation
  Post-processing: Parse into MutationSpec (schema) — anything unparseable counts as a failed attempt
  Loop: If validation fails, retry with constraint feedback (max 3 attempts)
  On repeated failure: reject with reasoning, log for Muse evaluation
```

**What you learn:** Provider abstraction for external models with unknown interfaces, constrained generation, structured input/output contracts, candidate-vs-deployment distinction, evaluation of generative model quality.

---

### 6. Safety Validation (Safety Agent)

**Proposed name:** Safety Agent

**What it actually does:** (in the graph this is the `safety_check` node, after `sandbox_render`)
- Checks mutations against safety constraints
- Verifies no forbidden properties are modified
- Validates accessibility requirements
- Checks for regressions against known issues

**Should it be an agent?** **No — deterministic validation with optional LLM accessibility check.**

**Recommended implementation:** Deterministic Python rules + optional LLM accessibility assessment.

**Reasoning:**
- "Does this mutation modify authentication?" is a deterministic check against the mutation specification schema.
- "Does this mutation modify only allowed properties?" is a schema validation.
- "Is this color change accessible?" may benefit from an LLM check against WCAG guidelines, but only after deterministic contrast ratio checks pass.
- Safety validation must be fast, reliable, and auditable. LLM calls introduce uncertainty. Use them only for checks that rules cannot perform.

**Implementation sketch:**
```
Safety pipeline (sequential, all must pass):
  1. Schema validation (deterministic): mutation conforms to MutationSpec
  2. Allowlist check (deterministic): only permitted properties modified
  3. Value range check (deterministic): values within valid ranges
  4. Accessibility check (deterministic + optional LLM): contrast ratios, font sizes
  5. Regression check (deterministic): no known problematic patterns
```

**What you learn:** Validation pipeline design, defense-in-depth, separating mechanical checks from judgment.

---

### 7. Evaluation Orchestration (Evaluation Agent)

**Proposed name:** Evaluation Agent

**What it actually does:**
- Orchestrates evaluation of mutations across multiple dimensions
- Runs deterministic evaluators and LLM-as-judge evaluators
- Aggregates scores
- Determines overall pass/fail

**Should it be an agent?** **No — a deterministic orchestrator.**

**Recommended implementation:** Python orchestration code that runs evaluators in parallel and aggregates results.

**Reasoning:**
- Which evaluators to run is determined by the mutation type (a color change needs accessibility evaluation; a copy change needs readability evaluation). This is rule-based.
- Running evaluators is embarrassingly parallel and doesn't require reasoning.
- Aggregating scores into a pass/fail decision follows configured thresholds.
- Some individual evaluators use LLM calls (LLM-as-judge), but the orchestration layer is deterministic.

**Implementation sketch:**
```
Evaluation orchestrator:
  1. Determine required evaluators based on mutation type (deterministic)
  2. Run evaluators in parallel:
     - Schema compliance (deterministic)
     - Accessibility (deterministic + LLM)
     - Design consistency (LLM-as-judge)
     - Regression (deterministic)
  3. Aggregate scores (deterministic)
  4. Apply pass/fail thresholds (deterministic)
  5. Return EvaluationResult
```

**What you learn:** Parallel execution, evaluation framework design, score aggregation, threshold configuration.

---

### 8. System Monitoring (Monitoring Agent)

**Proposed name:** Monitoring Agent

**What it actually does:**
- Tracks system health metrics
- Monitors experiment progress
- Detects anomalies in AI behavior (model drift, cost spikes)
- Alerts on failures

**Should it be an agent?** **No — standard monitoring infrastructure.**

**Recommended implementation:** OpenTelemetry instrumentation + CloudWatch alarms + periodic health check jobs. Experiment guardrail monitoring (auto-rollback) is deterministic code in the Experiment Manager. See OBSERVABILITY.md.

**Reasoning:**
- Monitoring is a solved problem with mature tooling. Using an LLM to "monitor" adds latency, cost, and unreliability to a system that must be the most reliable part of the platform.
- Anomaly detection can be done with statistical methods (standard deviation alerts, cost threshold checks).
- The only place an LLM might add value is in generating human-readable incident summaries — but this is a nice-to-have, not a core requirement.

**What you learn:** OpenTelemetry, CloudWatch configuration, alerting strategy, health check design.

---

## Summary: Responsibility Classification

Every responsibility is classified as exactly one primary kind: **deterministic Python**, **LLM call**, **LangGraph node** (a unit of orchestration that wraps one of the others), **tool**, **Jev decision**, **Muse generation**, or **human decision**.

| Responsibility | Classification | Runs where | Why |
|---|---|---|---|
| Event validation & persistence | Deterministic Python | Telemetry worker | Schema + idempotent insert |
| Signal detection (Observer) | Deterministic Python | Telemetry worker | Counting over time windows |
| Signal triage | Jev decision (graph node `signal_triage`) | Investigation worker | Confidence-aware go/no-go |
| Evidence research | LangGraph node containing the **only LLM agent** | Investigation worker | Needs iterative query reformulation |
| Product Memory search | Tool (`search_product_memory`) — deterministic retrieval | Called by Research agent | Retrieval is search, not reasoning |
| Hypothesis formation | LLM call (structured output), graph node `hypothesize` | Investigation worker | One-shot reasoning |
| Hypothesis critique | LLM call (structured output), graph node `critique` | Investigation worker | One-shot evaluation, generator/critic split |
| Evidence sufficiency | Jev decision, graph node `evidence_gate` | Investigation worker | Confidence-aware gating |
| Mutation planning (context + constraints) | Deterministic Python, graph node `build_mutation_context` | Investigation worker | Registry lookup; must not be improvised by a model |
| Mutation generation | Muse generation, graph node `generate_mutation` | Investigation worker | Constrained generative capability |
| Sandbox rendering | Deterministic Python (headless render), graph node `sandbox_render` | Investigation worker | Reproducible artefacts (screenshots, DOM, a11y scan) |
| Safety validation | Deterministic Python, graph node `safety_check` | Investigation worker | Safety must never be probabilistic |
| Evaluation orchestration | Deterministic Python (some evaluators use LLM-as-judge) | Investigation worker | Rule-based evaluator selection + aggregation |
| Experiment readiness | Jev decision, graph node `experiment_gate` | Investigation worker | Highest-stakes AI gate |
| Approval to experiment | **Human decision** (graph interrupt) | Evolution Lab | Accountability |
| Escalation review | **Human decision** (graph interrupt) | Evolution Lab | Jev uncertain or unavailable |
| Experiment assignment & statistics | Deterministic Python | Experiment Manager | Math, not judgment |
| Guardrail monitoring & auto-rollback | Deterministic Python | Experiment Manager | Moving toward safety needs no approval |
| Promotion to new Generation | **Human decision** (Jev advisory) | Evolution Lab | Accountability |
| Experiment report → Product Memory | Deterministic record + optional LLM summary | Ingestion worker | Summary is nice-to-have; facts are stored deterministically |
| System monitoring | Deterministic (OTel + CloudWatch) | Infrastructure | Solved problem |

**Result:** Of eight proposed "agents," only one (Research) is genuinely an autonomous agent with tools. Mutation generation is handled by Muse — a dedicated generative model behind a provider boundary, not an agent. The rest are deterministic code, single LLM calls, or Jev decisions.

**This is the correct outcome.** The five AI subsystems each have a clear role:

| Subsystem | Role |
|---|---|
| **RAG / Product Memory** | Evidence — what do we know? |
| **LangGraph workflow** | Reasoning / orchestration — investigate and hypothesize |
| **Jev** | Decision — should we proceed? |
| **Muse** | Generation — produce a candidate mutation |
| **Evaluation Engine** | Quality — is the candidate good enough? |

## LangGraph Orchestration

Even though most components are not agents, LangGraph still provides value as the orchestration layer for the investigation flow. The legend on each node shows what kind of work it does.

```mermaid
graph TD
    START(["Signal passed deterministic threshold"]) --> TRIAGE{"signal_triage [Jev]"}
    TRIAGE -->|stop| END_DISCARD(["End: signal logged"])
    TRIAGE -->|escalate| HUMAN_ESC["human_review [Human interrupt]"]
    TRIAGE -->|proceed| RESEARCH["research [LLM agent]"]

    RESEARCH <-->|tool calls| RAG[("search_product_memory [Tool: RAG retrieval]")]
    RESEARCH --> HYPOTHESIZE["hypothesize [LLM call]"]
    HYPOTHESIZE --> CRITIQUE["critique [LLM call]"]
    CRITIQUE -->|rejected| END_REJECT(["End: hypothesis archived"])
    CRITIQUE -->|approved| GATE{"evidence_gate [Jev]"}

    GATE -->|need_more_evidence, max 2 loops| RESEARCH
    GATE -->|escalate| HUMAN_ESC
    GATE -->|stop| END_REJECT
    GATE -->|proceed| PLAN["build_mutation_context [Deterministic]"]

    HUMAN_ESC -->|continue| PLAN
    HUMAN_ESC -->|stop| END_REJECT

    PLAN --> MUSE["generate_mutation [Muse]"]
    MUSE -->|unparseable, retry max 3| MUSE
    MUSE -->|3 failures| END_FAIL(["End: generation failed"])
    MUSE -->|MutationSpec| SANDBOX["sandbox_render [Deterministic]"]

    SANDBOX --> SAFETY{"safety_check [Deterministic]"}
    SAFETY -->|violation, retry with feedback| MUSE
    SAFETY -->|hard violation| END_UNSAFE(["End: safety rejected"])
    SAFETY -->|passed| EVALUATE["evaluate [Deterministic orchestrator + LLM judges]"]

    EVALUATE --> DECIDE{"experiment_gate [Jev]"}
    DECIDE -->|stop| END_LOW(["End: archived with scores"])
    DECIDE -->|proceed or escalate| APPROVAL["request_approval [Human interrupt]"]

    APPROVAL -->|rejected| END_HUMAN(["End: human rejected"])
    APPROVAL -->|approved| HANDOFF(["End: handed to Experiment Manager"])
```

**Human decision boundaries** are LangGraph *interrupts*: the graph checkpoints its state to PostgreSQL and stops. The Evolution Lab shows the pending decision; the human's answer resumes the graph. No thread or process waits for a human.

**The graph ends at approval.** Running an experiment takes days. That is not a workflow step to hold open; it is a deterministic lifecycle owned by the Experiment Manager (DATA_PIPELINES.md, "Experiment Data Flow"). Post-experiment analysis and the promotion decision happen outside the graph.

**Every loop is bounded** (research ≤ 2 extra loops, Muse ≤ 3 attempts total including safety retries). Unbounded loops are the most common way agent systems burn money.

**Why LangGraph for orchestration:** The investigation flow has conditional branches, loops (research → hypothesize → need more evidence → research again), and human-in-the-loop requirements. LangGraph's state graph model handles these naturally. Without it, you'd write a complex state machine in plain Python — which is possible but less maintainable as the flow evolves.

**LangGraph state:** The graph maintains state across nodes:
```python
# Conceptual
class InvestigationState(TypedDict):
    signal: BehaviorSignal
    evidence: Optional[EvidencePackage]
    hypothesis: Optional[Hypothesis]
    critique: Optional[CritiqueResult]
    mutation_context: Optional[MutationContext]     # Deterministic
    candidate_mutation: Optional[CandidateMutation]  # From Muse
    sandbox_result: Optional[SandboxResult]
    evaluation: Optional[EvaluationResult]
    decisions: list[Decision]
    research_loops: int
    muse_attempts: int
```

Note that `candidate_mutation` is explicitly named to reinforce that Muse produces candidates, not deployable changes. The state tracks the full progression: evidence → hypothesis → critique → Muse candidate → sandbox → evaluation.

---

## What You Should Understand Before Implementation

1. **An agent is a specific architectural pattern (autonomous decision-making with tools), not a synonym for "AI-powered component."** Most AI-powered components in DarwinUX are single LLM calls, dedicated models (Muse, Jev), or deterministic code.
2. **The cost of making something an agent is concrete: latency (seconds vs. milliseconds), cost (LLM tokens), unpredictability (non-deterministic behavior), and evaluation difficulty.** Each time you consider an agent, weigh these costs against the capability gained.
3. **LangGraph's value is in orchestration, not in making everything an agent.** A LangGraph node can contain a deterministic function, a single LLM call, a Muse call, or a Jev decision — it's the conditional routing and state management that justify LangGraph. Human decisions are graph *interrupts* (checkpoint and stop), and experiments live outside the graph — a workflow engine should not wait for weeks.
4. **The generator-critic pattern (hypothesis + critique) is more reliable than a single "do everything" agent.** Separating generation from evaluation forces the system to justify its reasoning.
5. **Muse, Jev, and general-purpose LLMs serve different roles.** Muse generates candidate mutations. Jev makes confidence-aware decisions. General-purpose LLMs handle reasoning tasks (hypothesis, critique, research). Don't conflate these — they have different input/output contracts, different evaluation criteria, and different provider boundaries.
6. **Jev's role is confidence-aware gating, not text generation.** Its value over plain thresholds or an LLM's "opinion" is a hypothesis to test: DarwinUX records rule, LLM-baseline, and Jev decisions side by side so the comparison can actually be measured. Every gate fails closed.
7. **Safety validation must be deterministic first, AI-augmented second.** Never rely solely on an LLM to enforce safety constraints. Muse's output always passes through deterministic validation before AI evaluation.
