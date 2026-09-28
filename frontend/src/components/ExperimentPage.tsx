"use client";

/**
 * Renders the variant the BACKEND assigned to this anonymous session, through
 * the same real UI Spec schema, component registry and SpecPage as /demo.
 *
 *   assignment "none"              -> bundled Generation 0, no exposure
 *   assignment "fallback"          -> bundled Generation 0 + experiment_fallback event
 *   assigned, spec fails the schema -> bundled Generation 0 + experiment_fallback (spec_invalid)
 *   assigned, render throws         -> bundled Generation 0 + experiment_fallback (render_error)
 *   assigned, rendered              -> the assigned spec + ONE experiment_exposure event
 *
 * ASSIGNED is not EXPOSED: the exposure event is sent from an effect that runs
 * only after the assigned spec has committed to the DOM. A session that falls
 * back never counts as an exposure of either variant. Both variants take the
 * same path (fetch -> validate -> render), so neither loads differently.
 * No candidate code, no raw HTML: candidates are data, rendered by the registry.
 */
import { Component, type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { SpecPage } from "@/components/SpecPage";
import { requestAssignment } from "@/lib/experiments/assignment";
import {
  createTelemetry,
  type ExperimentFallbackReason,
  type ExperimentVariant,
  type Telemetry,
} from "@/lib/telemetry/client";
import { DARWIN_API_BASE_URL } from "@/lib/telemetry/config";
import { getSessionId } from "@/lib/telemetry/session";
import generation0 from "@/ui-spec/generation-0.json";
import { parseUiSpec, type UiSpec, uiSpec } from "@/ui-spec/schema";

const GENERATION_0 = parseUiSpec(generation0);

type View =
  | { kind: "loading" }
  | { kind: "generation0" }
  | { kind: "fallback"; experiment: string; reason: ExperimentFallbackReason }
  | {
      kind: "variant";
      experiment: string;
      variant: ExperimentVariant;
      specHash: string;
      spec: UiSpec;
    };

class RenderBoundary extends Component<
  { onError: () => void; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch() {
    this.props.onError(); // no error details leave the page
  }

  render() {
    return this.state.failed ? null : this.props.children;
  }
}

/** Calls the renderer INSIDE the boundary, so anything it throws is caught there. */
function Rendered({
  spec,
  client,
  renderSpec,
}: {
  spec: UiSpec;
  client: Telemetry;
  renderSpec: (spec: UiSpec, telemetry: Telemetry) => ReactNode;
}) {
  return renderSpec(spec, client);
}

/** Fires once, after its siblings (the assigned spec) committed successfully. */
function ExposureBeacon({ onExposed }: { onExposed: () => void }) {
  const sent = useRef(false);
  useEffect(() => {
    if (sent.current) return; // once per mount, also under React StrictMode
    sent.current = true;
    onExposed();
  }, [onExposed]);
  return null;
}

export interface ExperimentPageProps {
  page?: string;
  baseUrl?: string | null;
  fetchImpl?: typeof fetch;
  telemetry?: Telemetry;
  sessionId?: () => string;
  /** Test seam: how a validated spec is rendered. Defaults to the real SpecPage. */
  renderSpec?: (spec: UiSpec, telemetry: Telemetry) => ReactNode;
}

export function ExperimentPage({
  page = "pricing_signup",
  baseUrl = DARWIN_API_BASE_URL,
  fetchImpl,
  telemetry,
  sessionId = getSessionId,
  renderSpec = (spec, client) => <SpecPage spec={spec} telemetry={client} />,
}: ExperimentPageProps) {
  const [view, setView] = useState<View>({ kind: "loading" });

  useEffect(() => {
    let active = true;
    void requestAssignment({
      baseUrl,
      sessionId: sessionId(),
      page,
      fetchImpl,
    }).then((a) => {
      if (!active) return;
      if (a.status === "none") return setView({ kind: "generation0" });
      if (a.status === "fallback")
        return setView({
          kind: "fallback",
          experiment: a.experiment_key,
          reason: a.reason,
        });
      const parsed = uiSpec.safeParse(a.spec); // the app's real schema; invalid is never rendered
      if (!parsed.success)
        return setView({
          kind: "fallback",
          experiment: a.experiment_key,
          reason: "spec_invalid",
        });
      setView({
        kind: "variant",
        experiment: a.experiment_key,
        variant: a.variant,
        specHash: a.spec_hash,
        spec: parsed.data,
      });
    });
    return () => {
      active = false;
    };
  }, [baseUrl, fetchImpl, page, sessionId]);

  const generation = view.kind === "variant" ? view.spec.generation : 0;
  const client = useMemo(
    () => telemetry ?? createTelemetry({ baseUrl, generation }),
    [telemetry, baseUrl, generation],
  );

  const reported = useRef(false);
  useEffect(() => {
    if (view.kind !== "fallback" || reported.current) return;
    reported.current = true;
    void client.track({
      type: "experiment_fallback",
      experiment: view.experiment,
      reason: view.reason,
    });
  }, [view, client]);

  if (view.kind === "loading") {
    return (
      <main className="spec-page" aria-busy="true">
        <p className="text emphasis-normal" role="status">
          Loading…
        </p>
      </main>
    );
  }
  if (view.kind !== "variant") return renderSpec(GENERATION_0, client);

  const { experiment, variant, specHash } = view;
  return (
    <RenderBoundary
      onError={() => setView({ kind: "fallback", experiment, reason: "render_error" })}
    >
      <Rendered spec={view.spec} client={client} renderSpec={renderSpec} />
      <ExposureBeacon
        onExposed={() =>
          void client.track({
            type: "experiment_exposure",
            experiment,
            variant,
            specHash,
          })
        }
      />
    </RenderBoundary>
  );
}
