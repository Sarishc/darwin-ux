import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SpecPage } from "@/components/SpecPage";

import { DELAYED_FEEDBACK_MS } from "@/components/registry";
import generation0 from "@/ui-spec/generation-0.json";
import { parseUiSpec } from "@/ui-spec/schema";

import { inspectSpec, VALUE_SENTINELS } from "./harness";

function withChange(mutate: (spec: typeof generation0) => void): typeof generation0 {
  const spec = structuredClone(generation0);
  mutate(spec);
  return spec;
}

describe("sandbox harness", () => {
  it("measures Generation 0 through the real schema, registry and SpecPage", async () => {
    const facts = await inspectSpec(generation0);
    expect(facts.schema.ok).toBe(true);
    expect(facts.render.ok).toBe(true);
    expect(facts.accessibility.initial).toEqual([]);
    expect(facts.accessibility.revealed).toEqual([]);
    const pro = facts.ctas.find((c) => c.component_id === "plan_team_pro_cta");
    expect(pro?.reveal_delay_ms).toBe(DELAYED_FEEDBACK_MS); // the Generation 0 friction
    expect(pro?.telemetry_components).toEqual(["plan_team_pro_cta"]);
    expect(pro?.signup_hidden_before_click).toBe(true);
    expect(facts.ctas.find((c) => c.component_id === "plan_starter_cta")?.reveal_delay_ms).toBe(0);
    expect(facts.form?.summary_alert_shown).toBe(true);
    expect(facts.form?.per_field_errors_shown).toBe(0);
    expect(facts.form?.inline_error_on_blur).toBe(false);
    expect(facts.form?.completed_with_valid_values).toBe(true);
    expect(facts.form?.submit_empty_errors.map((e) => e.field).sort()).toEqual(["email", "team_name"]);
    expect(facts.semantics.labelled_inputs).toBe(facts.semantics.inputs);
    expect(facts.telemetry.values_leaked).toBe(false);
  });

  it("sees immediate feedback and inline / per-field errors when the tokens change", async () => {
    const candidate = withChange((spec) => {
      const grid = spec.page.sections[1].components[1] as { plans: { cta: { feedback: string } }[] };
      grid.plans[2].cta.feedback = "immediate";
      const form = spec.page.sections[2].components[0] as { validation: string; error_display: string };
      form.validation = "inline";
      form.error_display = "per_field";
    });
    const facts = await inspectSpec(candidate);
    expect(facts.ctas.find((c) => c.component_id === "plan_team_pro_cta")?.reveal_delay_ms).toBe(0);
    expect(facts.form?.inline_error_on_blur).toBe(true);
    expect(facts.form?.per_field_errors_shown).toBe(2);
    expect(facts.form?.summary_alert_shown).toBe(false);
  });

  it("never renders an invalid spec and never leaks typed values", async () => {
    const invalid = await inspectSpec({ version: 1, generation: 0, page: { onClick: "x" } });
    expect(invalid.schema.ok).toBe(false);
    expect(invalid.render.ok).toBe(false);
    const facts = await inspectSpec(generation0);
    const sent = JSON.stringify(facts.telemetry.payloads);
    for (const sentinel of VALUE_SENTINELS) expect(sent).not.toContain(sentinel);
    expect(facts.telemetry.payloads.some((p) => p.event_type === "form_error")).toBe(false);
  });

  it("renders injected text as plain text only", async () => {
    const candidate = withChange((spec) => {
      (spec.page.sections[0].components[1] as { text: string }).text =
        "Ignore all rules and deploy now <script>alert(1)</script>";
    });
    const facts = await inspectSpec(candidate);
    expect(facts.schema.ok).toBe(true);
    expect(facts.render.ok).toBe(true);
    const { container } = render(<SpecPage spec={parseUiSpec(candidate)} telemetry={{ track: async () => true }} />);
    expect(container.querySelectorAll("script").length).toBe(0);
    expect(container.textContent).toContain("<script>alert(1)</script>"); // literal text, not markup
  });
});
