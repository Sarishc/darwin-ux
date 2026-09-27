import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ComponentSpec } from "@/ui-spec/schema";

import { registry, renderComponent, UnknownComponentError, validateField } from "./registry";

const ctx = { track: vi.fn(), runAction: vi.fn() };

describe("registry", () => {
  it("has exactly the allowlisted component types", () => {
    expect(Object.keys(registry).sort()).toEqual([
      "button",
      "heading",
      "notice",
      "plan_grid",
      "signup_form",
      "text",
    ]);
  });

  it("renders spec data as semantic elements", () => {
    render(
      <>
        {renderComponent({ type: "heading", id: "h", level: 2, text: "Plans" }, ctx)}
        {renderComponent({ type: "notice", id: "n", tone: "info", text: "Heads up" }, ctx)}
        {renderComponent(
          {
            type: "button",
            id: "b",
            label: "Go",
            variant: "primary",
            feedback: "immediate",
            action: "reveal_signup",
          },
          ctx,
        )}
      </>,
    );

    expect(screen.getByRole("heading", { level: 2, name: "Plans" })).toBeTruthy();
    expect(screen.getByRole("note").textContent).toBe("Heads up");
    expect(screen.getByRole("button", { name: "Go" }).getAttribute("type")).toBe("button");
  });

  it("renders markup-looking text as plain text, never as HTML", () => {
    const { container } = render(
      renderComponent(
        { type: "text", id: "t", emphasis: "normal", text: "<img src=x onerror=alert(1)>" },
        ctx,
      ),
    );

    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toBe("<img src=x onerror=alert(1)>");
  });

  it.each(["iframe", "script", "constructor", "toString", "__proto__", undefined])(
    "fails loudly on unknown type %s",
    (type) => {
      const spec = { type, id: "x" } as unknown as ComponentSpec;

      expect(() => renderComponent(spec, ctx)).toThrow(UnknownComponentError);
    },
  );
});

describe("form validation", () => {
  it.each([
    ["email", "", "required"],
    ["email", "   ", "required"],
    ["email", "not-an-email", "invalid_format"],
    ["email", "a@b.co", null],
    ["team_name", "", "required"],
    ["team_name", "Platform", null],
  ] as const)("%s = %j -> %s", (field, value, expected) => {
    expect(validateField(field, value)).toBe(expected);
  });
});
