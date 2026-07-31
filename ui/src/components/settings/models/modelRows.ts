import { eligibleSources } from './eligibility';
import { buildIdentifier } from './menus/identifiers';
import type { AgentBackend, AgentChain, AgentMapping, AgentSupply, RuntimeDependency, Source } from './types';

export type ModelChainRead =
  | { kind: 'ready'; chain: AgentChain }
  | { kind: 'error' };

export type ModelChainIndex = Record<string, ModelChainRead>;

export type ModelChainRequest = {
  backend: AgentBackend;
  modelId: string;
};

export const modelChainKey = (backend: AgentBackend, modelId: string): string =>
  `${backend}\u0000${modelId}`;

/** Lazy-start idleness is runnable-on-demand; only actual failures block Hub rows. */
export const runtimeHealthNeedsAttention = (health: RuntimeDependency['status']['health']): boolean =>
  health !== 'ok' && health !== 'not_started';

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

/** Enabled fixed-menu routes whose target still exists in an eligible source inventory. */
export function routableMappings(agent: AgentSupply, sources: Source[]): AgentMapping[] {
  const targets = new Set(
    orderedRouteSources(agent, sources).flatMap((source) => source.models.map((model) => model.id)),
  );
  return (agent.mappings ?? []).filter((mapping) => mapping.enabled && targets.has(mapping.target_model_id));
}

/** The model list the server says this backend exposes. */
export function listedModelIds(agent: AgentSupply): string[] {
  const primary = agent.menu_kind === 'fixed' ? agent.builtin_models ?? [] : agent.menu?.checked ?? [];
  const extras = [
    ...(agent.selected_model_id ? [agent.selected_model_id] : []),
    ...(agent.model_supply ?? []).map((model) => model.model_id),
    ...(agent.menu_kind === 'fixed' ? (agent.mappings ?? []).map((mapping) => mapping.builtin_id) : []),
  ];
  const seen = new Set<string>();
  return [...primary, ...extras].filter((modelId) => {
    if (!modelId || seen.has(modelId)) return false;
    seen.add(modelId);
    return true;
  });
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

/** Whether a row belongs in the non-healthy rollup, including automatic cooldowns. */
export function modelNeedsAttention(
  agent: AgentSupply,
  modelId: string,
  read: ModelChainRead | undefined,
  runtime?: RuntimeDependency | null,
): boolean {
  if (agent.mode !== 'hub') return false;
  if (read?.kind === 'error') return true;
  if (read?.kind === 'ready') {
    const head = read.chain.chain.find((link) => link.runnable);
    if (head?.channel === 'hub' && runtime && runtimeHealthNeedsAttention(runtime.status.health)) return true;
    return read.chain.supply_state !== 'ok';
  }
  return agent.model_supply?.find((model) => model.model_id === modelId)?.chain_length === 0;
}

export function agentNeedsModelSelection(agent: AgentSupply): boolean {
  return agent.mode === 'hub' && !agent.selected_model_id;
}

export function modelIssueCount(
  agents: AgentSupply[],
  chains: ModelChainIndex,
  runtime?: RuntimeDependency | null,
): number {
  return agents.reduce((count, agent) => {
    const modelIssues = listedModelIds(agent).filter((modelId) =>
      modelNeedsAttention(agent, modelId, chains[modelChainKey(agent.backend, modelId)], runtime),
    ).length;
    return count + modelIssues + (agentNeedsModelSelection(agent) ? 1 : 0);
  }, 0);
}
