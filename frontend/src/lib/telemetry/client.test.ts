import { afterEach, describe, expect, it, vi } from "vitest";

import { buildEventBody, COMPONENT_PATTERN, createTelemetry, TELEMETRY_PATH } from "./client";
import { getSessionId, resetSessionForTests, SESSION_STORAGE_KEY } from "./session";
import { UUID_PATTERN } from "./uuid";

const SESSION = "5b2f0c8e-4c1a-4a5e-9d6f-0a1b2c3d4e5f";
const options = { generation: 0, sessionId: () => SESSION };

/** The backend TelemetryEvent contract (the backend remains the authority). */
const BACKEND_EVENT_TYPE = /^[a-z][a-z0-9_]*$/;
const BACKEND_FIELDS = ["event_id", "event_type", "occurred_at", "payload", "session_id"];

function okFetch() {
  return vi.fn(async () => new Response(null, { status: 202 }));
}

afterEach(() => {
  sessionStorage.clear();
  resetSessionForTests();
});

describe("event body", () => {
  it("matches the backend contract", () => {
    const body = buildEventBody({ type: "button_click", component: "plan_team_pro_cta" }, options);

    expect(body).not.toBeNull();
    expect(Object.keys(body!).sort()).toEqual(BACKEND_FIELDS);
    expect(body!.event_id).toMatch(UUID_PATTERN);
    expect(body!.session_id).toBe(SESSION);
    expect(body!.event_type).toMatch(BACKEND_EVENT_TYPE);
    expect(body!.occurred_at).toMatch(/Z$/); // timezone-aware (UTC), as the backend requires
    expect(body!.payload).toEqual({ generation: 0, component: "plan_team_pro_cta" });
    expect(String(body!.payload.component)).toMatch(COMPONENT_PATTERN);
    expect(JSON.stringify(body).length).toBeLessThan(8 * 1024); // backend payload limit
  });

  it("gives every interaction a fresh event_id", () => {
    const ids = new Set(
      Array.from({ length: 50 }, () =>
        buildEventBody({ type: "button_click", component: "x" }, options),
      ).map((b) => b!.event_id),
    );

    expect(ids.size).toBe(50);
  });

  it("form_error carries only identifiers and a reason code", () => {
    const body = buildEventBody(
      { type: "form_error", component: "signup_form", field: "email", reason: "invalid_format" },
      options,
    );

    expect(body!.payload).toEqual({
      generation: 0,
      component: "signup_form",
      field: "email",
      reason: "invalid_format",
    });
  });

  it("drops events whose component is not a valid identifier", () => {
    expect(buildEventBody({ type: "button_click", component: "a b@c" }, options)).toBeNull();
  });
});

describe("experiment events", () => {
  const hash = "c".repeat(64);

  it("an exposure carries only the experiment key, variant and spec hash", () => {
    const event = {
      type: "experiment_exposure",
      experiment: "rage_fix_pricing",
      variant: "candidate",
      specHash: hash,
    } as const;
    const body = buildEventBody(event, options);

    expect(body?.event_type).toBe("experiment_exposure");
    expect(body?.payload).toEqual({
      generation: 0,
      experiment: "rage_fix_pricing",
      variant: "candidate",
      spec_hash: hash,
    });
  });

  it("a fallback carries only the experiment key and a reason code", () => {
    const event = {
      type: "experiment_fallback",
      experiment: "rage_fix_pricing",
      reason: "render_error",
    } as const;

    expect(buildEventBody(event, options)?.payload).toEqual({
      generation: 0,
      experiment: "rage_fix_pricing",
      reason: "render_error",
    });
  });

  it.each([
    { type: "experiment_exposure", experiment: "Bad Key!", variant: "candidate", specHash: hash },
    { type: "experiment_exposure", experiment: "rage_fix", variant: "treatment", specHash: hash },
    { type: "experiment_exposure", experiment: "rage_fix", variant: "control", specHash: "abc" },
    { type: "experiment_fallback", experiment: "rage_fix", reason: "because" },
  ])("drops a malformed experiment event %#", (event) => {
    expect(buildEventBody(event as never, options)).toBeNull();
  });
});

describe("sending", () => {
  it("POSTs JSON to the telemetry endpoint without cookies", async () => {
    const fetchImpl = okFetch();
    const telemetry = createTelemetry({ baseUrl: "http://api.test/", fetchImpl, ...options });

    await expect(telemetry.track({ type: "page_view", page: "pricing_signup" })).resolves.toBe(
      true,
    );

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`http://api.test${TELEMETRY_PATH}`);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("omit");
    expect(JSON.parse(String(init.body)).event_type).toBe("page_view");
  });

  it.each([
    ["a network error", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch")))],
    ["a 422 rejection", vi.fn(async () => new Response(null, { status: 422 }))],
    ["a 500", vi.fn(async () => new Response(null, { status: 500 }))],
    ["a synchronous throw", vi.fn(() => { throw new Error("boom"); })],
  ])("never rejects on %s", async (_label, fetchImpl) => {
    const onDropped = vi.fn();
    const telemetry = createTelemetry({
      baseUrl: "http://api.test",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onDropped,
      ...options,
    });

    await expect(telemetry.track({ type: "button_click", component: "x" })).resolves.toBe(false);
    expect(onDropped).toHaveBeenCalledOnce();
  });

  it("is a silent no-op without a base URL", async () => {
    const fetchImpl = okFetch();
    const telemetry = createTelemetry({ baseUrl: undefined, fetchImpl, ...options });

    await expect(telemetry.track({ type: "button_click", component: "x" })).resolves.toBe(false);
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("session id", () => {
  it("is a UUID that stays stable within one browser session", () => {
    const first = getSessionId();

    expect(first).toMatch(UUID_PATTERN);
    expect(getSessionId()).toBe(first);
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBe(first);
  });

  it("is new for a new session", () => {
    const first = getSessionId();
    sessionStorage.clear(); // the tab/session ended

    expect(getSessionId()).not.toBe(first);
  });

  it("uses sessionStorage only — nothing in localStorage, no cookies", () => {
    getSessionId();

    expect(localStorage.length).toBe(0);
    expect(document.cookie).toBe("");
    expect(Object.keys(sessionStorage)).toEqual([SESSION_STORAGE_KEY]);
  });

  it("replaces a tampered value", () => {
    sessionStorage.setItem(SESSION_STORAGE_KEY, "someone@example.com");

    expect(getSessionId()).toMatch(UUID_PATTERN);
  });

  it("falls back to an in-memory id when storage throws", () => {
    const broken = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    } as unknown as Storage;

    const id = getSessionId(broken);

    expect(id).toMatch(UUID_PATTERN);
    expect(getSessionId(broken)).toBe(id);
  });
});
