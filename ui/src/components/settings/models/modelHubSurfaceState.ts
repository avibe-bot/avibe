import { foldRegionRead, type RegionRead } from './regionRead';
import type { AgentSupply, Source } from './types';
import { getBackendUiMeta } from '@/lib/agentBackends';

export type ModelsSurfaceKind = 'direct_empty' | 'gateway';

/** Match runtime_stop: the upgrade-added empty Avibe catalog holds no runtime. */
export const agentUsesHubRuntime = (agent: AgentSupply): boolean =>
  agent.mode === 'hub' && (getBackendUiMeta(agent.backend).capabilities.supports_cli || (agent.catalog_models?.length ?? 0) > 0);

export const modelsSurfaceKind = (agents: AgentSupply[], sources: Source[]): ModelsSurfaceKind =>
  // An empty in-process catalog still needs the gateway card's model editor.
  // Whether it currently holds the runtime is a separate question above.
  !agents.some((agent) => agent.mode === 'hub') && sources.length === 0
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
