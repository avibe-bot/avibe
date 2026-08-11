import { modelChainKey, type ModelChainIndex } from './modelRows';
import { agentHasLiveChainProjection } from './runtimeLifecycle';
import { isTakeoverChain } from './takeover';
import type { AgentBackend, AgentSupply, RuntimeDependency, Source } from './types';

export type SupplyRelationKind = 'native' | 'gateway' | 'connected_unused' | 'takeover' | 'unavailable';

export type SupplyRelation = {
  sourceId: string;
  backend: AgentBackend;
  kind: SupplyRelationKind;
};

const relationKind = (
  source: Source,
  agent: AgentSupply,
  chains: ModelChainIndex,
): SupplyRelationKind => {
  let isCurrent = false;
  let isTakeover = false;
  for (const modelId of Object.keys(agent.routes ?? {})) {
    const read = chains[modelChainKey(agent.backend, modelId)];
    if (read?.kind !== 'ready' || !read.data.current) continue;
    const current = read.data.current;
    if (current.source_id !== source.id) continue;
    isCurrent = true;
    isTakeover ||= isTakeoverChain(read.data);
  }
  if (isTakeover) return 'takeover';
  if (isCurrent) return source.supply_channel === 'native_cli' ? 'native' : 'gateway';
  if (source.state.status === 'cooldown') return 'unavailable';
  return 'connected_unused';
};

export function buildSupplyRelations(
  agents: AgentSupply[],
  sources: Source[],
  chains: ModelChainIndex,
  runtime: RuntimeDependency | null,
): SupplyRelation[] {
  const sourceById = new Map(sources.map((source) => [source.id, source]));
  const relations: SupplyRelation[] = [];
  for (const agent of agents) {
    if (!agentHasLiveChainProjection(runtime, agent)) continue;
    const sourceIds = new Set(
      Object.values(agent.routes ?? {}).flatMap((route) => route.hops.map((hop) => hop.source_id)),
    );
    for (const source of sources) {
      if (!sourceIds.has(source.id)) continue;
      const canonical = sourceById.get(source.id);
      if (canonical) relations.push({ sourceId: source.id, backend: agent.backend, kind: relationKind(canonical, agent, chains) });
    }
  }
  return relations;
}
