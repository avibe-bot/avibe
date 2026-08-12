import { describe, expect, it } from "vitest";

import {
  advanceRouteChainInteraction,
  createRouteChainInteraction,
} from "./routeChainInteraction";
import type { RouteHop } from "./types";

const hops: RouteHop[] = [
  { source_id: "src_a", model_id: "model_a" },
  { source_id: "src_b", model_id: "model_b" },
  { source_id: "src_c", model_id: "model_c" },
];

describe("route chain interaction owner", () => {
  it("moves focus and the grabbed identity together without replacing its snapshot", () => {
    const grabbed = advanceRouteChainInteraction(
      createRouteChainInteraction(hops),
      { type: "begin-grab", index: 1 },
    );
    const moved = advanceRouteChainInteraction(grabbed, {
      type: "move-grab",
      direction: -1,
    });

    expect(moved.focusIndex).toBe(0);
    expect(moved.grab?.index).toBe(0);
    expect(moved.draft.map((hop) => hop.source_id)).toEqual([
      "src_b",
      "src_a",
      "src_c",
    ]);
    expect(
      advanceRouteChainInteraction(moved, { type: "cancel-grab" }).draft,
    ).toEqual(hops);
  });

  it("owns the focus result for removals and appends", () => {
    const removed = advanceRouteChainInteraction(
      createRouteChainInteraction(hops),
      { type: "remove", index: 1 },
    );
    expect(removed.focusIndex).toBe(1);

    const appended = advanceRouteChainInteraction(removed, {
      type: "append",
      hop: hops[1],
    });
    expect(appended.focusIndex).toBe(2);
    expect(appended.draft).toEqual([hops[0], hops[2], hops[1]]);
  });
});
