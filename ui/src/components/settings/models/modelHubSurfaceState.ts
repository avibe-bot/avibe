import { foldRegionRead, type RegionRead } from './regionRead';
import type { AgentSupply, Source } from './types';

export type ModelsSurfaceKind = 'direct_empty' | 'gateway';

export const modelsSurfaceKind = (agents: AgentSupply[], sources: Source[]): ModelsSurfaceKind =>
  agents.every((agent) => agent.mode === 'direct') && sources.length === 0
    ? 'direct_empty'
    : 'gateway';

/** Frame 09 has no region failure surface, so only two fresh reads may select it. */
export const modelsSurfaceKindFromReads = (
  agentsRead: RegionRead<AgentSupply[]>,
  sourcesRead: RegionRead<Source[]>,
): ModelsSurfaceKind => {
  const agents = foldRegionRead<AgentSupply[], AgentSupply[] | null>(agentsRead, {
    loading: () => null,
    ready: (data) => data,
    unread: () => null,
    degraded: () => null,
  });
  const sources = foldRegionRead<Source[], Source[] | null>(sourcesRead, {
    loading: () => null,
    ready: (data) => data,
    unread: () => null,
    degraded: () => null,
  });
  return agents && sources ? modelsSurfaceKind(agents, sources) : 'gateway';
};
