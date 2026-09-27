/**
 * Validate UI Spec JSON files with the app's REAL runtime schema (src/ui-spec/schema.ts).
 *
 *   npm run --silent validate-spec -- <file.json> [...]     one JSON line per file; exit 1 if any invalid
 *   npm run --silent validate-spec -- --json-schema          print the schema as JSON Schema
 *
 * The backend mutates UI Specs in Python; this is how it proves a candidate is
 * accepted by the exact Zod schema the demo renders with — no second copy of
 * the rules. Reads files only; writes nothing.
 */
import { readFileSync } from "node:fs";
import { z } from "zod";
import { uiSpec } from "../src/ui-spec/schema.ts";

const args = process.argv.slice(2);

if (args[0] === "--json-schema") {
  // superRefine rules (unique ids) cannot be expressed in JSON Schema; they still run in validation.
  console.log(JSON.stringify(z.toJSONSchema(uiSpec, { unrepresentable: "any" })));
  process.exit(0);
}

if (args.length === 0) {
  console.error("usage: validate-spec <file.json> [...] | --json-schema");
  process.exit(2);
}

let failures = 0;
for (const path of args) {
  let result: { path: string; ok: boolean; issues: string[] };
  try {
    const parsed = uiSpec.safeParse(JSON.parse(readFileSync(path, "utf8")));
    result = parsed.success
      ? { path, ok: true, issues: [] }
      : {
          path,
          ok: false,
          issues: parsed.error.issues.map((i) => `${i.path.join(".") || "(root)"}: ${i.message}`),
        };
  } catch (error) {
    result = { path, ok: false, issues: [`unreadable: ${(error as Error).name}`] };
  }
  if (!result.ok) failures += 1;
  console.log(JSON.stringify(result));
}
process.exit(failures ? 1 : 0);
