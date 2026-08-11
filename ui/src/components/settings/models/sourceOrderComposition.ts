import { eligibleSources } from './eligibility';
import type { AgentSupply, Source } from './types';

export type SourceOrderComposition = {
  available: Source[];
  missingOrderedIds: string[];
};

/** A pair of individually authoritative reads may still describe different server moments. */
export const combineSourceOrderReads = (
  agent: AgentSupply,
  sources: Source[],
): SourceOrderComposition => {
  const inventoryIds = new Set(sources.map((source) => source.id));
  return {
    available: eligibleSources(sources, agent),
    missingOrderedIds: (agent.sources?.order ?? []).filter((sourceId) => !inventoryIds.has(sourceId)),
  };
};
