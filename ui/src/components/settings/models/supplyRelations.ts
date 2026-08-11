import { modelChainKey, type ModelChainIndex } from './modelRows';
import { isTakeoverChain } from './takeover';
import type { AgentBackend, AgentSupply, Source } from './types';

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
  if (source.state.status === 'cooldown') return 'unavailable';
  let isCurrent = false;
  let isTakeover = false;
  for (const modelId of Object.keys(agent.routes ?? {})) {
    const read = chains[modelChainKey(agent.backend, modelId)];
    if (read?.kind !== 'ready' || !read.chain.current) continue;
    const current = read.chain.current;
    if (current.source_id !== source.id) continue;
    isCurrent = true;
    isTakeover ||= isTakeoverChain(read.chain);
  }
  if (isTakeover) return 'takeover';
  if (!isCurrent) return 'connected_unused';
  return source.supply_channel === 'native_cli' ? 'native' : 'gateway';
};

export function buildSupplyRelations(
  agents: AgentSupply[],
  sources: Source[],
  chains: ModelChainIndex,
): SupplyRelation[] {
  const sourceById = new Map(sources.map((source) => [source.id, source]));
  const relations: SupplyRelation[] = [];
  for (const agent of agents) {
    if (agent.mode !== 'hub') continue;
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
