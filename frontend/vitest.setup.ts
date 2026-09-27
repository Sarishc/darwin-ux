import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Unmount rendered trees between tests (Vitest globals are off).
afterEach(() => cleanup());
