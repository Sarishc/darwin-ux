import type { Metadata } from "next";

import { ActiveGenerationPage } from "@/components/ActiveGenerationPage";
import generation0 from "@/ui-spec/generation-0.json";
import { parseUiSpec } from "@/ui-spec/schema";

// Validated at module load, on the server: an invalid bundled spec fails the build.
// The committed Generation 0 file is never edited. It is the bootstrap artifact and
// the fallback; the page renders the ACTIVE generation from the backend when one is
// configured and reachable (Step 15).
const spec = parseUiSpec(generation0);

export const metadata: Metadata = { title: spec.page.title };

export default function DemoPage() {
  return <ActiveGenerationPage page={spec.page.id} />;
}
