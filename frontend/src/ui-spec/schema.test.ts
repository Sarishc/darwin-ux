import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import generation0 from "./generation-0.json";
import { parseUiSpec, uiSpec } from "./schema";

// Tests run from the frontend/ directory (jsdom: import.meta.url is not a file URL).
const GENERATION_0_PATH = resolve(process.cwd(), "src/ui-spec/generation-0.json");
// Generation 0 is historical input: it is never edited. A new generation is a new file.
const GENERATION_0_SHA256 = "80a28653cf9763b447acc56ff898eabbdc704bcf1cc608746ad38be54161d08a";

const clone = <T>(value: T): T => structuredClone(value);
type Mutable = { page: { sections: { components: Record<string, unknown>[] }[] } };

function withFirstComponent(patch: Record<string, unknown>): unknown {
  const spec = clone(generation0) as unknown as Mutable;
  Object.assign(spec.page.sections[0].components[0], patch);
  return spec;
}

describe("Generation 0 spec", () => {
  it("validates", () => {
    const spec = parseUiSpec(generation0);

    expect(spec.version).toBe(1);
    expect(spec.generation).toBe(0);
    expect(spec.page.id).toBe("pricing_signup");
  });

  it("is unchanged (immutable historical artifact)", () => {
    const digest = createHash("sha256").update(readFileSync(GENERATION_0_PATH)).digest("hex");

    expect(digest).toBe(GENERATION_0_SHA256);
  });

  it("contains the documented Generation 0 friction as data", () => {
    const spec = parseUiSpec(generation0);
    const components = spec.page.sections.flatMap((s) => s.components);
    const grid = components.find((c) => c.type === "plan_grid");
    const form = components.find((c) => c.type === "signup_form");

    expect(grid?.type === "plan_grid" && grid.plans.map((p) => p.cta.feedback)).toEqual([
      "immediate",
      "immediate",
      "delayed", // the highlighted plan's CTA gives no feedback for a while
    ]);
    expect(form?.type === "signup_form" && [form.validation, form.error_display]).toEqual([
      "on_submit",
      "summary",
    ]);
  });
});

describe("spec validation rejects anything that is not allowlisted data", () => {
  it("rejects an unknown component type", () => {
    const bad = withFirstComponent({ type: "iframe" });

    expect(uiSpec.safeParse(bad).success).toBe(false);
  });

  it.each([
    ["an event handler", { onClick: "alert(1)" }],
    ["raw HTML", { html: "<img src=x onerror=alert(1)>" }],
    ["dangerouslySetInnerHTML", { dangerouslySetInnerHTML: { __html: "<b>x</b>" } }],
    ["inline CSS", { style: "position:fixed" }],
    ["a class name", { className: "anything" }],
    ["a script URL", { src: "https://evil.example/x.js" }],
    ["a link", { href: "javascript:alert(1)" }],
  ])("rejects %s (unknown keys are errors, not ignored)", (_label, patch) => {
    expect(uiSpec.safeParse(withFirstComponent(patch)).success).toBe(false);
  });

  it("rejects token values outside their enum", () => {
    const spec = clone(generation0) as unknown as Mutable;
    spec.page.sections[0].components[1].emphasis = "blink";

    expect(uiSpec.safeParse(spec).success).toBe(false);
  });

  it("rejects actions that are not implemented in code", () => {
    const spec = clone(generation0) as unknown as {
      page: { sections: { components: { plans?: { cta: { action: string } }[] }[] }[] };
    };
    const grid = spec.page.sections[1].components[1];
    grid.plans![0].cta.action = "navigate:https://evil.example";

    expect(uiSpec.safeParse(spec).success).toBe(false);
  });

  it("rejects ids that could not be telemetry component identifiers", () => {
    expect(uiSpec.safeParse(withFirstComponent({ id: "Hero Heading!" })).success).toBe(false);
  });

  it("rejects duplicate ids", () => {
    expect(uiSpec.safeParse(withFirstComponent({ id: "hero_subheading" })).success).toBe(false);
  });

  it("rejects unsupported spec versions", () => {
    expect(uiSpec.safeParse({ ...clone(generation0), version: 2 }).success).toBe(false);
  });
});
