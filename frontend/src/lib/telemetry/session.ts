/**
 * The anonymous session id: one random UUID per browser tab session.
 *
 * Stored in sessionStorage, deliberately not localStorage or a cookie:
 * - it survives reloads and navigation within the tab (so one visit is one session);
 * - it disappears when the tab/session ends (so it never becomes a long-lived
 *   identity that links visits together);
 * - it is never sent anywhere except as `session_id` in telemetry.
 *
 * If storage is unavailable (e.g. blocked), an in-memory id is used for the
 * lifetime of the page. Telemetry must never break the page.
 */
import { UUID_PATTERN, randomUuid } from "./uuid";

export const SESSION_STORAGE_KEY = "darwin.session_id";

let memoryFallback: string | null = null;

function browserSessionStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null; // access itself can throw when storage is blocked
  }
}

export function getSessionId(storage: Storage | null = browserSessionStorage()): string {
  try {
    const existing = storage?.getItem(SESSION_STORAGE_KEY);
    if (existing && UUID_PATTERN.test(existing)) return existing;
    const created = randomUuid();
    storage?.setItem(SESSION_STORAGE_KEY, created);
    if (storage) return created;
  } catch {
    // fall through to the in-memory id
  }
  memoryFallback ??= randomUuid();
  return memoryFallback;
}

/** Test helper: forget the in-memory fallback. */
export function resetSessionForTests(): void {
  memoryFallback = null;
}
