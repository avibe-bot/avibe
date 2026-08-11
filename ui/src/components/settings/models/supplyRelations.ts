import { modelChainKey, type ModelChainIndex } from './modelRows';
import { freshRegionData } from './regionRead';
import { agentHasLiveChainProjection, type FreshRuntimeProjection } from './runtimeLifecycle';
import { classifyChainLink, classifySourceStatus } from './sourceStateClassification';
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
  let isCurrent = false;
  let isTakeover = false;
  let isUnavailable = false;
  let hasRunnableLink = false;
  for (const modelId of Object.keys(agent.routes ?? {})) {
    const read = chains[modelChainKey(agent.backend, modelId)];
    const chain = read ? freshRegionData(read) : undefined;
    if (!chain) continue;
    for (const link of chain.chain.filter((candidate) => candidate.source_id === source.id)) {
      hasRunnableLink ||= link.runnable;
      if (!link.runnable && classifyChainLink(link) !== null) isUnavailable = true;
    }
    if (chain.current?.source_id === source.id && chain.current.model_id === modelId) {
      isCurrent = true;
      isTakeover ||= isTakeoverChain(chain);
    }
  }
  if (isTakeover) return 'takeover';
  if (isCurrent) return source.supply_channel === 'native_cli' ? 'native' : 'gateway';
  if ((!hasRunnableLink && isUnavailable) || classifySourceStatus(source.state.status) !== null) return 'unavailable';
  return 'connected_unused';
};

export function buildSupplyRelations(
  agents: AgentSupply[],
  sources: Source[],
  chains: ModelChainIndex,
  runtime: FreshRuntimeProjection | null,
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
