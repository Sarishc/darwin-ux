/** The Generation 0 page end to end in the DOM, with a recording telemetry client. */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createTelemetry, type Telemetry, type TrackedEvent } from "@/lib/telemetry/client";
import generation0 from "@/ui-spec/generation-0.json";
import { parseUiSpec } from "@/ui-spec/schema";

import { DELAYED_FEEDBACK_MS } from "./registry";
import { SpecPage } from "./SpecPage";

const spec = parseUiSpec(generation0);
const EMAIL_VALUE = "jane.doe.secret@example.com";
const TEAM_VALUE = "Project Nightingale";

function recorder(): Telemetry & { events: TrackedEvent[] } {
  const events: TrackedEvent[] = [];
  return {
    events,
    track: async (event) => {
      events.push(event);
      return true;
    },
  };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

function revealSignup() {
  fireEvent.click(screen.getByRole("button", { name: "Get started" }));
  act(() => vi.advanceTimersByTime(DELAYED_FEEDBACK_MS));
}

describe("Generation 0 page", () => {
  it("renders from the UI Spec and knows it is generation 0", () => {
    const { container } = render(<SpecPage spec={spec} telemetry={recorder()} />);

    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
      "Shared notes for small teams",
    );
    expect(screen.getAllByRole("article")).toHaveLength(3);
    expect(container.querySelector("main")?.dataset.generation).toBe("0");
    expect(screen.getByText(/Generation 0 · rendered from UI Spec v1/)).toBeTruthy();
    expect(screen.queryByRole("form")).toBeNull(); // signup hidden until a plan is chosen
  });

  it("sends a page_view once", () => {
    const telemetry = recorder();

    render(<SpecPage spec={spec} telemetry={telemetry} />);

    expect(telemetry.events).toEqual([{ type: "page_view", page: "pricing_signup" }]);
  });

  it("delayed CTA: nothing happens at first, every click is observed (rage-click surface)", () => {
    const telemetry = recorder();
    render(<SpecPage spec={spec} telemetry={telemetry} />);
    const cta = screen.getByRole("button", { name: "Get started" });

    for (let i = 0; i < 4; i++) fireEvent.click(cta);
    act(() => vi.advanceTimersByTime(DELAYED_FEEDBACK_MS - 1));
    expect(screen.queryByText("Create your team workspace")).toBeNull(); // still no feedback

    act(() => vi.advanceTimersByTime(1));
    expect(screen.getByText("Create your team workspace")).toBeTruthy();
    const clicks = telemetry.events.filter((e) => e.type === "button_click");
    expect(clicks).toEqual(
      Array.from({ length: 4 }, () => ({ type: "button_click", component: "plan_team_pro_cta" })),
    );
  });

  it("immediate CTAs reveal the form at once", () => {
    render(<SpecPage spec={spec} telemetry={recorder()} />);

    fireEvent.click(screen.getByRole("button", { name: "Choose Team" }));

    expect(screen.getByText("Create your team workspace")).toBeTruthy();
  });

  it("form errors are reported by field and reason — never with the typed values", () => {
    const telemetry = recorder();
    render(<SpecPage spec={spec} telemetry={telemetry} />);
    revealSignup();

    fireEvent.change(screen.getByLabelText("Work email"), { target: { value: "jane.doe.secret" } });
    fireEvent.change(screen.getByLabelText("Team name"), { target: { value: TEAM_VALUE } });
    fireEvent.click(screen.getByRole("button", { name: "Create workspace" }));
    fireEvent.change(screen.getByLabelText("Team name"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Create workspace" }));

    const errors = telemetry.events.filter((e) => e.type === "form_error");
    expect(errors).toEqual([
      { type: "form_error", component: "signup_form", field: "email", reason: "invalid_format" },
      { type: "form_error", component: "signup_form", field: "email", reason: "invalid_format" },
      { type: "form_error", component: "signup_form", field: "team_name", reason: "required" },
    ]);
    // Generation 0 friction: one vague summary, no per-field help.
    expect(screen.getByRole("alert").textContent).toBe("Some details are missing or invalid.");
    expect(screen.queryByText("Enter a valid email address.")).toBeNull();
    const serialized = JSON.stringify(telemetry.events);
    expect(serialized).not.toContain("jane.doe.secret");
    expect(serialized).not.toContain(TEAM_VALUE);
  });

  it("no typed value appears in any request body sent to the backend", async () => {
    const bodies: string[] = [];
    const fetchImpl = vi.fn(async (_url: unknown, init?: RequestInit) => {
      bodies.push(String(init?.body));
      return new Response(null, { status: 202 });
    });
    const telemetry = createTelemetry({
      baseUrl: "http://api.test",
      generation: 0,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    render(<SpecPage spec={spec} telemetry={telemetry} />);
    revealSignup();

    fireEvent.change(screen.getByLabelText("Work email"), { target: { value: EMAIL_VALUE } });
    fireEvent.click(screen.getByRole("button", { name: "Create workspace" })); // team missing
    await act(async () => {});

    expect(bodies.length).toBeGreaterThan(0);
    for (const body of bodies) {
      expect(body).not.toContain(EMAIL_VALUE);
      expect(body).not.toContain("jane.doe");
    }
  });

  it("successful local signup shows completion and stores nothing", () => {
    render(<SpecPage spec={spec} telemetry={recorder()} />);
    revealSignup();

    fireEvent.change(screen.getByLabelText("Work email"), { target: { value: EMAIL_VALUE } });
    fireEvent.change(screen.getByLabelText("Team name"), { target: { value: TEAM_VALUE } });
    fireEvent.click(screen.getByRole("button", { name: "Create workspace" }));

    expect(screen.getByRole("status").textContent).toBe(
      "Demo signup completed — no account was created.",
    );
    expect(localStorage.length).toBe(0);
  });

  it("works exactly the same when every telemetry request fails", async () => {
    const failing = createTelemetry({
      baseUrl: "http://api.test",
      generation: 0,
      fetchImpl: (async () => Promise.reject(new TypeError("Failed to fetch"))) as typeof fetch,
    });
    render(<SpecPage spec={spec} telemetry={failing} />);

    revealSignup();
    await act(async () => {});
    fireEvent.change(screen.getByLabelText("Work email"), { target: { value: EMAIL_VALUE } });
    fireEvent.change(screen.getByLabelText("Team name"), { target: { value: TEAM_VALUE } });
    fireEvent.click(screen.getByRole("button", { name: "Create workspace" }));

    expect(screen.getByRole("status").textContent).toContain("Demo signup completed");
  });

  it("form inputs are labelled and the submit is a real button", () => {
    render(<SpecPage spec={spec} telemetry={recorder()} />);
    revealSignup();

    expect(screen.getByLabelText("Work email").getAttribute("type")).toBe("email");
    expect(screen.getByLabelText("Team name").tagName).toBe("INPUT");
    expect(screen.getByRole("button", { name: "Create workspace" }).getAttribute("type")).toBe(
      "submit",
    );
  });
});
