/**
 * Where telemetry goes. NEXT_PUBLIC_* values are inlined into the browser
 * bundle at build time: this is a public endpoint, never a secret.
 * Unset -> telemetry is disabled (the demo still works).
 */
export const DARWIN_API_BASE_URL = process.env.NEXT_PUBLIC_DARWIN_API_BASE_URL ?? null;
