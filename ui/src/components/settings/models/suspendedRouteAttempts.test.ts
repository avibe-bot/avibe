import { describe, expect, it } from "vitest";

import {
  emptySuspendedRouteAttempts,
  holdSuspendedRouteAttempt,
  releaseSuspendedRouteAttempt,
} from "./suspendedRouteAttempts";

describe("suspended route attempts", () => {
  it("settles only the matching backend", () => {
    const claude = {
      backend: "claude" as const,
      modelId: "claude-model",
      stage: "initial" as const,
      manual_override: { hops: [] },
      submitted: [],
    };
    const codex = {
      backend: "codex" as const,
      modelId: "codex-model",
      stage: "confirmed" as const,
      manual_override: { hops: [] },
      submitted: [],
    };
    const held = holdSuspendedRouteAttempt(
      holdSuspendedRouteAttempt(emptySuspendedRouteAttempts(), claude),
      codex,
    );

    const remaining = releaseSuspendedRouteAttempt(held, "codex");

    expect(remaining.get("claude")).toEqual(claude);
    expect(remaining.has("codex")).toBe(false);
  });
});
