# DarwinUX — Open Questions

Questions are grouped by urgency. **Blocking** means blocking a *specific* roadmap step (named), not the project. Nothing below blocks Step 1.

Anything about Jev or Muse that is not answered here must not be assumed anywhere else in the repository.

## BLOCKING

### B1. Jev integration (blocks Step 10; Steps 1–9 use `RulesDecider` / `LLMBaselineDecider`)

Known: Jev is a proprietary AI model developed by TypeSafe AI, treated in DarwinUX as a decision-oriented component.

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

### B4. Source of experiment traffic (blocks Step 8)

A portfolio project has no real users, so experiments cannot produce real UX evidence.

Recommendation: a synthetic user simulator (scripted personas in a headless browser whose behaviour responds to the friction in the UI), with all results labelled `traffic_source = simulated`. To decide: how realistic the simulator should be, and whether any real-user testing (friends, a small beta) is planned.

## NON-BLOCKING

Decide during the step that needs it; a sensible default is given.

| # | Question | Default until decided |
|---|---|---|
| N1 | Initial LLM provider/model for hypothesis, critique, research, judges | Any one provider behind the port; choose on structured-output support and cost |
| N2 | Critic on a different provider than the generator? | Same provider, different prompt, in v1; test cross-provider later |
| N3 | Embedding model and dimension | Any hosted embedding model; record model on each chunk |
| N4 | Local queue emulator | ElasticMQ or LocalStack in docker compose; pick the lighter one |
| N5 | Local trace viewer | Jaeger via OTel collector |
| N6 | Prompt storage | Prompt files in git, version ID recorded on every `ModelCall` |
| N7 | How much LangChain to use | Only document loaders/splitters where they save time; LangGraph for orchestration |
| N8 | Experiment statistics | Fixed-horizon test with pre-registered sample size; sequential testing later |
| N9 | Evolution Lab authentication | Single-user auth sufficient for approvals; no AI-created approvals ever |
| N10 | Headless browser / accessibility tooling for the sandbox | A mainstream headless browser automation library + an automated WCAG checker |
| N11 | AWS region and monthly budget ceiling | Nearest region; budget alert set before the first `apply` |
| N12 | Data retention for user events and model payloads | 90 days raw events locally; explicit CloudWatch retention; revisit before any real users |
| N13 | Reranker | None until retrieval metrics show precision is the bottleneck |
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
