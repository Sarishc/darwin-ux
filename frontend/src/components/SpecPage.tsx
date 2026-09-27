"use client";

/**
 * Renders a whole (already validated) UI Spec through the registry and wires
 * the two things renderers may do: telemetry and allowlisted actions.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { renderComponent, type RenderContext } from "@/components/registry";
import { createTelemetry, type Telemetry, type TrackedEvent } from "@/lib/telemetry/client";
import { DARWIN_API_BASE_URL } from "@/lib/telemetry/config";
import type { UiSpec } from "@/ui-spec/schema";

export function SpecPage({ spec, telemetry }: { spec: UiSpec; telemetry?: Telemetry }) {
  const [signupRevealed, setSignupRevealed] = useState(false);
  const signupRef = useRef<HTMLElement | null>(null);

  const client = useMemo(
    () =>
      telemetry ??
      createTelemetry({
        baseUrl: DARWIN_API_BASE_URL,
        generation: spec.generation,
        onDropped:
          process.env.NODE_ENV === "development"
            ? (reason) => console.warn(`[telemetry] event not delivered: ${reason}`)
            : undefined,
      }),
    [telemetry, spec.generation],
  );

  const track = useCallback(
    (event: TrackedEvent) => {
      void client.track(event); // fire-and-forget: the UI never waits for telemetry
    },
    [client],
  );

  const ctx: RenderContext = useMemo(
    () => ({
      track,
      runAction: (action) => {
        if (action === "reveal_signup") setSignupRevealed(true);
      },
    }),
    [track],
  );

  const viewed = useRef(false);
  useEffect(() => {
    if (viewed.current) return; // once per mount, also under React StrictMode
    viewed.current = true;
    track({ type: "page_view", page: spec.page.id });
  }, [track, spec.page.id]);

  useEffect(() => {
    if (signupRevealed) signupRef.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [signupRevealed]);

  return (
    <main
      className="spec-page"
      data-page={spec.page.id}
      data-generation={spec.generation}
      data-spec-version={spec.version}
    >
      {spec.page.sections.map((section) => {
        if (section.visibility === "after_signup_reveal" && !signupRevealed) return null;
        return (
          <section
            key={section.id}
            data-section={section.id}
            className={`section space-${section.spacing}`}
            ref={section.visibility === "after_signup_reveal" ? signupRef : undefined}
          >
            {section.components.map((component) => renderComponent(component, ctx))}
          </section>
        );
      })}
      <footer className="generation-badge">
        Generation {spec.generation} · rendered from UI Spec v{spec.version}
      </footer>
    </main>
  );
}
