import { eligibleSources } from './eligibility';
import type { AgentSupply, Source } from './types';

export type SourceOrderComposition = {
  available: Source[];
  missingOrderedIds: string[];
  missingInventoryIds: string[];
  hasHole: boolean;
};

/** A pair of individually authoritative reads may still describe different server moments. */
export const combineSourceOrderReads = (
  agent: AgentSupply,
  sources: Source[],
): SourceOrderComposition => {
  const inventoryIds = new Set(sources.map((source) => source.id));
  const projectedIds = new Set([
    ...(agent.sources?.order ?? []),
    ...(agent.sources?.eligibility ?? []).map((entry) => entry.source_id),
  ]);
  const missingOrderedIds = (agent.sources?.order ?? []).filter((sourceId) => !inventoryIds.has(sourceId));
  const missingInventoryIds = sources
    .map((source) => source.id)
    .filter((sourceId) => !projectedIds.has(sourceId));
  return {
    available: eligibleSources(sources, agent),
    missingOrderedIds,
    missingInventoryIds,
    hasHole: missingOrderedIds.length > 0 || missingInventoryIds.length > 0,
  };
};
