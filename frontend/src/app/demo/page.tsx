import type { Metadata } from "next";

import { SpecPage } from "@/components/SpecPage";
import generation0 from "@/ui-spec/generation-0.json";
import { parseUiSpec } from "@/ui-spec/schema";

// Validated at module load, on the server: an invalid spec fails the build and
// never reaches a browser. The committed Generation 0 file is never edited;
// later generations will be new files.
const spec = parseUiSpec(generation0);

export const metadata: Metadata = { title: spec.page.title };

export default function DemoPage() {
  return <SpecPage spec={spec} />;
}
