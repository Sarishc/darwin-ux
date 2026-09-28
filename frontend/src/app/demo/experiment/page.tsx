import type { Metadata } from "next";

import { ExperimentPage } from "@/components/ExperimentPage";

// The experiment route: the backend assigns Generation 0 or a sandbox-approved
// candidate to this anonymous session. /demo itself stays plain Generation 0.
export const metadata: Metadata = { title: "Notewise — plans" };

export default function DemoExperimentPage() {
  return <ExperimentPage page="pricing_signup" />;
}
