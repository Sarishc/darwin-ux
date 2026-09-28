/**
 * Asks the backend which generation a page currently serves (Step 15).
 *
 * Contract (backend: GET /api/v1/generations/active?page=<page>, read-only):
 *   { status: "active", generation, spec_version_id, spec_hash, spec }
 *   { status: "none" }
 *
 * Never throws. Network errors, timeouts, non-2xx answers and malformed bodies all
 * resolve to `{ status: "none" }`, and the page renders its bundled Generation 0.
 * The spec is NOT trusted here: the caller validates it with the real UI Spec schema.
 * There is no promotion or rollback call — those are human CLI commands only.
 */
import { z } from "zod";

import { SPEC_HASH_PATTERN } from "@/lib/telemetry/client";
import { UUID_PATTERN } from "@/lib/telemetry/uuid";

export const ACTIVE_GENERATION_PATH = "/api/v1/generations/active";
export const ACTIVE_GENERATION_TIMEOUT_MS = 3000;

export const activeGenerationResponse = z.discriminatedUnion("status", [
  z.object({ status: z.literal("none") }).strict(),
  z
    .object({
      status: z.literal("active"),
      generation: z.number().int().min(0),
      spec_version_id: z.string().regex(UUID_PATTERN),
      spec_hash: z.string().regex(SPEC_HASH_PATTERN),
      spec: z.unknown(), // validated by the caller with the real UI Spec schema
    })
    .strict(),
]);

export type ActiveGeneration = z.infer<typeof activeGenerationResponse>;

const NONE: ActiveGeneration = { status: "none" };

export async function requestActiveGeneration(options: {
  baseUrl: string | null | undefined;
  page: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}): Promise<ActiveGeneration> {
  const base = options.baseUrl?.trim().replace(/\/+$/, "") ?? "";
  if (!base) return NONE;
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(),
    options.timeoutMs ?? ACTIVE_GENERATION_TIMEOUT_MS,
  );
  try {
    const fetchImpl = options.fetchImpl ?? globalThis.fetch;
    const url = `${base}${ACTIVE_GENERATION_PATH}?page=${encodeURIComponent(options.page)}`;
    const response = await fetchImpl(url, { credentials: "omit", signal: controller.signal });
    if (!response.ok) return NONE;
    const parsed = activeGenerationResponse.safeParse(await response.json());
    return parsed.success ? parsed.data : NONE;
  } catch {
    return NONE;
  } finally {
    clearTimeout(timer);
  }
}
