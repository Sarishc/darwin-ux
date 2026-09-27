"use client";

/**
 * The component registry: the ONLY way a UI Spec becomes React.
 *
 * Each allowlisted spec `type` maps to one renderer written in code. A spec
 * can choose *which* renderer and pass it validated data; it can never
 * supply code, markup, styles, or handlers. Anything not in this map is an
 * error. This map is the future safety boundary for AI-proposed mutations.
 */
import { type FormEvent, type ReactNode, useRef, useState } from "react";

import type { FormErrorReason, TrackedEvent } from "@/lib/telemetry/client";
import type {
  ButtonSpec,
  ComponentSpec,
  ComponentType,
  FormFieldName,
  PlanCardSpec,
  SignupFormSpec,
} from "@/ui-spec/schema";

/** What renderers may do besides rendering: report telemetry, run allowlisted actions. */
export interface RenderContext {
  track: (event: TrackedEvent) => void; // fire-and-forget; never throws
  runAction: (action: ButtonSpec["action"]) => void;
}

/** Generation 0 friction: how long a `delayed` button gives no feedback at all. */
export const DELAYED_FEEDBACK_MS = 1500;

type RendererFor<T extends ComponentType> = (props: {
  spec: Extract<ComponentSpec, { type: T }>;
  ctx: RenderContext;
}) => ReactNode;

type Registry = { [T in ComponentType]: RendererFor<T> };

// ---- Renderers ------------------------------------------------------------------------

function Heading({ spec }: { spec: Extract<ComponentSpec, { type: "heading" }> }) {
  const Tag = (["h1", "h2", "h3"] as const)[spec.level - 1];
  return <Tag data-component={spec.id}>{spec.text}</Tag>;
}

function Text({ spec }: { spec: Extract<ComponentSpec, { type: "text" }> }) {
  return (
    <p data-component={spec.id} className={`text emphasis-${spec.emphasis}`}>
      {spec.text}
    </p>
  );
}

function Notice({ spec }: { spec: Extract<ComponentSpec, { type: "notice" }> }) {
  return (
    <p data-component={spec.id} role="note" className={`notice tone-${spec.tone}`}>
      {spec.text}
    </p>
  );
}

export function CtaButton({ spec, ctx }: { spec: ButtonSpec; ctx: RenderContext }) {
  // `delayed`: the click is registered but NOTHING visible happens for a while —
  // no disabled state, no spinner. Users click again. That is the friction.
  const pending = useRef(false);

  const onClick = () => {
    ctx.track({ type: "button_click", component: spec.id }); // every click is observed
    if (spec.feedback === "immediate") {
      ctx.runAction(spec.action);
      return;
    }
    if (pending.current) return; // later clicks do nothing — silently
    pending.current = true;
    window.setTimeout(() => {
      pending.current = false;
      ctx.runAction(spec.action);
    }, DELAYED_FEEDBACK_MS);
  };

  return (
    <button
      type="button"
      data-component={spec.id}
      className={`button variant-${spec.variant}`}
      onClick={onClick}
    >
      {spec.label}
    </button>
  );
}

function PlanCard({ spec, ctx }: { spec: PlanCardSpec; ctx: RenderContext }) {
  return (
    <article
      data-component={spec.id}
      className={`plan-card${spec.highlighted ? " highlighted" : ""}`}
      aria-labelledby={`${spec.id}_name`}
    >
      <h3 id={`${spec.id}_name`}>{spec.name}</h3>
      <p className="price">{spec.price_label}</p>
      <ul>
        {spec.features.map((feature) => (
          <li key={feature}>{feature}</li>
        ))}
      </ul>
      <CtaButton spec={spec.cta} ctx={ctx} />
    </article>
  );
}

function PlanGrid({
  spec,
  ctx,
}: {
  spec: Extract<ComponentSpec, { type: "plan_grid" }>;
  ctx: RenderContext;
}) {
  return (
    <div data-component={spec.id} className={`plan-grid gap-${spec.gap}`}>
      {spec.plans.map((plan) => (
        <PlanCard key={plan.id} spec={plan} ctx={ctx} />
      ))}
    </div>
  );
}

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Validation result per field: a reason code, never the value. */
export function validateField(name: FormFieldName, value: string): FormErrorReason | null {
  const trimmed = value.trim();
  if (!trimmed) return "required";
  if (name === "email" && !EMAIL_PATTERN.test(trimmed)) return "invalid_format";
  return null;
}

const REASON_TEXT: Record<FormErrorReason, string> = {
  required: "This field is required.",
  invalid_format: "Enter a valid email address.",
};

export function SignupForm({ spec, ctx }: { spec: SignupFormSpec; ctx: RenderContext }) {
  // Values live only in this component's state. They are never sent anywhere.
  const [values, setValues] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Partial<Record<FormFieldName, FormErrorReason>>>({});
  const [showSummary, setShowSummary] = useState(false);
  const [completed, setCompleted] = useState(false);

  const validateAll = () => {
    const found: Partial<Record<FormFieldName, FormErrorReason>> = {};
    for (const field of spec.fields) {
      const reason = validateField(field.name, values[field.name] ?? "");
      if (reason) found[field.name] = reason;
    }
    return found;
  };

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    ctx.track({ type: "button_click", component: `${spec.id}_submit` });
    const found = validateAll();
    setErrors(found);
    setShowSummary(Object.keys(found).length > 0);
    for (const [field, reason] of Object.entries(found) as [FormFieldName, FormErrorReason][]) {
      ctx.track({ type: "form_error", component: spec.id, field, reason });
    }
    if (Object.keys(found).length === 0) {
      setCompleted(true);
      setValues({});
    }
  };

  const onBlur = (name: FormFieldName) => {
    if (spec.validation !== "inline") return; // Generation 0: validates on submit only
    const reason = validateField(name, values[name] ?? "");
    setErrors((current) => ({ ...current, [name]: reason ?? undefined }));
  };

  if (completed) {
    return (
      <p data-component={`${spec.id}_completion`} role="status" className="notice tone-info">
        {spec.completion_text}
      </p>
    );
  }

  const perField = spec.error_display === "per_field";
  return (
    <form data-component={spec.id} className="signup-form" onSubmit={onSubmit} noValidate>
      <h2>{spec.title}</h2>
      {showSummary && !perField ? (
        <p role="alert" className="notice tone-warning">
          {spec.summary_error_text}
        </p>
      ) : null}
      {spec.fields.map((field) => {
        const inputId = `${spec.id}_${field.name}`;
        const error = errors[field.name];
        const showFieldError = perField && error;
        return (
          <div key={field.name} className="field">
            <label htmlFor={inputId}>{field.label}</label>
            <input
              id={inputId}
              name={field.name}
              type={field.input}
              autoComplete="off"
              value={values[field.name] ?? ""}
              aria-invalid={showFieldError ? true : undefined}
              aria-describedby={showFieldError ? `${inputId}_error` : undefined}
              onChange={(event) =>
                setValues((current) => ({ ...current, [field.name]: event.target.value }))
              }
              onBlur={() => onBlur(field.name)}
            />
            {showFieldError ? (
              <p id={`${inputId}_error`} className="field-error">
                {REASON_TEXT[error]}
              </p>
            ) : null}
          </div>
        );
      })}
      <button type="submit" className="button variant-primary">
        {spec.submit_label}
      </button>
    </form>
  );
}

// ---- The registry ------------------------------------------------------------------------

export const registry: Registry = {
  heading: Heading,
  text: Text,
  notice: Notice,
  button: CtaButton,
  plan_grid: PlanGrid,
  signup_form: SignupForm,
};

export class UnknownComponentError extends Error {
  constructor(type: unknown) {
    super(`Unknown UI Spec component type: ${String(type).slice(0, 64)}`);
    this.name = "UnknownComponentError";
  }
}

/**
 * Render one component spec. Fails loudly on a type outside the registry,
 * including prototype names such as "constructor" (Object.hasOwn, not `in`).
 */
export function renderComponent(spec: ComponentSpec, ctx: RenderContext, key?: string): ReactNode {
  const type: unknown = (spec as { type?: unknown }).type;
  if (typeof type !== "string" || !Object.hasOwn(registry, type)) {
    throw new UnknownComponentError(type);
  }
  const Renderer = registry[type as ComponentType] as RendererFor<ComponentType>;
  return <Renderer key={key ?? spec.id} spec={spec as never} ctx={ctx} />;
}
