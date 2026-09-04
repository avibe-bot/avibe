import { catalogModelIds } from './backendCatalog';
import { eligibleSources } from './eligibility';
import type { RegionRead } from './regionRead';
import type { AgentBackend, AgentChain, AgentSupply, Source } from './types';

export type ModelChainRead = RegionRead<AgentChain>;

export type ModelChainIndex = Record<string, ModelChainRead>;

export type ModelChainRequest = {
  backend: AgentBackend;
  modelId: string;
};

export const modelChainKey = (backend: AgentBackend, modelId: string): string =>
  `${backend}\u0000${modelId}`;

/** Manual inventory is a credential-backed capability; subscriptions are read-only. */
export function manualModelSources(sources: Source[]): Source[] {
  return sources.filter((source) => source.kind === 'api_key');
}

/** Count suppliers from the complete route inventory, never a searched subset. */
export function modelSupplierCounts(sources: Source[]): ReadonlyMap<string, number> {
  const counts = new Map<string, number>();
  for (const source of sources) {
    for (const modelId of new Set(source.models.map((model) => model.id))) {
      counts.set(modelId, (counts.get(modelId) ?? 0) + 1);
    }
  }
  return counts;
}

/** Every server-eligible source is a route-target inventory; the order only ranks groups. */
export function orderedRouteSources(agent: AgentSupply, sources: Source[]): Source[] {
  const eligible = eligibleSources(sources, agent);
  const byId = new Map(eligible.map((source) => [source.id, source]));
  const ordered = (agent.sources?.order ?? [])
    .map((sourceId) => byId.get(sourceId))
    .filter((source): source is Source => Boolean(source));
  const seen = new Set(ordered.map((source) => source.id));
  return [...ordered, ...eligible.filter((source) => !seen.has(source.id))];
}

/** Whether the model already exists on an eligible supplier omitted from this Agent's order. */
export function modelHasOffOrderSupplier(agent: AgentSupply, sources: Source[], modelId: string): boolean {
  const enabled = new Set(agent.sources?.order ?? []);
  return eligibleSources(sources, agent).some(
    (source) => !enabled.has(source.id) && source.models.some((model) => (
      model.id === modelId
    )),
  );
}

export const COLLAPSED_MODEL_LIMIT = 6;

export type CollapsedModelRows = {
  visible: string[];
  hidden: string[];
};

export type ModelSupplyState = 'available' | 'paused' | 'unconfigured';

/** Structural emptiness wins before live runnability, exactly as the contract specifies. */
export function modelSupplyState(agent: AgentSupply, modelId: string): ModelSupplyState {
  const supply = agent.model_supply?.find((row) => row.model_id === modelId);
  if (!supply) return 'available';
  if (supply.chain_length === 0) return 'unconfigured';
  return supply.has_runnable_hop ? 'available' : 'paused';
}

/** Keep the catalog order stable while bounding each collapsed group. */
export function collapsedModelRows(agent: AgentSupply, expanded = false): CollapsedModelRows {
  const models = catalogModelIds(agent);
  if (expanded) return { visible: models, hidden: [] };
  return {
    visible: models.slice(0, COLLAPSED_MODEL_LIMIT),
    hidden: models.slice(COLLAPSED_MODEL_LIMIT),
  };
}

/** One chain read per backend/model pair, even if a duplicated Agent row reaches the page. */
export function modelChainRequests(agents: AgentSupply[]): ModelChainRequest[] {
  const requests = new Map<string, ModelChainRequest>();
  for (const agent of agents) {
    if (agent.mode !== 'hub') continue;
    for (const modelId of catalogModelIds(agent)) {
      const key = modelChainKey(agent.backend, modelId);
      if (!requests.has(key)) requests.set(key, { backend: agent.backend, modelId });
    }
  }
  return [...requests.values()];
}
