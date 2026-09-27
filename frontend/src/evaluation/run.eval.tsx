/**
 * Sandbox runner entry (executed only by `npm run sandbox-harness`, never by the app).
 *
 *   SANDBOX_INPUT=<in.json> SANDBOX_OUTPUT=<out.json> npm run --silent sandbox-harness
 *
 * Input:  {"specs": {"<key>": <UI Spec JSON>, ...}}   (written by the backend to an OS temp dir)
 * Output: {"harness_version": "...", "facts": {"<key>": SpecFacts, ...}}
 *
 * Both paths come from the environment, never from spec content; specs are parsed
 * as data and rendered only through the real schema and registry.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { expect, test } from "vitest";

import { HARNESS_VERSION, inspectSpecs } from "./harness";

test("sandbox harness", async () => {
  const input = process.env.SANDBOX_INPUT;
  const output = process.env.SANDBOX_OUTPUT;
  expect(input && output, "SANDBOX_INPUT and SANDBOX_OUTPUT are required").toBeTruthy();
  const parsed = JSON.parse(readFileSync(input as string, "utf8")) as { specs?: unknown };
  const specs =
    parsed && typeof parsed.specs === "object" && parsed.specs !== null
      ? (parsed.specs as Record<string, unknown>)
      : {};
  const facts = await inspectSpecs(specs);
  writeFileSync(output as string, JSON.stringify({ harness_version: HARNESS_VERSION, facts }));
}, 120_000);
