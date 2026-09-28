"use client";

/**
 * Renders the page's ACTIVE generation (Step 15): Generation 0 until a human promotes
 * a candidate, then the promoted generation, and Generation 0 again after a rollback.
 *
 *   backend answers "active" with a spec that passes the real UI Spec schema
 *        -> render it; telemetry carries its generation, spec hash and version id
 *   anything else (no backend, timeout, error, "none", invalid spec, generation mismatch)
 *        -> render the bundled Generation 0; telemetry claims generation 0 only
 *
 * Rendering always goes through SpecPage and the component registry. There are no
 * admin controls here: promotion and rollback are CLI-only.
 */
import { type ReactNode, useEffect, useState } from "react";

import { SpecPage } from "@/components/SpecPage";
import { requestActiveGeneration } from "@/lib/generations/active";
import type { Telemetry } from "@/lib/telemetry/client";
import { DARWIN_API_BASE_URL } from "@/lib/telemetry/config";
import generation0 from "@/ui-spec/generation-0.json";
import { parseUiSpec, type UiSpec, uiSpec } from "@/ui-spec/schema";

export const BUNDLED_GENERATION_0 = parseUiSpec(generation0);

type View =
  | { kind: "loading" }
  | { kind: "bundled" }
  | { kind: "active"; spec: UiSpec; specHash: string; specVersionId: string };

export interface ActiveGenerationPageProps {
  page?: string;
  baseUrl?: string | null;
  fetchImpl?: typeof fetch;
  telemetry?: Telemetry;
  /** Test seam / reuse: how a spec is rendered. Defaults to the real SpecPage. */
  renderSpec?: (
    spec: UiSpec,
    attribution: { specHash?: string; specVersionId?: string },
  ) => ReactNode;
}

export function ActiveGenerationPage({
  page = "pricing_signup",
  baseUrl = DARWIN_API_BASE_URL,
  fetchImpl,
  telemetry,
  renderSpec,
}: ActiveGenerationPageProps) {
  const [view, setView] = useState<View>(() =>
    baseUrl?.trim() ? { kind: "loading" } : { kind: "bundled" },
  );

  useEffect(() => {
    if (!baseUrl?.trim()) return;
    let active = true;
    void requestActiveGeneration({ baseUrl, page, fetchImpl }).then((answer) => {
      if (!active) return;
      if (answer.status !== "active") return setView({ kind: "bundled" });
      const parsed = uiSpec.safeParse(answer.spec); // the app's real schema
      if (!parsed.success || parsed.data.generation !== answer.generation) {
        return setView({ kind: "bundled" });
      }
      setView({
        kind: "active",
        spec: parsed.data,
        specHash: answer.spec_hash,
        specVersionId: answer.spec_version_id,
      });
    });
    return () => {
      active = false;
    };
  }, [baseUrl, fetchImpl, page]);

  if (view.kind === "loading") {
    return (
      <main className="spec-page" aria-busy="true">
        <p className="text emphasis-normal" role="status">
          Loading…
        </p>
      </main>
    );
  }
  const spec = view.kind === "active" ? view.spec : BUNDLED_GENERATION_0;
  const attribution =
    view.kind === "active" ? { specHash: view.specHash, specVersionId: view.specVersionId } : {};
  if (renderSpec) return renderSpec(spec, attribution);
  return (
    <SpecPage
      key={attribution.specVersionId ?? "bundled"}
      spec={spec}
      telemetry={telemetry}
      {...attribution}
    />
  );
}
