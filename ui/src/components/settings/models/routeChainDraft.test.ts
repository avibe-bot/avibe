import { describe, expect, it } from "vitest";

import {
  reorderRouteDraft,
  routeCandidates,
  validateRouteDraft,
} from "./routeChainDraft";
import type { AgentSupply, RouteHop, Source } from "./types";

const source = (id: string, models: string[]): Source => ({
  id,
  last_discovered_at: null,
  kind: "api_key",
  vendor: "anthropic",
  display_name: id.toUpperCase(),
  protocol: "anthropic",
  supply_channel: "hub",
  billing: "metered",
  state: { status: "active", retry_at: null, detail_key: null },
  models: models.map((modelId) => ({
    id: modelId,
    origin: "discovered",
    reasoning_efforts: [],
    reasoning_efforts_source: null,
  })),
});

const agent: AgentSupply = {
  backend: "claude",
  cli_present: true,
  mode: "hub",
  menu_kind: "fixed",
  sources: {
    order: ["src_b", "src_a"],
    eligibility: [
      { source_id: "src_a", eligible: true },
      { source_id: "src_b", eligible: true },
    ],
  },
};

const sources = [
  source("src_a", ["model-a", "model-b"]),
  source("src_b", ["model-a"]),
];

describe("routeChainDraft", () => {
  it("builds eligible candidates without duplicate exact hops", () => {
    const candidates = routeCandidates(agent, sources, [
      { source_id: "src_a", model_id: "model-a" },
    ]);

    expect(candidates.map(({ hop }) => hop)).toEqual([
      { source_id: "src_b", model_id: "model-a" },
      { source_id: "src_a", model_id: "model-b" },
    ]);
  });

  it("keeps unchanged stale hops but validates changed and duplicate hops", () => {
    const stale: RouteHop = { source_id: "missing", model_id: "old-model" };
    const origin = [stale];

    expect(validateRouteDraft(agent, sources, origin, origin)).toEqual({
      invalidIndexes: [],
      valid: true,
    });
    expect(validateRouteDraft(agent, sources, [], [stale])).toEqual({
      invalidIndexes: [0],
      valid: false,
    });
    expect(
      validateRouteDraft(
        agent,
        sources,
        [],
        [
          { source_id: "src_a", model_id: "model-a" },
          { source_id: "src_a", model_id: "model-a" },
        ],
      ),
    ).toEqual({ invalidIndexes: [0, 1], valid: false });
  });

  it("sorts listed sources first and preserves stable order for ties", () => {
    const draft: RouteHop[] = [
      { source_id: "missing", model_id: "stale-1" },
      { source_id: "src_a", model_id: "model-b" },
      { source_id: "src_b", model_id: "model-a" },
      { source_id: "missing", model_id: "stale-2" },
    ];

    expect(reorderRouteDraft(agent, draft)).toEqual([
      { source_id: "src_b", model_id: "model-a" },
      { source_id: "src_a", model_id: "model-b" },
      { source_id: "missing", model_id: "stale-1" },
      { source_id: "missing", model_id: "stale-2" },
    ]);
  });
});
