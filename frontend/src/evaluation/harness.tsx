/**
 * The candidate sandbox harness: measures FACTS about a UI Spec by rendering it
 * through the real schema, the real registry and the real SpecPage in jsdom.
 * It does not judge; the backend's candidate_eval policy compares source and
 * candidate facts and decides.
 *
 * Safety: the spec is DATA. It is parsed with the app's Zod schema first; only
 * a valid spec is rendered, and only through `SpecPage` / `registry` — the
 * same allowlisted path the demo uses. Telemetry is captured in memory
 * (never sent). Timers are faked to measure click feedback deterministically.
 *
 * Runs under Vitest (jsdom). Not part of the Next.js app bundle.
 */
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import axe from "axe-core";
import { vi } from "vitest";

import { SpecPage } from "@/components/SpecPage";
import { DELAYED_FEEDBACK_MS } from "@/components/registry";
import { buildEventBody, type Telemetry, type TrackedEvent } from "@/lib/telemetry/client";
import { type UiSpec, uiSpec } from "@/ui-spec/schema";

export const HARNESS_VERSION = "sandbox_harness.v1";
/** jsdom has no layout, so contrast cannot be measured here; a real browser step will. */
export const DISABLED_AXE_RULES = ["color-contrast"] as const;
/** Sentinels typed into the form; they must never appear in any telemetry payload. */
export const VALUE_SENTINELS = ["sentinel.value.7@example.com", "Sentinel Team Seven"] as const;

export interface AxeViolation {
  id: string;
  impact: string;
  nodes: number;
}

export interface CtaFacts {
  component_id: string;
  telemetry_components: string[]; // button_click components emitted by one click
  reveal_delay_ms: number | null; // 0 immediate, DELAYED_FEEDBACK_MS delayed, null never
  signup_hidden_before_click: boolean;
}

export interface FormFacts {
  present: boolean;
  submit_empty_errors: { field: string; reason: string }[];
  summary_alert_shown: boolean;
  per_field_errors_shown: number;
  inline_error_on_blur: boolean;
  completed_with_valid_values: boolean;
  telemetry_components: string[];
}

export interface SpecFacts {
  schema: { ok: boolean; issues: string[] };
  render: { ok: boolean; error: string | null; dom_nodes: number; buttons: number };
  accessibility: {
    initial: AxeViolation[];
    revealed: AxeViolation[];
    disabled_rules: string[];
  };
  semantics: {
    heading_levels: number[];
    inputs: number;
    labelled_inputs: number;
    focusable_buttons: number;
  };
  ctas: CtaFacts[];
  form: FormFacts | null;
  telemetry: { payloads: Record<string, string | number>[]; values_leaked: boolean };
  render_ms: number; // informational: local jsdom timing, NOT a web-performance metric
}

class Recorder implements Telemetry {
  events: TrackedEvent[] = [];
  async track(event: TrackedEvent): Promise<boolean> {
    this.events.push(event);
    return true;
  }
}

function ctaIds(spec: UiSpec): string[] {
  const ids: string[] = [];
  for (const section of spec.page.sections) {
    for (const component of section.components) {
      if (component.type === "button") ids.push(component.id);
      if (component.type === "plan_grid") for (const plan of component.plans) ids.push(plan.cta.id);
    }
  }
  return ids;
}

function signupSectionIds(spec: UiSpec): string[] {
  return spec.page.sections.filter((s) => s.visibility === "after_signup_reveal").map((s) => s.id);
}

function signupVisible(container: HTMLElement, spec: UiSpec): boolean {
  const hidden = signupSectionIds(spec);
  return hidden.length > 0 && hidden.every((id) => container.querySelector(`[data-section="${id}"]`));
}

async function axeViolations(container: HTMLElement): Promise<AxeViolation[]> {
  const rules = Object.fromEntries(DISABLED_AXE_RULES.map((r) => [r, { enabled: false }]));
  const result = await axe.run(container, { rules });
  return result.violations
    .map((v) => ({ id: v.id, impact: v.impact ?? "unknown", nodes: v.nodes.length }))
    .sort((a, b) => a.id.localeCompare(b.id));
}

function mount(spec: UiSpec) {
  const recorder = new Recorder();
  const view = render(<SpecPage spec={spec} telemetry={recorder} />);
  return { recorder, container: view.container };
}

function clickAndMeasure(spec: UiSpec, id: string): CtaFacts {
  vi.useFakeTimers();
  try {
    const { recorder, container } = mount(spec);
    const hiddenBefore = !signupVisible(container, spec);
    const button = container.querySelector<HTMLElement>(`[data-component="${id}"]`);
    let delay: number | null = null;
    if (button) {
      act(() => fireEvent.click(button));
      if (signupVisible(container, spec)) {
        delay = 0;
      } else {
        act(() => vi.advanceTimersByTime(DELAYED_FEEDBACK_MS));
        if (signupVisible(container, spec)) delay = DELAYED_FEEDBACK_MS;
      }
    }
    const clicks = recorder.events.filter((e) => e.type === "button_click");
    return {
      component_id: id,
      telemetry_components: clicks.map((e) => (e.type === "button_click" ? e.component : "")),
      reveal_delay_ms: delay,
      signup_hidden_before_click: hiddenBefore,
    };
  } finally {
    cleanup();
    vi.useRealTimers();
  }
}

/** Reveal the signup section with the first CTA (fake timers cover delayed feedback). */
function mountRevealed(spec: UiSpec) {
  vi.useFakeTimers();
  const mounted = mount(spec);
  const [first] = ctaIds(spec);
  const button = first
    ? mounted.container.querySelector<HTMLElement>(`[data-component="${first}"]`)
    : null;
  if (button) {
    act(() => fireEvent.click(button));
    act(() => vi.advanceTimersByTime(DELAYED_FEEDBACK_MS));
  }
  vi.useRealTimers();
  return mounted;
}

function formFacts(spec: UiSpec): FormFacts | null {
  const form = spec.page.sections.flatMap((s) => s.components).find((c) => c.type === "signup_form");
  if (!form || form.type !== "signup_form") return null;
  try {
    // 1. submit empty
    let { recorder, container } = mountRevealed(spec);
    const formEl = container.querySelector<HTMLFormElement>(`form[data-component="${form.id}"]`);
    if (!formEl) return { ...emptyForm(), present: false };
    act(() => fireEvent.submit(formEl));
    const errors = recorder.events.flatMap((e) =>
      e.type === "form_error" ? [{ field: e.field, reason: e.reason }] : [],
    );
    const summary = container.querySelector('[role="alert"]') !== null;
    const perField = container.querySelectorAll(".field-error").length;
    const components = recorder.events.flatMap((e) =>
      e.type === "form_error" || (e.type === "button_click" && e.component.startsWith(form.id))
        ? [e.component]
        : [],
    );
    cleanup();

    // 2. inline: an invalid value, then blur, before any submit
    ({ recorder, container } = mountRevealed(spec));
    const email = container.querySelector<HTMLInputElement>(`#${form.id}_email`);
    let inline = false;
    if (email) {
      act(() => fireEvent.change(email, { target: { value: "not-an-email" } }));
      act(() => fireEvent.blur(email));
      inline = container.querySelector(".field-error") !== null;
    }
    cleanup();

    // 3. valid values (sentinels) complete the form; they must never reach telemetry
    ({ recorder, container } = mountRevealed(spec));
    const inputs = form.fields.map((f) =>
      container.querySelector<HTMLInputElement>(`#${form.id}_${f.name}`),
    );
    form.fields.forEach((f, i) => {
      const input = inputs[i];
      const value = f.name === "email" ? VALUE_SENTINELS[0] : VALUE_SENTINELS[1];
      if (input) act(() => fireEvent.change(input, { target: { value } }));
    });
    const formAgain = container.querySelector<HTMLFormElement>(`form[data-component="${form.id}"]`);
    if (formAgain) act(() => fireEvent.submit(formAgain));
    const completed =
      container.querySelector(`[data-component="${form.id}_completion"]`) !== null;
    return {
      present: true,
      submit_empty_errors: errors,
      summary_alert_shown: summary,
      per_field_errors_shown: perField,
      inline_error_on_blur: inline,
      completed_with_valid_values: completed,
      telemetry_components: [...new Set(components)].sort(),
    };
  } finally {
    cleanup();
    vi.useRealTimers();
  }
}

function emptyForm(): FormFacts {
  return {
    present: false,
    submit_empty_errors: [],
    summary_alert_shown: false,
    per_field_errors_shown: 0,
    inline_error_on_blur: false,
    completed_with_valid_values: false,
    telemetry_components: [],
  };
}

function payloads(events: TrackedEvent[], generation: number) {
  return events.flatMap((event) => {
    const body = buildEventBody(event, {
      generation,
      sessionId: () => "00000000-0000-4000-8000-000000000000",
      now: () => new Date(0),
    });
    return body ? [{ event_type: body.event_type, ...body.payload }] : [];
  });
}

/** Measure every fact the backend policy needs. Never throws: failures become facts. */
export async function inspectSpec(input: unknown): Promise<SpecFacts> {
  const parsed = uiSpec.safeParse(input);
  const facts: SpecFacts = {
    schema: {
      ok: parsed.success,
      issues: parsed.success
        ? []
        : parsed.error.issues.map((i) => `${i.path.join(".") || "(root)"}: ${i.message}`),
    },
    render: { ok: false, error: null, dom_nodes: 0, buttons: 0 },
    accessibility: { initial: [], revealed: [], disabled_rules: [...DISABLED_AXE_RULES] },
    semantics: { heading_levels: [], inputs: 0, labelled_inputs: 0, focusable_buttons: 0 },
    ctas: [],
    form: null,
    telemetry: { payloads: [], values_leaked: false },
    render_ms: 0,
  };
  if (!parsed.success) return facts;
  const spec = parsed.data;

  try {
    const started = performance.now();
    const { container } = mount(spec);
    facts.render_ms = Math.round((performance.now() - started) * 1000) / 1000;
    facts.render = {
      ok: true,
      error: null,
      dom_nodes: container.querySelectorAll("*").length,
      buttons: container.querySelectorAll("button").length,
    };
    facts.accessibility.initial = await axeViolations(container);
    cleanup();

    const revealed = mountRevealed(spec);
    facts.accessibility.revealed = await axeViolations(revealed.container);
    const headings = [...revealed.container.querySelectorAll("h1, h2, h3, h4, h5, h6")];
    const inputs = [...revealed.container.querySelectorAll("input")];
    const buttons = [...revealed.container.querySelectorAll("button")];
    facts.semantics = {
      heading_levels: headings.map((h) => Number(h.tagName.slice(1))),
      inputs: inputs.length,
      labelled_inputs: inputs.filter((i) => i.labels !== null && i.labels.length > 0).length,
      focusable_buttons: buttons.filter((b) => !b.disabled && b.tabIndex >= 0).length,
    };
    cleanup();
  } catch (error) {
    cleanup();
    facts.render = { ok: false, error: (error as Error).name, dom_nodes: 0, buttons: 0 };
    return facts;
  }

  facts.ctas = ctaIds(spec).map((id) => clickAndMeasure(spec, id));
  facts.form = formFacts(spec);

  // Telemetry that WOULD be sent across all interactions (built, never posted).
  const all = new Recorder();
  vi.useFakeTimers();
  try {
    render(<SpecPage spec={spec} telemetry={all} />);
    for (const id of ctaIds(spec)) {
      const button = document.querySelector<HTMLElement>(`[data-component="${id}"]`);
      if (button) act(() => fireEvent.click(button));
    }
    act(() => vi.advanceTimersByTime(DELAYED_FEEDBACK_MS));
    const form = document.querySelector<HTMLFormElement>("form");
    if (form) {
      for (const input of form.querySelectorAll("input")) {
        const value = input.type === "email" ? VALUE_SENTINELS[0] : VALUE_SENTINELS[1];
        act(() => fireEvent.change(input, { target: { value } }));
      }
      act(() => fireEvent.submit(form));
    }
  } finally {
    cleanup();
    vi.useRealTimers();
  }
  const sent = payloads(all.events, spec.generation);
  facts.telemetry = {
    payloads: sent,
    values_leaked: VALUE_SENTINELS.some((s) => JSON.stringify(sent).includes(s)),
  };
  return facts;
}

/** Facts for many specs, keyed by the caller's ids (e.g. content hashes). */
export async function inspectSpecs(specs: Record<string, unknown>): Promise<Record<string, SpecFacts>> {
  const out: Record<string, SpecFacts> = {};
  for (const key of Object.keys(specs).sort()) out[key] = await inspectSpec(specs[key]);
  return out;
}
