/** /demo renders the ACTIVE generation from the backend, or the bundled Generation 0. */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ACTIVE_GENERATION_PATH } from "@/lib/generations/active";
import type { Telemetry, TrackedEvent } from "@/lib/telemetry/client";
import generation0 from "@/ui-spec/generation-0.json";
import type { UiSpec } from "@/ui-spec/schema";

import { ActiveGenerationPage } from "./ActiveGenerationPage";

const BASE = "http://127.0.0.1:8000";
const HASH = "d".repeat(64);
const VERSION_ID = "7c1e2f3a-4b5c-4d6e-8f90-a1b2c3d4e5f6";

function generation1() {
  const spec = structuredClone(generation0) as typeof generation0 & { generation: number };
  spec.generation = 1;
  const grid = spec.page.sections[1].components[1] as { plans: { cta: { feedback: string } }[] };
  grid.plans[2].cta.feedback = "immediate";
  return spec;
}

function recorder(): Telemetry & { events: TrackedEvent[] } {
  const events: TrackedEvent[] = [];
  return { events, track: async (e) => (events.push(e), true) };
}

function answer(body: unknown, status = 200) {
  return vi.fn(async () => new Response(JSON.stringify(body), { status }));
}

async function renderWith(fetchImpl: typeof fetch, baseUrl: string | null = BASE) {
  const seen: { spec: UiSpec; attribution: { specHash?: string; specVersionId?: string } }[] = [];
  const view = render(
    <ActiveGenerationPage
      baseUrl={baseUrl}
      fetchImpl={fetchImpl}
      telemetry={recorder()}
      renderSpec={(spec, attribution) => {
        seen.push({ spec, attribution });
        return <main data-generation={spec.generation}>{spec.page.title}</main>;
      }}
    />,
  );
  await waitFor(() => expect(view.container.querySelector("main[aria-busy]")).toBeNull());
  return { ...view, seen };
}

describe("active generation", () => {
  it("renders the promoted generation with its server-issued identity for telemetry", async () => {
    const fetchImpl = answer({
      status: "active",
      generation: 1,
      spec_version_id: VERSION_ID,
      spec_hash: HASH,
      spec: generation1(),
    });
    const { container, seen } = await renderWith(fetchImpl);

    expect(container.querySelector("main")?.dataset.generation).toBe("1");
    expect(seen.at(-1)?.attribution).toEqual({ specHash: HASH, specVersionId: VERSION_ID });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${BASE}${ACTIVE_GENERATION_PATH}?page=pricing_signup`);
    expect(init.method).toBeUndefined(); // a plain GET: read-only, nothing to promote here
    expect(init.credentials).toBe("omit");
  });

  it("renders the real SpecPage for an active Generation 0", async () => {
    const fetchImpl = answer({
      status: "active",
      generation: 0,
      spec_version_id: VERSION_ID,
      spec_hash: HASH,
      spec: generation0,
    });
    const view = render(
      <ActiveGenerationPage baseUrl={BASE} fetchImpl={fetchImpl} telemetry={recorder()} />,
    );
    await waitFor(() => expect(screen.getByText(/Generation 0 · rendered/)).toBeTruthy());
    expect(view.container.querySelector("main")?.dataset.generation).toBe("0");
  });
});

describe("fail closed: bundled Generation 0", () => {
  it.each([
    ["no active generation", answer({ status: "none" })],
    ["a 500", answer({ status: "active" }, 500)],
    ["a network error", vi.fn(async () => Promise.reject(new Error("offline")))],
    ["a malformed body", answer({ status: "active", generation: "1" })],
    ["an extra field", answer({ status: "none", promoted_by: "someone" })],
    [
      "an invalid spec (unknown key)",
      answer({
        status: "active",
        generation: 1,
        spec_version_id: VERSION_ID,
        spec_hash: HASH,
        spec: { ...generation1(), onClick: "alert(1)" },
      }),
    ],
    [
      "a generation that does not match its spec",
      answer({
        status: "active",
        generation: 3,
        spec_version_id: VERSION_ID,
        spec_hash: HASH,
        spec: generation1(),
      }),
    ],
  ])("%s", async (_label, fetchImpl) => {
    const { container, seen } = await renderWith(fetchImpl as unknown as typeof fetch);

    expect(container.querySelector("main")?.dataset.generation).toBe("0");
    expect(seen.at(-1)?.attribution).toEqual({}); // bundled: no server identity to claim
  });

  it("no backend configured: bundled Generation 0 at once, without a request", async () => {
    const fetchImpl = answer({ status: "none" });
    const { container } = await renderWith(fetchImpl, null);

    expect(fetchImpl).not.toHaveBeenCalled();
    expect(container.querySelector("main")?.dataset.generation).toBe("0");
  });
});
