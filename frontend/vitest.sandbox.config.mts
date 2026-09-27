import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

// Runs ONLY the sandbox runner (see src/evaluation/run.eval.tsx); `npm test` never picks it up.
export default defineConfig({
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "jsdom",
    include: ["src/evaluation/run.eval.tsx"],
    setupFiles: ["./vitest.setup.ts"],
    reporters: ["dot"],
  },
});
