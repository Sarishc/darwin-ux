# DarwinUX — Open Questions

Questions are grouped by urgency. **Blocking** means blocking a *specific* roadmap step (named), not the project. Nothing below blocks Step 1.

Anything about Jev or Muse that is not answered here must not be assumed anywhere else in the repository.

## BLOCKING

### B1. Jev integration (blocks Step 10; Steps 1–9 use `RulesDecider` / `LLMBaselineDecider`)

Known: Jev is a proprietary AI model developed by TypeSafe AI, treated in DarwinUX as a decision-oriented component.

**Step 11 update — documented publicly** (docs.typesafe.ai/api, /models, /confidence; read 2026-09-27): (1) hosted HTTP API `POST https://api.typesafe.ai/v1/systemone` plus official client SDKs; (2) JSON body `{state, model, questions}`, `state` may be text, an object or an array; (3) typed questions `noul` / `choice` / `score` with schema-shaped answers (`choice`, `probabilities`, `confidence`), plus `usage`; (4) `confidence` is a statistic derived from the probability distribution, not a probability — no calibration claim found; (6, partly) versioned model ids (`jev-1.13.0`) and aliases (`jev-latest`); (8) Bearer API key. The adapter is implemented against this but **never called live**. Still unknown below: 5, determinism in 6, 7, 9, 10.

Unknown — to be verified with TypeSafe AI documentation or contacts:

1. How is Jev accessed (hosted API, SDK, self-hosted, other)? What is the official interface?
2. What input format does it accept? Structured JSON, free text, both? Size limits?
3. What does it return? A label, a score, a confidence, a rationale? Is the output schema-guaranteed?
4. What does its confidence value mean, and has it been calibrated? On what kind of data?
5. Which decision types does it support (classification, scoring, gating, escalation), and does it need task-specific configuration or fine-tuning?
6. Is it deterministic for identical inputs? Is there versioning of model releases?
7. Latency, rate limits, pricing, availability expectations?
8. Authentication method and credential type (so a Secrets Manager entry can be designed — no names assumed)?
9. Data handling: retention, training on submitted data, region, compliance?
10. Licensing/terms for using it in a public portfolio project (can results be published)?

### B2. Muse integration (blocks Step 10; Steps 1–9 use `FixtureMuse` / `LLMBaselineGenerator`)

Known: Muse is the name for DarwinUX's generative mutation component, which must be first-class and must never deploy.

**Step 12 check (2026-09-27):** no Muse package is installed, and TypeSafe AI's official documentation (docs.typesafe.ai: models, API reference, introduction, primitives) describes only Jev — a System One *decision* model returning typed answers — and never mentions Muse. No authoritative Muse interface was found, so none was implemented; `MuseAdapter` is an explicit, refusing seam. All questions below remain open.

Unknown:

1. **Who provides Muse?** Is it a TypeSafe AI model, another vendor's model, or a component DarwinUX is expected to build? (The earlier draft stated it was from TypeSafe AI; that was not verified and has been removed.)
2. Is Muse a model, a hosted service, or a library?
3. Can it accept structured constraints (allowed paths, enums, ranges) and a current UI representation as input?
4. What does it output — structured JSON, code, design files, images? Can it be made to emit a DarwinUX `MutationSpec`, or is an adapter translation needed?
5. Does it support schema-constrained output natively?
6. Determinism, seeding, versioning?
7. Latency, pricing, rate limits, authentication, data handling, licensing (same as B1.7–B1.10)?

### B3. Target application for Generation 0 (blocks Step 2)

Recommendation: a small demo app (sign-up or checkout flow) inside the Next.js project at `/demo`, rendering from a UI Spec, with deliberate known friction.

To decide: which flow; which components go in the initial registry; is there ever a real application DarwinUX should evolve?

### B4. Source of experiment traffic (blocks Step 8) — partly addressed in Step 14

Step 14 labels every experiment `traffic_source = simulated | real` (stored, and repeated in every analysis report). All traffic so far is simulated: scripted sessions through the real worker path plus manual browser sessions. There is still no simulator of realistic user behaviour.

A portfolio project has no real users, so experiments cannot produce real UX evidence.

Recommendation: a synthetic user simulator (scripted personas in a headless browser whose behaviour responds to the friction in the UI), with all results labelled `traffic_source = simulated`. To decide: how realistic the simulator should be, and whether any real-user testing (friends, a small beta) is planned.

## NON-BLOCKING

Decide during the step that needs it; a sensible default is given.

| # | Question | Default until decided |
|---|---|---|
| N1 | Initial LLM provider/model for hypothesis, critique, research, judges | Step 9 ships the port and a deterministic `FakeLLMProvider` only. Choose one provider behind the port on structured-output support and cost, and require it to pass `make hypothesis-eval` before use |
| N2 | Critic on a different provider than the generator? | Step 10: same port and provider, separate request (`hypothesis_critique.v1`); test cross-provider once a real provider exists |
| N3 | Embedding model and dimension | Step 8 ships only a deterministic hashing baseline (384-d, recorded per document). Choosing a real model means a migration if its dimension differs, a full re-embed, and beating the baseline on the golden set |
| N4 | Local queue (no Docker locally) | A native SQS-compatible emulator or a Postgres-backed queue behind the queue port; decide in the telemetry step |
| N5 | Local trace viewer | Jaeger via OTel collector |
| N6 | Prompt storage | Prompt files in git, version ID recorded on every `ModelCall` |
| N7 | How much LangChain to use | Only document loaders/splitters where they save time; LangGraph for orchestration |
| N8 | Experiment statistics | Step 14: Wilson intervals per variant + Newcombe difference interval, fixed pre-registered primary metric, an operational floor of 100 exposed sessions per variant (not a power calculation). Still open: a real power/sample-size calculation, sequential testing or alpha spending for repeated looks, multiple-guardrail correction, sample-ratio-mismatch check |
| N18 | No completion event | Generation 0 emits no "signup completed" event, so task success/completion cannot be measured; `signup_submit_session_rate` counts attempts. Adding a completion event is a telemetry-contract change for a later step |
| N19 | Automatic rollback on guardrail breach | The architecture planned it; Step 14 only flags `stop_recommended` and a human runs `make experiment-stop`. Decide the rule (and its false-alarm control under repeated looks) before automating |
| N22 | Reviewer identity | Step 15 reviewer strings are self-asserted CLI labels, not authenticated identities; approvals prove "someone with CLI and database access typed this", nothing more. Real authentication (N9) must bind approvals to verified identities before real traffic |
| N23 | Promotion on simulated evidence | promotion_policy.v1 allows `traffic_source = simulated` (there are no real users) and records it in the evidence hash. Decide whether a real-traffic requirement belongs in a later policy version |
| N24 | Generation summaries in Product Memory | Not done in Step 15: promotion correctness must not depend on RAG. Add a deferred, idempotent ingestion of a deterministic generation summary (Step 16) |
| N25 | Step 12/13 golden sets use the real demo page | `make mutation-eval` and `make sandbox-eval` (chain cases) run on the real `pricing_signup` page of the configured database and assume it has only Generation 0. After a real promotion they stop with a clear precondition message (a Generation 1 exists, and pre-Step-15 signals are correctly refused as `unknown` attribution). Refactor them onto isolated per-case pages, as the Step 14/15 sets already are |
| N21 | Client clock vs server window boundaries | Collection windows are server time; exposure/outcome times are the client's clock. Events are also required to arrive after their window opened, but a skewed client clock can still move an event across a pause boundary. Decide whether to attribute by server receipt time (robust to skew, sensitive to queue lag) or to estimate per-session clock offset |
| N20 | Visible generation label differs between arms | The SpecPage footer shows "Generation 0" vs "Generation 1": a small visible difference between variants, and "Generation 1" is not a promoted generation. Decide whether the badge should show "candidate" or be hidden inside experiments |
| N9 | Evolution Lab authentication | Single-user auth sufficient for approvals; no AI-created approvals ever |
| N10 | Headless browser / accessibility tooling for the sandbox | Step 13 uses jsdom + Testing Library + axe-core (no browser): enough for behaviour, telemetry and rule-based accessibility. A real browser is still needed for colour contrast, layout, visual regression and real timing |
| N17 | Inline validation without per-field display | The Step 13 harness found that `validation: inline` shows nothing when `error_display` is `summary` (errors only render per-field). Decide whether the renderer should surface inline errors in summary mode, or the mutation surface should couple the two |
| N11 | AWS region and monthly budget ceiling | Nearest region; budget alert set before the first `apply` |
| N12 | Data retention for user events and model payloads | 90 days raw events locally; explicit CloudWatch retention; revisit before any real users |
| N13 | Reranker | None until retrieval metrics show precision is the bottleneck |
| N15 | LangGraph checkpointing for human-in-the-loop | Step 10 uses DarwinUX-owned persistence (`research_run`) and re-enters the graph on resume; revisit LangGraph's Postgres checkpointer if graphs grow long-lived mid-node state |
| N16 | Stale `running` research runs after a crash | Not swept yet: the last `research_step` shows where it stopped; add a sweeper (mark `failed`) before running research in a worker |
| N14 | Terraform state locking mechanism | S3 backend native locking if the chosen Terraform version supports it |

## FUTURE RESEARCH

Interesting, not planned.

- Does Jev outperform rules and an LLM baseline at each gate — and is its confidence calibrated on DarwinUX data?
- Does Muse produce more valid / more successful mutations than a general LLM with structured output?
- Can generation history be used to learn priors over which mutation types tend to win?
- Safe auto-promotion for a narrowly defined low-risk tier — what evidence would justify it?
- Multi-armed bandits instead of fixed A/B tests.
- Detecting and preventing self-reinforcing feedback loops in Product Memory.
- Evaluating LLM-as-judge design-consistency scores against expert designers.
- Hybrid (keyword + vector) retrieval and learned reranking for Product Memory.
- Counterfactual/offline evaluation of mutations using logged simulator behaviour.
- Extending the mutation surface (e.g., new template variants authored by humans from AI suggestions).

---

## What You Should Understand Before Implementation

1. **An unknown is safer written down than guessed.** A guessed API name in one document becomes an assumption in five.
2. **Blocking is relative to a step.** Jev and Muse questions block their integration step, not the project — ports and baselines let everything else proceed.
3. **Every default is reversible.** Non-blocking questions have a default so work can continue, chosen so that changing it later is cheap.
4. **Honest limitations are part of the design.** Simulated traffic, unverified model capabilities, and small golden sets are stated constraints, not hidden ones.
