/**
 * Asks the backend which UI Spec this anonymous session should render.
 *
 * The backend is the ONLY place assignment happens (stable hashing of
 * experiment key + session id). The browser never computes a variant: it
 * renders what it is told, or Generation 0 when anything is uncertain.
 *
 * Contract (backend: POST /api/v1/experiments/assignment):
 *   { status: "none" }
 *   { status: "assigned", experiment_key, variant, spec_hash, spec }
 *   { status: "fallback", experiment_key, reason }
 *
 * Never throws. Network errors, timeouts, non-2xx answers and malformed
 * bodies all resolve to `{ status: "none" }` (Generation 0, no exposure).
 * The spec itself is NOT trusted here: the caller validates it with the
 * app's real UI Spec schema before rendering.
 */
import { z } from "zod";

import { EXPERIMENT_KEY_PATTERN, SPEC_HASH_PATTERN } from "@/lib/telemetry/client";

export const ASSIGNMENT_PATH = "/api/v1/experiments/assignment";
export const ASSIGNMENT_TIMEOUT_MS = 3000;

const experimentKey = z.string().regex(EXPERIMENT_KEY_PATTERN);

export const assignmentResponse = z.discriminatedUnion("status", [
  z.object({ status: z.literal("none") }).strict(),
  z
    .object({
      status: z.literal("assigned"),
      experiment_key: experimentKey,
      variant: z.enum(["control", "candidate"]),
      spec_hash: z.string().regex(SPEC_HASH_PATTERN),
      spec: z.unknown(), // validated by the caller with the real UI Spec schema
    })
    .strict(),
  z
    .object({
      status: z.literal("fallback"),
      experiment_key: experimentKey,
      reason: z.enum([
        "spec_invalid",
        "render_error",
        "spec_unavailable",
        "spec_hash_mismatch",
        "assignment_error",
      ]),
    })
    .strict(),
]);

export type Assignment = z.infer<typeof assignmentResponse>;

export interface AssignmentOptions {
  baseUrl: string | null | undefined;
  sessionId: string;
  page: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

const NONE: Assignment = { status: "none" };

export async function requestAssignment(options: AssignmentOptions): Promise<Assignment> {
  const base = options.baseUrl?.trim().replace(/\/+$/, "") ?? "";
  if (!base) return NONE; // no backend configured: Generation 0
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? ASSIGNMENT_TIMEOUT_MS);
  try {
    const fetchImpl = options.fetchImpl ?? globalThis.fetch;
    const response = await fetchImpl(`${base}${ASSIGNMENT_PATH}`, {
      method: "POST", // the session id travels in the body, never in a URL
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        session_id: options.sessionId,
        page: options.page,
      }),
      credentials: "omit",
      signal: controller.signal,
    });
    if (!response.ok) return NONE;
    const parsed = assignmentResponse.safeParse(await response.json());
    return parsed.success ? parsed.data : NONE;
  } catch {
    return NONE;
  } finally {
    clearTimeout(timer);
  }
}
