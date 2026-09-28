/**
 * The DarwinUX browser telemetry client.
 *
 * Contract (the backend is the authority; see backend TelemetryEvent):
 *   POST {base}/api/v1/telemetry/events
 *   { event_id, event_type, session_id, occurred_at, payload }
 *
 * Privacy by construction: callers cannot pass arbitrary payloads. Each event
 * type has a fixed payload shape made of identifiers and small enums —
 * never text the user typed, never identity. Unknown or malformed input is
 * dropped, not sent.
 *
 * Failure semantics: `track` never throws and never rejects. Telemetry is
 * fire-and-forget; the UI must behave identically whether it succeeds,
 * fails, is rejected by the backend, or is disabled. No retries: the backend
 * is idempotent on event_id, but losing an occasional event is acceptable and
 * retry storms from a broken page are not.
 */
import type { FormFieldName } from "@/ui-spec/schema";

import { getSessionId } from "./session";
import { randomUuid, UUID_PATTERN } from "./uuid";

export const TELEMETRY_PATH = "/api/v1/telemetry/events";

/** Mirrors the backend identifier rule for payload.component (backend enforces it). */
export const COMPONENT_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/;

export type FormErrorReason = "required" | "invalid_format";

/** Experiments (Step 14). The backend owns assignment; these only report what rendered. */
export type ExperimentVariant = "control" | "candidate";
export type ExperimentFallbackReason =
  "spec_invalid" | "render_error" | "spec_unavailable" | "spec_hash_mismatch" | "assignment_error";
export const EXPERIMENT_KEY_PATTERN = /^[a-z][a-z0-9_]{2,63}$/;
export const SPEC_HASH_PATTERN = /^[0-9a-f]{64}$/;
const VARIANTS: readonly string[] = ["control", "candidate"];
const FALLBACK_REASONS: readonly string[] = [
  "spec_invalid",
  "render_error",
  "spec_unavailable",
  "spec_hash_mismatch",
  "assignment_error",
];

export type TrackedEvent =
  | { type: "page_view"; page: string }
  | { type: "button_click"; component: string }
  | { type: "form_error"; component: string; field: FormFieldName; reason: FormErrorReason }
  // Sent once, only AFTER the assigned variant rendered successfully.
  | {
      type: "experiment_exposure";
      experiment: string;
      variant: ExperimentVariant;
      specHash: string;
    }
  // The assigned variant could not be rendered; Generation 0 was shown instead (not an exposure).
  | { type: "experiment_fallback"; experiment: string; reason: ExperimentFallbackReason };

export interface TelemetryEventBody {
  event_id: string;
  event_type: TrackedEvent["type"];
  session_id: string;
  occurred_at: string;
  payload: Record<string, string | number>;
  /** UI attribution (Step 15): what this page rendered. Claims; the backend verifies. */
  ui_generation: number;
  ui_spec_hash?: string;
  ui_spec_version_id?: string;
}

export interface TelemetryOptions {
  /** e.g. http://127.0.0.1:8000 — null/empty disables telemetry. */
  baseUrl: string | null | undefined;
  generation: number;
  /** The served spec's hash and version id — only when the backend served the spec. */
  specHash?: string | null;
  specVersionId?: string | null;
  fetchImpl?: typeof fetch;
  sessionId?: () => string;
  now?: () => Date;
  onDropped?: (reason: string) => void;
}

export interface Telemetry {
  /** Resolves to true if the backend accepted (2xx), false otherwise. Never rejects. */
  track(event: TrackedEvent): Promise<boolean>;
}

/** Build the request body, or null if the event is not safe/valid to send. */
export function buildEventBody(
  event: TrackedEvent,
  options: Pick<
    TelemetryOptions,
    "generation" | "sessionId" | "now" | "specHash" | "specVersionId"
  >,
): TelemetryEventBody | null {
  const common = { generation: options.generation };
  let payload: Record<string, string | number>;
  switch (event.type) {
    case "page_view":
      if (!COMPONENT_PATTERN.test(event.page)) return null;
      payload = { ...common, page: event.page };
      break;
    case "button_click":
      if (!COMPONENT_PATTERN.test(event.component)) return null;
      payload = { ...common, component: event.component };
      break;
    case "form_error":
      if (!COMPONENT_PATTERN.test(event.component)) return null;
      payload = { ...common, component: event.component, field: event.field, reason: event.reason };
      break;
    case "experiment_exposure":
      if (
        !EXPERIMENT_KEY_PATTERN.test(event.experiment) ||
        !VARIANTS.includes(event.variant) ||
        !SPEC_HASH_PATTERN.test(event.specHash)
      )
        return null;
      payload = {
        ...common,
        experiment: event.experiment,
        variant: event.variant,
        spec_hash: event.specHash,
      };
      break;
    case "experiment_fallback":
      if (
        !EXPERIMENT_KEY_PATTERN.test(event.experiment) ||
        !FALLBACK_REASONS.includes(event.reason)
      )
        return null;
      payload = { ...common, experiment: event.experiment, reason: event.reason };
      break;
    default:
      return null;
  }
  const attribution =
    options.specHash &&
    options.specVersionId &&
    SPEC_HASH_PATTERN.test(options.specHash) &&
    UUID_PATTERN.test(options.specVersionId)
      ? { ui_spec_hash: options.specHash, ui_spec_version_id: options.specVersionId }
      : {}; // bundled Generation 0: no server-issued identity, so nothing to claim
  return {
    event_id: randomUuid(), // fresh per interaction: the backend's idempotency key
    event_type: event.type,
    session_id: (options.sessionId ?? getSessionId)(),
    occurred_at: (options.now?.() ?? new Date()).toISOString(), // UTC with "Z"
    payload,
    ui_generation: options.generation,
    ...attribution,
  };
}

export function createTelemetry(options: TelemetryOptions): Telemetry {
  const base = options.baseUrl?.trim().replace(/\/+$/, "") ?? "";
  const drop = (reason: string) => {
    options.onDropped?.(reason);
    return false;
  };

  return {
    async track(event: TrackedEvent): Promise<boolean> {
      try {
        if (!base) return drop("telemetry disabled (no base URL)");
        const body = buildEventBody(event, options);
        if (body === null) return drop(`invalid ${event.type} event`);
        const fetchImpl = options.fetchImpl ?? globalThis.fetch;
        const response = await fetchImpl(`${base}${TELEMETRY_PATH}`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
          keepalive: true, // still delivered if the user navigates away
          credentials: "omit", // no cookies: telemetry is anonymous
        });
        return response.ok || drop(`backend responded ${response.status}`);
      } catch {
        return drop("network error"); // never propagate into the UI
      }
    },
  };
}
