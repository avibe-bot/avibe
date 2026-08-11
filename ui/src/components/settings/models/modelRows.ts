import { eligibleSources } from './eligibility';
import { buildIdentifier } from './menus/identifiers';
import type { AgentBackend, AgentChain, AgentSupply, Source } from './types';

export type ModelChainRead =
  | { kind: 'loading' }
  | { kind: 'ready'; chain: AgentChain }
  | { kind: 'error' };

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
  const standardVendors = new Set(agent.standard_vendors ?? []);
  return eligibleSources(sources, agent).some(
    (source) => !enabled.has(source.id) && source.models.some((model) => (
      agent.menu_kind === 'open'
        ? buildIdentifier(source.vendor, model.id, standardVendors) === modelId
        : model.id === modelId
    )),
  );
}

/** The model list the server says this backend exposes. */
export function listedModelIds(agent: AgentSupply): string[] {
  const primary = agent.menu_kind === 'fixed' ? agent.builtin_models ?? [] : agent.menu?.checked ?? [];
  const extras = [
    ...(agent.selected_model_id ? [agent.selected_model_id] : []),
    ...(agent.model_supply ?? []).map((model) => model.model_id),
    ...Object.keys(agent.routes ?? {}),
  ];
  const seen = new Set<string>();
  return [...primary, ...extras].filter((modelId) => {
    if (!modelId || seen.has(modelId)) return false;
    seen.add(modelId);
    return true;
  });
}

export const NOMINAL_MODEL_BASELINE = 3;

export type CollapsedModelRows = {
  visible: string[];
  hidden: string[];
};

/**
 * Keep every structurally unsupplied model plus a small nominal baseline.
 * This reads only AgentSupply, so late or failed per-model chain reads cannot
 * reorganize the group under the user's pointer.
 */
export function collapsedModelRows(agent: AgentSupply, expanded = false): CollapsedModelRows {
  const models = listedModelIds(agent);
  if (expanded) return { visible: models, hidden: [] };
  const emptyRoutes = new Set(
    (agent.model_supply ?? [])
      .filter((row) => row.chain_length === 0)
      .map((row) => row.model_id),
  );
  const baseline = new Set(
    models.filter((modelId) => !emptyRoutes.has(modelId)).slice(0, NOMINAL_MODEL_BASELINE),
  );
  const visible = models.filter((modelId) => emptyRoutes.has(modelId) || baseline.has(modelId));
  const visibleSet = new Set(visible);
  return { visible, hidden: models.filter((modelId) => !visibleSet.has(modelId)) };
}

/** One chain read per backend/model pair, even if a duplicated Agent row reaches the page. */
export function modelChainRequests(agents: AgentSupply[]): ModelChainRequest[] {
  const requests = new Map<string, ModelChainRequest>();
  for (const agent of agents) {
    if (agent.mode !== 'hub') continue;
    for (const modelId of listedModelIds(agent)) {
      const key = modelChainKey(agent.backend, modelId);
      if (!requests.has(key)) requests.set(key, { backend: agent.backend, modelId });
    }
  }
  return [...requests.values()];
}
