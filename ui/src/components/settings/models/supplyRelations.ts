import { modelChainKey, type ModelChainIndex } from './modelRows';
import { catalogModelIds } from './backendCatalog';
import { equalHopIdentity, hopBelongsToSource } from './hopIdentity';
import { foldRegionRead } from './regionRead';
import { agentHasLiveChainProjection, type FreshRuntimeProjection } from './runtimeLifecycle';
import { classifyChainLink, classifySourceStatus } from './sourceStateClassification';
import { isTakeoverChain } from './takeover';
import type { AgentBackend, AgentChain, AgentSupply, Source } from './types';

export type SupplyRelationKind = 'native' | 'gateway' | 'passthrough' | 'connected_unused' | 'takeover' | 'unavailable';

export type SupplyRelation = {
  sourceId: string;
  backend: AgentBackend;
  kind: SupplyRelationKind;
};

const relationKind = (
  source: Source,
  chains: readonly AgentChain[],
): SupplyRelationKind => {
  let isCurrent = false;
  let isTakeover = false;
  let isUnavailable = false;
  let hasRunnableLink = false;
  let passthroughOnly = true;
  for (const chain of chains) {
    for (const link of chain.chain.filter((candidate) => hopBelongsToSource(candidate, source.id))) {
      passthroughOnly &&= chain.route_origin === 'passthrough';
      hasRunnableLink ||= link.runnable;
      if (!link.runnable && classifyChainLink(link) !== null) isUnavailable = true;
      if (equalHopIdentity(chain.current, link)) {
        isCurrent = true;
        isTakeover ||= isTakeoverChain(chain);
      }
    }
  }
  if (isTakeover) return 'takeover';
  if (isCurrent) return source.supply_channel === 'native_cli' ? 'native' : passthroughOnly ? 'passthrough' : 'gateway';
  if ((!hasRunnableLink && isUnavailable) || classifySourceStatus(source.state.status) !== null) return 'unavailable';
  return 'connected_unused';
};

export function buildSupplyRelations(
  agents: AgentSupply[],
  sources: Source[],
  chains: ModelChainIndex,
  runtime: FreshRuntimeProjection | null,
): SupplyRelation[] {
  const relations: SupplyRelation[] = [];
  for (const agent of agents) {
    if (!agentHasLiveChainProjection(runtime, agent)) continue;
    const sourceIds = new Set<string>();
    const effectiveChains: AgentChain[] = [];
    for (const modelId of catalogModelIds(agent)) {
      const key = modelChainKey(agent.backend, modelId);
      const read = chains[key];
      const chain = read ? foldRegionRead(read, {
        loading: () => null,
        ready: (value) => modelChainKey(value.backend, value.model_id) === key ? value : null,
        unread: () => null,
        degraded: () => null,
      }) : null;
      if (chain) effectiveChains.push(chain);
      // Unread chains can retain known manual membership, never inferred defaults
      // or stale inherited hops. Only fresh chains classify current supply.
      for (const hop of chain?.chain ?? agent.routes?.[modelId]?.hops ?? []) sourceIds.add(hop.source_id);
    }
    for (const source of sources) {
      if (!sourceIds.has(source.id)) continue;
      relations.push({ sourceId: source.id, backend: agent.backend, kind: relationKind(source, effectiveChains) });
    }
  }
  return relations;
}
