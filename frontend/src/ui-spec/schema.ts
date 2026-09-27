/**
 * The UI Spec: a structured, *data-only* description of a DarwinUX page.
 *
 * Why this exists: later steps will let AI propose changes (MutationSpecs)
 * to the product UI. They must only ever be able to change *data* — pick
 * allowlisted component types, text, and design-token values — never code.
 * So this schema allows exactly that and nothing else:
 *
 *   - every object is `.strict()`: unknown keys (onClick, html, style,
 *     script, className, href, ...) are rejected, not ignored;
 *   - every visual/behavioural choice is a closed enum of design tokens;
 *   - text is plain text (rendered by React as text nodes, never as HTML);
 *   - actions name an allowlisted behaviour implemented in code; the spec
 *     cannot supply a handler.
 *
 * It describes only what the demo needs. It is not a page builder.
 */
import { z } from "zod";

// ---- Design tokens -----------------------------------------------------------------

export const spacing = z.enum(["sm", "md", "lg"]);
export const buttonVariant = z.enum(["primary", "secondary"]);
export const emphasis = z.enum(["normal", "strong"]);
export const tone = z.enum(["info", "warning"]);
/** How quickly a button acknowledges a click. `delayed` = Generation 0 friction. */
export const buttonFeedback = z.enum(["immediate", "delayed"]);
/** When the signup form validates. `on_submit` = Generation 0 friction. */
export const formValidation = z.enum(["on_submit", "inline"]);
/** How form errors are shown. `summary` (one vague message) = Generation 0 friction. */
export const errorDisplay = z.enum(["summary", "per_field"]);

// ---- Primitives --------------------------------------------------------------------

/**
 * Component ids double as telemetry `payload.component` values, so they must
 * satisfy the backend's identifier rule (^[A-Za-z0-9_.:-]{1,128}$, enforced
 * server-side — the backend remains the authority). We use the stricter
 * snake_case subset.
 */
export const componentId = z.string().regex(/^[a-z][a-z0-9_]{0,63}$/, "snake_case id");

/** Plain text. Short, single-purpose, and never interpreted as markup. */
const text = (max: number) => z.string().trim().min(1).max(max);

/** Behaviours a button may trigger. Implemented in code, chosen by name. */
export const buttonAction = z.enum(["reveal_signup"]);

// ---- Components ----------------------------------------------------------------------

export const headingSpec = z
  .object({
    type: z.literal("heading"),
    id: componentId,
    level: z.union([z.literal(1), z.literal(2), z.literal(3)]),
    text: text(120),
  })
  .strict();

export const textSpec = z
  .object({
    type: z.literal("text"),
    id: componentId,
    text: text(400),
    emphasis: emphasis,
  })
  .strict();

export const noticeSpec = z
  .object({
    type: z.literal("notice"),
    id: componentId,
    tone: tone,
    text: text(200),
  })
  .strict();

export const buttonSpec = z
  .object({
    type: z.literal("button"),
    id: componentId,
    label: text(40),
    variant: buttonVariant,
    feedback: buttonFeedback,
    action: buttonAction,
  })
  .strict();

export const planCardSpec = z
  .object({
    type: z.literal("plan_card"),
    id: componentId,
    name: text(40),
    price_label: text(40),
    features: z.array(text(80)).min(1).max(6),
    highlighted: z.boolean(),
    cta: buttonSpec,
  })
  .strict();

export const planGridSpec = z
  .object({
    type: z.literal("plan_grid"),
    id: componentId,
    gap: spacing,
    plans: z.array(planCardSpec).min(1).max(4),
  })
  .strict();

/** The only fields the demo form can have. Values never leave the browser. */
export const formFieldName = z.enum(["email", "team_name"]);

export const formFieldSpec = z
  .object({
    name: formFieldName,
    label: text(40),
    input: z.enum(["email", "text"]),
  })
  .strict();

export const signupFormSpec = z
  .object({
    type: z.literal("signup_form"),
    id: componentId,
    title: text(80),
    fields: z.array(formFieldSpec).min(1).max(4),
    submit_label: text(40),
    validation: formValidation,
    error_display: errorDisplay,
    summary_error_text: text(160),
    completion_text: text(160),
  })
  .strict();

export const componentSpec = z.discriminatedUnion("type", [
  headingSpec,
  textSpec,
  noticeSpec,
  buttonSpec,
  planGridSpec,
  signupFormSpec,
]);

// ---- Page -------------------------------------------------------------------------------

export const sectionSpec = z
  .object({
    id: componentId,
    spacing: spacing,
    /** `after_signup_reveal`: hidden until a `reveal_signup` action has completed. */
    visibility: z.enum(["always", "after_signup_reveal"]),
    components: z.array(componentSpec).min(1).max(12),
  })
  .strict();

export const uiSpec = z
  .object({
    version: z.literal(1),
    generation: z.number().int().min(0),
    page: z
      .object({
        id: componentId,
        title: text(80),
        sections: z.array(sectionSpec).min(1).max(8),
      })
      .strict(),
  })
  .strict()
  .superRefine((spec, ctx) => {
    // Ids are telemetry identities: they must be unique within a page.
    const seen = new Set<string>();
    const visit = (id: string) => {
      if (seen.has(id)) ctx.addIssue({ code: "custom", message: `duplicate id: ${id}` });
      seen.add(id);
    };
    visit(spec.page.id);
    for (const section of spec.page.sections) {
      visit(section.id);
      for (const component of section.components) {
        visit(component.id);
        if (component.type === "plan_grid") {
          for (const plan of component.plans) {
            visit(plan.id);
            visit(plan.cta.id);
          }
        }
      }
    }
  });

export type UiSpec = z.infer<typeof uiSpec>;
export type SectionSpec = z.infer<typeof sectionSpec>;
export type ComponentSpec = z.infer<typeof componentSpec>;
export type ComponentType = ComponentSpec["type"];
export type ButtonSpec = z.infer<typeof buttonSpec>;
export type PlanCardSpec = z.infer<typeof planCardSpec>;
export type SignupFormSpec = z.infer<typeof signupFormSpec>;
export type FormFieldName = z.infer<typeof formFieldName>;

/** Parse untrusted input into a UiSpec. Throws a ZodError describing every problem. */
export function parseUiSpec(input: unknown): UiSpec {
  return uiSpec.parse(input);
}
