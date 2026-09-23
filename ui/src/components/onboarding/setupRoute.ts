import type { VibeAgentBrief, VibeAgentFull } from '../../context/ApiContext';
import type { AssistantId } from './collaborationTimeline';
import { ASSISTANT_ORDER } from './collaborationTimeline';
import { readSetupTargets } from './setupTargets';
import { routeChainMatchesAttempt, sameManualOverride, sameRouteDraft } from '../settings/models/routeChainDraft';
import { catalogModels, chosenCandidate, draftRowFor, offeredCandidates } from '../settings/models/backendCatalog';
import { eligibilityOf } from '../settings/models/eligibility';
import type {
  AgentBackend,
  AgentChain,
  AgentSupply,
  AgentSources,
  BackendModelCandidates,
  BackendModelsPut,
  RouteHop,
} from '../settings/models/types';

export const hopIdentity = (hop: RouteHop): string => `${hop.source_id}\0${hop.model_id}`;

export const targetKey = (backend: AgentBackend, modelId: string): string =>
  `${backend}\0${modelId}`;

export type SetupRouteTargetSnapshot = {
  backend: AgentBackend;
  modelId: string;
  agentNames: string[];
  designatedNames?: string[];
  chain: AgentChain;
  membership: RouteHop[];
  sourceBaseline?: string;
};

export type SetupRouteHydration = {
  defaultAgentName: string | null;
  targets: SetupRouteTargetSnapshot[];
  union: RouteHop[];
  missingModels: { backend: AgentBackend; agentName: string }[];
};

/** The assistant card that opened the route editor. */
export type SetupRouteFocus = {
  backend: AgentBackend;
  agentName?: string;
};

/**
 * Pick the snapshot the focused card may edit.
 *
 * A hosted card always supplies identity and never inherits another backend's
 * target. Standalone tests that omit focus keep the first hydrated snapshot.
 */
export const selectSetupRouteTarget = (
  targets: readonly SetupRouteTargetSnapshot[],
  focus?: SetupRouteFocus | null,
): SetupRouteTargetSnapshot | null => {
  if (!targets.length) return null;
  if (!focus) return targets[0] ?? null;
  if (focus.agentName) {
    const named = targets.find((target) => (
      target.backend === focus.backend && target.agentNames.includes(focus.agentName!)
    ));
    if (named) return named;
  }
  return targets.find((target) => target.backend === focus.backend) ?? null;
};

export type SetupRouteReadApi = {
  listVibeAgents: (params?: { cache?: boolean }) => Promise<{
    ok: boolean;
    agents: VibeAgentBrief[];
    default_agent_name: string | null;
  }>;
  getVibeAgent: (
    name: string,
    params?: { cache?: boolean },
  ) => Promise<{ ok: boolean; agent?: VibeAgentFull | null }>;
  getAgentChain: (backend: AgentBackend, model: string) => Promise<AgentChain>;
};

export type SetupRouteWriteApi = {
  getVibeAgent: SetupRouteReadApi['getVibeAgent'];
  updateVibeAgent: (name: string, payload: { model: string }) => Promise<{ ok: boolean; agent?: VibeAgentFull | null }>;
  listAgents: () => Promise<AgentSupply[]>;
  getAgentChain: SetupRouteReadApi['getAgentChain'];
  previewAgentChain: (
    backend: AgentBackend,
    model: string,
    body: { manual_override: { hops: RouteHop[] } | null },
  ) => Promise<AgentChain>;
  putAgentChain: (
    backend: AgentBackend,
    model: string,
    body: { hops: RouteHop[] },
  ) => Promise<{ chain: AgentChain }>;
  getAgentModelCandidates: (backend: AgentBackend) => Promise<BackendModelCandidates>;
  putAgentModels: (backend: AgentBackend, body: BackendModelsPut) => Promise<AgentSupply>;
};

export type TargetSaveResult =
  | { key: string; kind: 'skipped' }
  | { key: string; kind: 'confirmed'; chain: AgentChain }
  | { key: string; kind: 'failed'; error: string }
  | { key: string; kind: 'reconcile'; chain: AgentChain };

/** Manual hops win when present; otherwise the automatic chain order. */
export const chainMembership = (chain: AgentChain): RouteHop[] => {
  if (chain.manual_override?.hops.length) {
    return chain.manual_override.hops.map((hop) => ({ source_id: hop.source_id, model_id: hop.model_id }));
  }
  return chain.chain.map((link) => ({ source_id: link.source_id, model_id: link.model_id }));
};

/**
 * One shared order, projected through each assistant's server-owned eligibility.
 *
 * Setup states it as one thing — 「the default model, and what to fall back to」 —
 * and Hub credentials are available to every assistant. Backend-native sources
 * remain backend-specific, so only those hops are excluded for other assistants.
 * Filtering by each assistant's previous membership instead would silently keep
 * different Hub routes even after the user saves a shared order.
 *
 * Claude Code keeps its restricted menu model and maps that model's chain to the
 * shared hops. Codex and OpenCode select the shared first hop as their menu model
 * after its chain has been saved, so the visible Agent model matches the first call.
 */
export const targetChanged = (shared: RouteHop[], target: SetupRouteTargetSnapshot): boolean =>
  !sameRouteDraft(shared, chainMembership(target.chain));

const eligibleRoute = (shared: RouteHop[], supply: AgentSupply): RouteHop[] =>
  supply.sources ? shared.filter((hop) => eligibilityOf(supply, hop.source_id).eligible) : shared;

const sourceBaseline = (sources: AgentSources | null | undefined): string => {
  if (!sources) return 'null';
  const ids = [...new Set([
    ...sources.order,
    ...(sources.eligibility ?? []).map((entry) => entry.source_id),
  ])].sort();
  return JSON.stringify({
    order: sources.order,
    eligible: ids.map((id) => [id, eligibilityOf({ sources }, id).eligible]),
  });
};

export const withMembershipHop = (membership: RouteHop[], hop: RouteHop): RouteHop[] => {
  if (membership.some((row) => hopIdentity(row) === hopIdentity(hop))) return membership;
  return [...membership, hop];
};

export const appendSharedHop = (order: RouteHop[], hop: RouteHop): RouteHop[] => {
  if (order.some((row) => hopIdentity(row) === hopIdentity(hop))) return order;
  return [...order, hop];
};

export const moveRouteHop = (order: RouteHop[], index: number, direction: -1 | 1): RouteHop[] => {
  const next = index + direction;
  if (index < 0 || next < 0 || next >= order.length) return order;
  const copy = [...order];
  const current = copy[index];
  const swap = copy[next];
  if (!current || !swap) return order;
  copy[index] = swap;
  copy[next] = current;
  return copy;
};

const backendRank = (backend: AgentBackend): number => {
  const index = ASSISTANT_ORDER.indexOf(backend as AssistantId);
  return index === -1 ? ASSISTANT_ORDER.length : index;
};

const byName = (left: string, right: string): number => (left < right ? -1 : left > right ? 1 : 0);

export const orderSetupTargets = (
  targets: readonly SetupRouteTargetSnapshot[],
  defaultAgentName?: string | null,
): SetupRouteTargetSnapshot[] => {
  const inDefault = defaultAgentName
    ? targets.filter((target) => target.agentNames.includes(defaultAgentName))
    : [];
  const rest = defaultAgentName
    ? targets.filter((target) => !target.agentNames.includes(defaultAgentName))
    : [...targets];
  const byStable = (left: SetupRouteTargetSnapshot, right: SetupRouteTargetSnapshot) =>
    backendRank(left.backend) - backendRank(right.backend)
    || byName(left.agentNames[0] ?? '', right.agentNames[0] ?? '')
    || byName(left.modelId, right.modelId);
  return [...inDefault].sort(byStable).concat([...rest].sort(byStable));
};

export const unionRouteOrder = (
  targets: readonly SetupRouteTargetSnapshot[],
  defaultAgentName?: string | null,
): RouteHop[] => {
  const seen = new Set<string>();
  const union: RouteHop[] = [];
  for (const target of orderSetupTargets(targets, defaultAgentName)) {
    for (const hop of target.membership) {
      const key = hopIdentity(hop);
      if (seen.has(key)) continue;
      seen.add(key);
      union.push(hop);
    }
  }
  return union;
};

/** The Agent's exact saved model. Never selected_model_id or the first catalog row. */
export const menuModelFor = (agent: Pick<VibeAgentFull, 'model'>, _supply?: AgentSupply): string | null => {
  const saved = agent.model?.trim();
  return saved ? saved : null;
};

const discloseNames = (
  backend: AgentBackend,
  modelId: string,
  designated: string[],
  supplies: readonly AgentSupply[],
): string[] => {
  const names = new Set(designated);
  for (const supply of supplies) {
    if (supply.backend !== backend) continue;
    for (const named of supply.named_agents ?? []) {
      if (named.effective_model_id === modelId) names.add(named.name);
    }
  }
  return [...names].sort(byName);
};

export async function hydrateSetupRoutes(
  reads: SetupRouteReadApi,
  supplies: readonly AgentSupply[],
  enabledBackends?: ReadonlySet<AgentBackend>,
): Promise<SetupRouteHydration> {
  const listing = await reads.listVibeAgents({ cache: false });
  if (!listing.ok) throw new Error('onboarding.route.readFailed');
  const designated = await readSetupTargets(listing.agents.filter((agent) =>
    !enabledBackends || enabledBackends.has(agent.backend as AgentBackend)), reads, { requireReadable: true });
  const supplyByBackend = new Map(supplies.map((row) => [row.backend, row]));
  const grouped = new Map<string, { backend: AgentBackend; modelId: string; names: string[] }>();
  const missingModels: SetupRouteHydration['missingModels'] = [];

  for (const agent of designated) {
    const backend = agent.backend as AgentBackend;
    const modelId = menuModelFor(agent, supplyByBackend.get(backend));
    if (!modelId) {
      missingModels.push({ backend, agentName: agent.name });
      continue;
    }
    const key = targetKey(backend, modelId);
    const existing = grouped.get(key);
    if (existing) existing.names.push(agent.name);
    else grouped.set(key, { backend, modelId, names: [agent.name] });
  }

  const targets: SetupRouteTargetSnapshot[] = [];
  for (const group of grouped.values()) {
    try {
      const chain = await reads.getAgentChain(group.backend, group.modelId);
      targets.push({
        backend: group.backend,
        modelId: group.modelId,
        agentNames: discloseNames(group.backend, group.modelId, group.names, supplies),
        designatedNames: group.names,
        chain,
        membership: chainMembership(chain),
        sourceBaseline: sourceBaseline(supplyByBackend.get(group.backend)?.sources),
      });
    } catch {
      throw new Error('onboarding.route.readFailed');
    }
  }

  const ordered = orderSetupTargets(targets, listing.default_agent_name);
  return {
    defaultAgentName: listing.default_agent_name,
    targets: ordered,
    union: unionRouteOrder(ordered, listing.default_agent_name),
    missingModels,
  };
}

export const classifyRetry = (
  target: SetupRouteTargetSnapshot,
  current: AgentChain,
  desired: RouteHop[],
): 'skip' | 'retry' | 'reconcile' => {
  if (routeChainMatchesAttempt(current, {
    backend: target.backend,
    modelId: target.modelId,
    submitted: desired,
    manual_override: { hops: desired },
  })) return 'skip';
  const original = chainMembership(target.chain);
  const sameMembership = sameRouteDraft(chainMembership(current), original);
  const sameOverride = sameManualOverride(current.manual_override, target.chain.manual_override);
  return sameMembership && sameOverride ? 'retry' : 'reconcile';
};

const errorMessage = (error: unknown): string => (error instanceof Error ? error.message : String(error));

const precheckTarget = async (
  target: SetupRouteTargetSnapshot,
  api: SetupRouteWriteApi,
): Promise<{ kind: 'ok'; supply: AgentSupply } | 'reconcile' | { kind: 'failed'; error: string }> => {
  try {
    const supplies = await api.listAgents();
    const supply = supplies.find((row) => row.backend === target.backend);
    if (supply?.mode !== 'hub') return 'reconcile';
    if (target.sourceBaseline !== undefined && target.sourceBaseline !== sourceBaseline(supply.sources)) return 'reconcile';
    for (const name of target.agentNames) {
      const result = await api.getVibeAgent(name, { cache: false });
      if (result.ok && result.agent && result.agent.backend === target.backend && result.agent.model === target.modelId) {
        return { kind: 'ok', supply };
      }
    }
    return 'reconcile';
  } catch (error) {
    return { kind: 'failed', error: errorMessage(error) };
  }
};

/**
 * Make the backend know the model this target is for, before anything asks it to
 * route one.
 *
 * A route is previewed and saved against a model the backend's catalog names. An
 * open-menu backend's catalog is the user's own, so a machine that just installed
 * OpenCode has an empty one — while the Agent it was installed with already carries
 * the model this target was hydrated from. Without this, the first preview of that
 * model is refused for a model nobody could have added yet, and setup's route step
 * can never succeed on a new install.
 *
 * Only the open-menu path, and only a model the catalog does not already hold: a
 * fixed-menu backend ships its own catalog, and a server that predates backend
 * catalogs states none at all, so both are left exactly as they were.
 *
 * What is added is a candidate the server offered, so it is added the way the
 * picker adds one — the whole agreement, through the functions that own it,
 * rather than the fields this path happens to need. `draftRowFor` owns which row
 * an id gets, and `chosenCandidate` pairs a pick with the projection it was
 * picked against so the two cannot drift apart. Composing either half here is
 * what produced the defects this comment is now the record of: a protocol
 * defaulted to Responses under an Anthropic-family model, and an addition that
 * promised nothing about its suppliers.
 *
 * So the protocol is the one the server derived — the generic default is
 * Responses, and an Anthropic-family model adopted without asking would be
 * written down as a Responses model and speak the wrong protocol on every later
 * turn, a silently wrong config in place of a loud refusal. And the suppliers
 * are the projection that candidate was offered with, which is what lets the
 * server refuse this write when inventory has moved since the read; without it
 * the addition is filed as a hand-written row, which promises nothing, and a
 * catalog entry nobody agreed to commits while the route preview still fails.
 *
 * When no offered candidate names this id, or the one that does states no
 * protocol, nothing is written and the preview refuses exactly as it did before:
 * the person is sent to the catalog to say what this model is, rather than being
 * given an answer nobody gave.
 */
const adoptTargetModel = async (
  target: Pick<SetupRouteTargetSnapshot, 'backend' | 'modelId'>,
  api: SetupRouteWriteApi,
  supply: AgentSupply,
): Promise<AgentSupply> => {
  if (target.backend === 'claude') return supply;
  const catalog = catalogModels(supply);
  if (!catalog || catalog.some((model) => model.id === target.modelId)) return supply;
  const offered = offeredCandidates(await api.getAgentModelCandidates(target.backend));
  const candidate = offered.get(target.modelId);
  if (!candidate || (target.backend === 'opencode' && !candidate.native_protocol)) {
    throw new Error('onboarding.route.catalogFailed');
  }
  const chosen = chosenCandidate(candidate);
  const adopted = draftRowFor(chosen.candidate, [], catalog);
  return api.putAgentModels(target.backend, {
    baseline: catalog,
    models: [...catalog, adopted],
    expected_suppliers: { [adopted.id]: chosen.expected_suppliers },
  });
};

/** Repair a model-less Agent only after its chosen menu model has a confirmed route. */
export async function repairMissingSetupModel(
  backend: AgentBackend,
  agentName: string,
  selectedModel: string,
  shared: RouteHop[],
  api: SetupRouteWriteApi,
): Promise<string> {
  if (shared.length === 0) throw new Error('onboarding.route.noEligible');
  const modelId = backend === 'claude' ? selectedModel : shared[0]!.model_id;
  if (!modelId) throw new Error('onboarding.route.catalogFailed');
  const before = await api.getVibeAgent(agentName, { cache: false });
  if (!before.ok || !before.agent || before.agent.backend !== backend
    || (before.agent.model && before.agent.model !== modelId)) {
    throw new Error('onboarding.route.changed');
  }
  const supply = (await api.listAgents()).find((row) => row.backend === backend);
  if (supply?.mode !== 'hub') throw new Error('onboarding.route.changed');
  const desired = eligibleRoute(shared, supply);
  if (desired.length === 0) throw new Error('onboarding.route.noEligible');
  let adopted: AgentSupply;
  try {
    adopted = await adoptTargetModel({ backend, modelId }, api, supply);
  } catch {
    throw new Error('onboarding.route.catalogFailed');
  }
  let chain: AgentChain;
  try {
    chain = await api.getAgentChain(backend, modelId);
  } catch {
    throw new Error('onboarding.route.chainWriteFailed');
  }
  const target: SetupRouteTargetSnapshot = {
    backend, modelId, agentNames: [agentName], designatedNames: [agentName],
    chain, membership: chainMembership(chain),
  };
  const saved = await writeTarget(target, desired, api, adopted);
  if (saved.kind !== 'confirmed') {
    try {
      const readback = await api.getAgentChain(backend, modelId);
      if (!routeChainMatchesAttempt(readback, {
        backend, modelId, submitted: desired, manual_override: { hops: desired },
      })) throw new Error('unconfirmed');
    } catch {
      throw new Error(saved.kind === 'reconcile' ? 'onboarding.route.changed' : 'onboarding.route.chainWriteFailed');
    }
  }
  const current = await api.getVibeAgent(agentName, { cache: false });
  if (!current.ok || !current.agent || current.agent.backend !== backend
    || (current.agent.model && current.agent.model !== modelId)) {
    throw new Error('onboarding.route.changed');
  }
  if (current.agent.model !== modelId) {
    try {
      await api.updateVibeAgent(agentName, { model: modelId });
    } catch {
      // A lost response may follow a committed write; the read below decides.
    }
  }
  try {
    const readback = await api.getVibeAgent(agentName, { cache: false });
    if (readback.ok && readback.agent?.backend === backend && readback.agent.model === modelId) {
      return modelId;
    }
  } catch {
    // Keep the stage visible and retryable when the read is unavailable.
  }
  throw new Error('onboarding.route.modelSwitchFailed');
}

const writePreferredTarget = async (
  target: SetupRouteTargetSnapshot,
  desired: RouteHop[],
  api: SetupRouteWriteApi,
  supply: AgentSupply,
): Promise<TargetSaveResult> => {
  const key = targetKey(target.backend, target.modelId);
  const preferred = desired[0]?.model_id;
  if (!preferred || target.backend === 'claude' || preferred === target.modelId) {
    return writeTarget(target, desired, api, supply);
  }
  let stage: 'catalogFailed' | 'chainWriteFailed' | 'modelSwitchFailed' = 'catalogFailed';
  try {
    const oldChain = await api.getAgentChain(target.backend, target.modelId);
    if (classifyRetry(target, oldChain, chainMembership(target.chain)) === 'reconcile') {
      return { key, kind: 'reconcile', chain: oldChain };
    }
    const adoptedSupply = await adoptTargetModel({ ...target, modelId: preferred }, api, supply);
    stage = 'chainWriteFailed';
    const nextChain = await api.getAgentChain(target.backend, preferred);
    const nextTarget = { ...target, modelId: preferred, chain: nextChain, membership: chainMembership(nextChain) };
    const saved = await writeTarget(nextTarget, desired, api, adoptedSupply);
    if (saved.kind === 'failed') return { key, kind: 'failed', error: 'onboarding.route.chainWriteFailed' };
    if (saved.kind !== 'confirmed') return { ...saved, key };
    // The route must exist before the Agent can name it. A failed model switch
    // leaves the old Agent route intact and the new chain available for retry.
    stage = 'modelSwitchFailed';
    for (const name of target.designatedNames ?? target.agentNames) {
      const current = await api.getVibeAgent(name, { cache: false });
      if (!current.ok || !current.agent || current.agent.backend !== target.backend
        || (current.agent.model !== target.modelId && current.agent.model !== preferred)) {
        return { key, kind: 'reconcile', chain: saved.chain };
      }
      if (current.agent.model === preferred) continue;
      const updated = await api.updateVibeAgent(name, { model: preferred });
      if (!updated.ok || updated.agent?.name !== name || updated.agent.backend !== target.backend
        || updated.agent.model !== preferred) throw new Error('onboarding.route.modelSwitchFailed');
      const readback = await api.getVibeAgent(name, { cache: false });
      if (!readback.ok || readback.agent?.model !== preferred) throw new Error('onboarding.route.modelSwitchFailed');
    }
    return { ...saved, key };
  } catch (error) {
    if (stage === 'modelSwitchFailed') {
      try {
        const chain = await api.getAgentChain(target.backend, preferred);
        const agents = await Promise.all((target.designatedNames ?? target.agentNames).map((name) =>
          api.getVibeAgent(name, { cache: false })));
        if (routeChainMatchesAttempt(chain, {
          backend: target.backend, modelId: preferred, submitted: desired,
          manual_override: { hops: desired },
        }) && agents.every((row) => row.ok && row.agent?.backend === target.backend
          && row.agent.model === preferred)) {
          return { key, kind: 'confirmed', chain };
        }
      } catch {
        // The write outcome remains unknown; a later read can reconcile it.
      }
    }
    return { key, kind: 'failed', error: error instanceof Error && error.message.startsWith('onboarding.route.')
      ? error.message : `onboarding.route.${stage}` };
  }
};

/**
 * The write, and nothing that decides whether it may happen.
 *
 * `supply` is required rather than re-read here because the contract wants the
 * Agent, the mode and the chain checked against the baseline before EVERY write,
 * and a parameter this function cannot invent is what makes 「its caller checked」
 * true by construction instead of by each caller remembering to.
 */
const writeTarget = async (
  target: SetupRouteTargetSnapshot,
  desired: RouteHop[],
  api: SetupRouteWriteApi,
  supply: AgentSupply,
): Promise<TargetSaveResult> => {
  const key = targetKey(target.backend, target.modelId);
  try {
    // Everything that decides whether this save may write at all comes first,
    // because a decision made after a write is not a decision. The contract
    // re-reads the chain against the baseline before each write, and a target
    // another Settings surface has moved since hydration is sent to
    // reconciliation — which has to mean nothing was persisted, not that what
    // was persisted gets taken back. The catalog addition below is a write like
    // any other, so it sits on this side of the check with the chain write.
    const pre = await api.getAgentChain(target.backend, target.modelId);
    if (routeChainMatchesAttempt(pre, {
      backend: target.backend,
      modelId: target.modelId,
      submitted: desired,
      manual_override: { hops: desired },
    })) {
      return { key, kind: 'confirmed', chain: pre };
    }
    const check = classifyRetry(target, pre, desired);
    if (check === 'reconcile') return { key, kind: 'reconcile', chain: pre };
    // Still ahead of the preview, which is what needs it: the backend validates
    // an override against its catalog, so the model has to be in there before
    // anything asks to route it.
    await adoptTargetModel(target, api, supply);
    await api.previewAgentChain(target.backend, target.modelId, { manual_override: { hops: desired } });
    await api.putAgentChain(target.backend, target.modelId, { hops: desired });
    const readback = await api.getAgentChain(target.backend, target.modelId);
    if (routeChainMatchesAttempt(readback, {
      backend: target.backend,
      modelId: target.modelId,
      submitted: desired,
      manual_override: { hops: desired },
    })) {
      return { key, kind: 'confirmed', chain: readback };
    }
    return { key, kind: 'reconcile', chain: readback };
  } catch (error) {
    return { key, kind: 'failed', error: errorMessage(error) };
  }
};

export async function saveSetupRoutes(
  shared: RouteHop[],
  targets: readonly SetupRouteTargetSnapshot[],
  api: SetupRouteWriteApi,
  options: { dirty: boolean } = { dirty: true },
): Promise<TargetSaveResult[]> {
  if (!options.dirty) {
    return targets.map((target) => ({ key: targetKey(target.backend, target.modelId), kind: 'skipped' as const }));
  }
  const results: TargetSaveResult[] = [];
  for (const target of targets) {
    const key = targetKey(target.backend, target.modelId);
    const precheck = await precheckTarget(target, api);
    if (precheck === 'reconcile') {
      const chain = await api.getAgentChain(target.backend, target.modelId).catch(() => target.chain);
      results.push({ key, kind: 'reconcile', chain });
      continue;
    }
    if (precheck.kind === 'failed') {
      results.push({ key, kind: 'failed', error: precheck.error });
      continue;
    }
    const desired = eligibleRoute(shared, precheck.supply);
    if (shared.length > 0 && desired.length === 0) {
      results.push({ key, kind: 'failed', error: 'onboarding.route.noEligible' });
      continue;
    }
    const baselineHops = chainMembership(target.chain);
    if (desired.length === 0 || (sameRouteDraft(desired, baselineHops)
      && (target.backend === 'claude' || target.modelId === desired[0]?.model_id))) {
      results.push({ key, kind: 'skipped' });
      continue;
    }
    results.push(await writePreferredTarget(target, desired, api, precheck.supply));
  }
  return results;
}

export async function retrySetupRoutes(
  shared: RouteHop[],
  targets: readonly SetupRouteTargetSnapshot[],
  previous: readonly TargetSaveResult[],
  api: SetupRouteWriteApi,
): Promise<TargetSaveResult[]> {
  const results: TargetSaveResult[] = [];
  for (const target of targets) {
    const key = targetKey(target.backend, target.modelId);
    const prior = previous.find((row) => row.key === key);
    if (prior?.kind === 'confirmed' && prior.chain.model_id !== target.modelId) {
      try {
        const supplies = await api.listAgents();
        const supply = supplies.find((row) => row.backend === target.backend);
        if (supply?.mode !== 'hub' || (target.sourceBaseline !== undefined
          && target.sourceBaseline !== sourceBaseline(supply.sources))) {
          results.push({ key, kind: 'reconcile', chain: prior.chain });
          continue;
        }
        const desired = eligibleRoute(shared, supply);
        const current = await api.getAgentChain(target.backend, prior.chain.model_id);
        const agents = await Promise.all((target.designatedNames ?? target.agentNames).map((name) =>
          api.getVibeAgent(name, { cache: false })));
        if (sameRouteDraft(chainMembership(prior.chain), desired)
          && routeChainMatchesAttempt(current, {
            backend: target.backend, modelId: prior.chain.model_id,
            submitted: desired, manual_override: { hops: desired },
          }) && agents.every((row) => row.ok && row.agent?.model === prior.chain.model_id)) {
          results.push(prior);
        } else {
          results.push({ key, kind: 'reconcile', chain: current });
        }
      } catch (error) {
        results.push({ key, kind: 'failed', error: errorMessage(error) });
      }
      continue;
    }
    if (prior?.kind === 'confirmed' && sameRouteDraft(chainMembership(prior.chain), shared)) {
      results.push(prior);
      continue;
    }
    if (!prior || prior.kind === 'skipped') {
      // A skipped receipt belongs to the old draft. A later edit may now require
      // this target, so compare it against the current order again.
      const [rechecked] = await saveSetupRoutes(shared, [target], api);
      results.push(rechecked ?? { key, kind: 'skipped' });
      continue;
    }
    if (shared.length === 0) {
      results.push({ key, kind: 'skipped' });
      continue;
    }
    let current: AgentChain;
    try {
      current = await api.getAgentChain(target.backend, target.modelId);
    } catch (error) {
      results.push({ key, kind: 'failed', error: errorMessage(error) });
      continue;
    }
    // The chain above is only one of the three things the contract re-reads
    // before a write. A retry needs the other two more than the first attempt
    // did, not less: it is separated from the hydration it was built on by
    // however long the person spent looking at the failure, so the backend may
    // have left hub and the Agent may now name a different model — and writing
    // a route for a target that no longer exists is what asking afterwards
    // cannot undo. Same question, same owner, so the two paths cannot answer it
    // differently.
    const precheck = await precheckTarget(target, api);
    if (precheck === 'reconcile') {
      results.push({ key, kind: 'reconcile', chain: current });
      continue;
    }
    if (precheck.kind === 'failed') {
      results.push({ key, kind: 'failed', error: precheck.error });
      continue;
    }
    const desired = eligibleRoute(shared, precheck.supply);
    if (desired.length === 0) {
      results.push({ key, kind: 'failed', error: 'onboarding.route.noEligible' });
      continue;
    }
    if (target.backend !== 'claude' && target.modelId !== desired[0]?.model_id) {
      results.push(await writePreferredTarget(target, desired, api, precheck.supply));
      continue;
    }
    const classification = classifyRetry(target, current, desired);
    if (classification === 'skip') {
      results.push({ key, kind: 'confirmed', chain: current });
      continue;
    }
    if (classification === 'reconcile') {
      results.push({ key, kind: 'reconcile', chain: current });
      continue;
    }
    results.push(await writePreferredTarget(target, desired, api, precheck.supply));
  }
  return results;
}

export const saveNeedsRetry = (results: readonly TargetSaveResult[]): boolean =>
  results.some((row) => row.kind === 'failed' || row.kind === 'reconcile');

/** Hub sources reach every assistant; backend-native sources stay on eligible routes. */
export const hopsFor = (
  targets: readonly SetupRouteTargetSnapshot[],
  hop: RouteHop,
  supplies: readonly AgentSupply[],
): string[] => [...new Set(targets.flatMap((target) => {
  const supply = supplies.find((row) => row.backend === target.backend);
  return !supply || !supply.sources || eligibilityOf(supply, hop.source_id).eligible
    ? target.agentNames : [];
}))];
