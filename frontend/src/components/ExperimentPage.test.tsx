/** The experiment route: backend-assigned variant, exposure only after a successful render. */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ASSIGNMENT_PATH } from "@/lib/experiments/assignment";
import type { Telemetry, TrackedEvent } from "@/lib/telemetry/client";
import generation0 from "@/ui-spec/generation-0.json";

import { ExperimentPage } from "./ExperimentPage";
import { SpecPage } from "./SpecPage";

const BASE = "http://127.0.0.1:8000";
const SESSION = "5b2f0c8e-4c1a-4a5e-9d6f-0a1b2c3d4e5f";
const KEY = "rage_fix_pricing";
const CONTROL_HASH = "a".repeat(64);
const CANDIDATE_HASH = "b".repeat(64);

function candidateSpec() {
  const spec = structuredClone(generation0) as typeof generation0 & {
    generation: number;
  };
  spec.generation = 1;
  const grid = spec.page.sections[1].components[1] as {
    plans: { cta: { feedback: string } }[];
  };
  grid.plans[2].cta.feedback = "immediate"; // Team Pro: the Step 12/13 rage-click fix
  return spec;
}

function recorder(): Telemetry & { events: TrackedEvent[] } {
  const events: TrackedEvent[] = [];
  return { events, track: async (e) => (events.push(e), true) };
}

function answer(body: unknown, status = 200) {
  return vi.fn(async () => new Response(JSON.stringify(body), { status }));
}

async function renderWith(
  fetchImpl: typeof fetch,
  extra: Partial<Parameters<typeof ExperimentPage>[0]> = {},
) {
  const telemetry = recorder();
  const view = render(
    <ExperimentPage
      baseUrl={BASE}
      fetchImpl={fetchImpl}
      telemetry={telemetry}
      sessionId={() => SESSION}
      {...extra}
    />,
  );
  await waitFor(() => expect(view.container.querySelector("main[aria-busy]")).toBeNull());
  return { ...view, telemetry };
}

const experimentEvents = (events: TrackedEvent[]) =>
  events.filter((e) => e.type === "experiment_exposure" || e.type === "experiment_fallback");

describe("variant resolution", () => {
  it("asks for an assignment once, with the session id in the POST body (never the URL)", async () => {
    const fetchImpl = answer({ status: "none" });
    await renderWith(fetchImpl);

    const calls = fetchImpl.mock.calls as unknown as [string, RequestInit][];
    const assignments = calls.filter(([url]) => url.endsWith(ASSIGNMENT_PATH));
    expect(assignments).toHaveLength(1);
    const [url, init] = assignments[0];
    expect(url).toBe(`${BASE}${ASSIGNMENT_PATH}`);
    expect(url).not.toContain(SESSION);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("omit");
    expect(JSON.parse(String(init.body))).toEqual({ session_id: SESSION, page: "pricing_signup" });
    // "none" -> the active generation is asked for (read-only GET, no session id).
    const others = calls.filter(([url]) => !url.endsWith(ASSIGNMENT_PATH));
    expect(others.map(([url]) => url)).toEqual([
      `${BASE}/api/v1/generations/active?page=pricing_signup`,
    ]);
    expect(others[0][1].method).toBeUndefined();
  });

  it("no running experiment: Generation 0, no exposure", async () => {
    const { container, telemetry } = await renderWith(answer({ status: "none" }));

    expect(container.querySelector("main")?.dataset.generation).toBe("0");
    expect(experimentEvents(telemetry.events)).toEqual([]);
  });

  it("assigned candidate: renders the candidate and reports ONE exposure after rendering", async () => {
    const { container, telemetry } = await renderWith(
      answer({
        status: "assigned",
        experiment_key: KEY,
        variant: "candidate",
        spec_hash: CANDIDATE_HASH,
        spec: candidateSpec(),
      }),
    );

    expect(container.querySelector("main")?.dataset.generation).toBe("1");
    expect(experimentEvents(telemetry.events)).toEqual([
      {
        type: "experiment_exposure",
        experiment: KEY,
        variant: "candidate",
        specHash: CANDIDATE_HASH,
      },
    ]);
    // The candidate's behaviour, not just its data: Team Pro now reveals the form at once.
    fireEvent.click(screen.getByRole("button", { name: "Get started" }));
    expect(screen.getByRole("heading", { name: "Create your team workspace" })).toBeTruthy();
  });

  it("assigned control: Generation 0 from the server, exposure as control", async () => {
    const { container, telemetry } = await renderWith(
      answer({
        status: "assigned",
        experiment_key: KEY,
        variant: "control",
        spec_hash: CONTROL_HASH,
        spec: generation0,
      }),
    );

    expect(container.querySelector("main")?.dataset.generation).toBe("0");
    expect(experimentEvents(telemetry.events)).toEqual([
      {
        type: "experiment_exposure",
        experiment: KEY,
        variant: "control",
        specHash: CONTROL_HASH,
      },
    ]);
  });
});

describe("fail closed: Generation 0, never a false exposure", () => {
  it.each([
    ["an unknown key (onClick)", { ...candidateSpec(), onClick: "alert(1)" }],
    [
      "an unknown component type",
      {
        ...candidateSpec(),
        page: {
          id: "x",
          title: "x",
          sections: [
            {
              id: "s",
              spacing: "md",
              visibility: "always",
              components: [{ type: "script", id: "x" }],
            },
          ],
        },
      },
    ],
  ])("a candidate spec with %s is never rendered", async (_label, spec) => {
    const { container, telemetry } = await renderWith(
      answer({
        status: "assigned",
        experiment_key: KEY,
        variant: "candidate",
        spec_hash: CANDIDATE_HASH,
        spec,
      }),
    );

    expect(container.querySelector("main")?.dataset.generation).toBe("0");
    expect(container.querySelector("script")).toBeNull();
    expect(experimentEvents(telemetry.events)).toEqual([
      { type: "experiment_fallback", experiment: KEY, reason: "spec_invalid" },
    ]);
  });

  it("a candidate that throws while rendering falls back to Generation 0 with no exposure", async () => {
    const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
    const renderSpec = (spec: { generation: number }, client: Telemetry) => {
      if (spec.generation === 1) throw new Error("simulated render crash");
      return <SpecPage spec={spec as never} telemetry={client} />;
    };
    const { container, telemetry } = await renderWith(
      answer({
        status: "assigned",
        experiment_key: KEY,
        variant: "candidate",
        spec_hash: CANDIDATE_HASH,
        spec: candidateSpec(),
      }),
      { renderSpec },
    );
    await act(async () => {});
    quiet.mockRestore();

    expect(container.querySelector("main")?.dataset.generation).toBe("0");
    expect(experimentEvents(telemetry.events)).toEqual([
      { type: "experiment_fallback", experiment: KEY, reason: "render_error" },
    ]);
  });

  it("a server-side fallback is rendered as Generation 0 and reported", async () => {
    const { container, telemetry } = await renderWith(
      answer({
        status: "fallback",
        experiment_key: KEY,
        reason: "spec_hash_mismatch",
      }),
    );

    expect(container.querySelector("main")?.dataset.generation).toBe("0");
    expect(experimentEvents(telemetry.events)).toEqual([
      {
        type: "experiment_fallback",
        experiment: KEY,
        reason: "spec_hash_mismatch",
      },
    ]);
  });

  it.each([
    ["a network error", vi.fn(async () => Promise.reject(new Error("offline")))],
    ["a 500", answer({ status: "assigned" }, 500)],
    ["a malformed body", answer({ status: "assigned", experiment_key: KEY, variant: "treatment" })],
    ["an extra field", answer({ status: "none", winner: "candidate" })],
  ])("%s means Generation 0 and no experiment events", async (_label, fetchImpl) => {
    const { container, telemetry } = await renderWith(fetchImpl as unknown as typeof fetch);

    expect(container.querySelector("main")?.dataset.generation).toBe("0");
    expect(experimentEvents(telemetry.events)).toEqual([]);
  });

  it("no backend configured: Generation 0 without any request", async () => {
    const fetchImpl = answer({ status: "none" });
    const { container } = await renderWith(fetchImpl, { baseUrl: null });

    expect(fetchImpl).not.toHaveBeenCalled();
    expect(container.querySelector("main")?.dataset.generation).toBe("0");
  });
});
