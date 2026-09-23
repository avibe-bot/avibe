import { describe, expect, it, vi } from 'vitest';
import type { VibeAgentBrief, VibeAgentFull } from '../../context/ApiContext';
import type {
  AgentChain,
  AgentSupply,
  BackendModel,
  BackendModelsPut,
  ModelCandidate,
  ModelCandidateSupplier,
  NativeProtocol,
  RouteHop,
} from '../settings/models/types';
import {
  classifyRetry,
  hydrateSetupRoutes,
  retrySetupRoutes,
  saveSetupRoutes,
  selectSetupRouteTarget,
  targetChanged,
  targetKey,
  unionRouteOrder,
  type SetupRouteTargetSnapshot,
} from './setupRoute';

const hop = (source_id: string, model_id: string): RouteHop => ({ source_id, model_id });
const A = hop('src_a', 'opus-5');
const B = hop('src_a', 'sonnet-4');
const C = hop('src_b', 'gpt-5');
const D = hop('src_c', 'codex-mini');

const chainOf = (
  backend: AgentChain['backend'],
  modelId: string,
  hops: RouteHop[],
  origin: AgentChain['route_origin'] = 'manual',
): AgentChain => ({
  contract_version: 10,
  backend,
  model_id: modelId,
  manual_override: origin === 'manual' ? { hops } : null,
  route_origin: origin,
  current: hops[0] ?? null,
  chain: hops.map((row) => ({
    source_id: row.source_id,
    model_id: row.model_id,
    channel: 'hub',
    health: 'healthy',
    runnable: true,
    reason: null,
    retry_at: null,
  })),
  supply_state: 'ok',
});

const target = (
  backend: AgentChain['backend'],
  modelId: string,
  hops: RouteHop[],
  names: string[],
  origin: AgentChain['route_origin'] = 'manual',
): SetupRouteTargetSnapshot => {
  const chain = chainOf(backend, modelId, hops, origin);
  return { backend, modelId, agentNames: names, chain, membership: hops };
};

const brief = (name: string, backend: VibeAgentBrief['backend'], model: string): VibeAgentBrief => ({
  id: `${backend}-${name}`,
  name,
  display_name: name,
  description: null,
  backend,
  model,
  reasoning_effort: null,
  enabled: true,
  archived: false,
  archived_at: null,
  source: 'file',
  updated_at: '',
});

const full = (row: VibeAgentBrief): VibeAgentFull => ({
  ...row,
  system_prompt: null,
  created_at: '',
  metadata: { builtin_default: true },
});

const supply = (backend: AgentSupply['backend'], names: Array<{ name: string; model: string }>): AgentSupply => ({
  backend,
  cli_present: true,
  mode: 'hub',
  menu_kind: 'fixed',
  named_agents: names.map((row) => ({
    name: row.name,
    effective_model_id: row.model,
    supply_status: 'ok',
  })),
});

describe('setup route projection', () => {
  it('keeps two models on the same source as distinct rows', () => {
    const claude = target('claude', 'opus-5', [A, B], ['claude']);
    expect(unionRouteOrder([claude])).toEqual([A, B]);
  });

  it('does not treat a displayed union as a write when existing orders diverge', () => {
    const claude = target('claude', 'opus-5', [A, B], ['claude']);
    const codex = target('codex', 'gpt-5', [B, A], ['codex']);
    const union = unionRouteOrder([claude, codex]);
    expect(union).toEqual([A, B]);
    expect(targetChanged(union, claude)).toBe(false);
    expect(targetChanged(union, codex)).toBe(true);
  });

  it('gives every assistant the whole shared order, not the part it already had', () => {
    const claude = target('claude', 'opus-5', [A, B], ['claude']);
    const codex = target('codex', 'gpt-5', [C, D], ['codex']);
    const union = unionRouteOrder([claude, codex]);
    expect(union).toEqual([A, B, C, D]);
    // Neither assistant is already on the shared route, so both have something to save.
    expect(targetChanged(union, claude)).toBe(true);
    expect(targetChanged(union, codex)).toBe(true);
  });

  it('selects the focused Agent/backend and does not inherit another card\'s target', () => {
    const claude = target('claude', 'opus-5', [A, B], ['claude']);
    const codex = target('codex', 'gpt-5', [C], ['codex']);
    expect(selectSetupRouteTarget([claude, codex])).toBe(claude);
    expect(selectSetupRouteTarget([claude, codex], { backend: 'codex', agentName: 'codex' })).toBe(codex);
    expect(selectSetupRouteTarget([claude, codex], { backend: 'codex' })).toBe(codex);
    expect(selectSetupRouteTarget([claude, codex], { backend: 'opencode', agentName: 'opencode' })).toBeNull();
  });
});

describe('hydrateSetupRoutes', () => {
  it('uses each designated Agent\'s exact saved model, not selected_model_id', async () => {
    const claude = brief('claude', 'claude', 'opus-5');
    const custom = brief('mine', 'claude', 'other-model');
    const reads = {
      listVibeAgents: vi.fn(async () => ({ ok: true, agents: [claude, custom], default_agent_name: 'mine' })),
      getVibeAgent: vi.fn(async (name: string) => ({
        ok: true,
        agent: full(name === 'claude' ? claude : custom),
      })),
      getAgentChain: vi.fn(async (backend: AgentChain['backend'], model: string) => chainOf(backend, model, [A, B])),
    };
    const hydration = await hydrateSetupRoutes(reads, [
      { ...supply('claude', [{ name: 'claude', model: 'opus-5' }]), selected_model_id: 'ignored-default' },
    ]);
    expect(reads.getAgentChain).toHaveBeenCalledWith('claude', 'opus-5');
    expect(reads.getAgentChain).not.toHaveBeenCalledWith('claude', 'ignored-default');
    expect(hydration.targets).toHaveLength(1);
    expect(hydration.targets[0]?.modelId).toBe('opus-5');
    expect(hydration.union).toEqual([A, B]);
  });
});

describe('saveSetupRoutes', () => {
  const writes = (
    chains: Record<string, AgentChain>,
    options: { failOn?: string } = {},
  ) => {
    const store = { ...chains };
    const keyOf = (backend: string, model: string) => `${backend}:${model}`;
    return {
      store,
      api: {
        getVibeAgent: vi.fn(async (name: string) => ({
          ok: true,
          agent: full(brief(name, name === 'codex' ? 'codex' : 'claude', store[keyOf(name === 'codex' ? 'codex' : 'claude', name === 'codex' ? 'gpt-5' : 'opus-5')]?.model_id ?? 'opus-5')),
        })),
        listAgents: vi.fn(async () => [supply('claude', [{ name: 'claude', model: 'opus-5' }]), supply('codex', [{ name: 'codex', model: 'gpt-5' }])]),
        getAgentChain: vi.fn(async (backend: AgentChain['backend'], model: string) => {
          const current = store[keyOf(backend, model)];
          if (!current) throw new Error(`missing ${backend} ${model}`);
          return current;
        }),
        previewAgentChain: vi.fn(async (backend: AgentChain['backend'], model: string, body: { manual_override: { hops: RouteHop[] } | null }) =>
          chainOf(backend, model, body.manual_override?.hops ?? [], 'manual')),
        putAgentChain: vi.fn(async (backend: AgentChain['backend'], model: string, body: { hops: RouteHop[] }) => {
          const key = keyOf(backend, model);
          if (options.failOn === key) throw new Error(`failed ${key}`);
          const next = chainOf(backend, model, body.hops, 'manual');
          store[key] = next;
          return { chain: next };
        }),
        getAgentModelCandidates: vi.fn(async () => ({ builtin: [], providers: [], in_list: [] })),
        putAgentModels: vi.fn(async () => supply('claude', [{ name: 'claude', model: 'opus-5' }])),
      },
    };
  };

  it('opening without an edit writes nothing, even when displayed union differs', async () => {
    const claude = target('claude', 'opus-5', [A, B], ['claude']);
    const codex = target('codex', 'gpt-5', [B, A], ['codex']);
    const { api } = writes({
      'claude:opus-5': claude.chain,
      'codex:gpt-5': codex.chain,
    });
    const results = await saveSetupRoutes([A, B], [claude, codex], api, { dirty: false });
    expect(api.putAgentChain).not.toHaveBeenCalled();
    expect(results.every((row) => row.kind === 'skipped')).toBe(true);
  });

  it('persists an explicit first hop on an empty chain', async () => {
    const claude = target('claude', 'opus-5', [], ['claude'], 'automatic');
    const { api, store } = writes({ 'claude:opus-5': claude.chain });
    api.getVibeAgent = vi.fn(async () => ({ ok: true, agent: full(brief('claude', 'claude', 'opus-5')) }));
    const live = { ...claude, membership: [A] };
    const results = await saveSetupRoutes([A], [live], api, { dirty: true });
    expect(api.putAgentChain).toHaveBeenCalledWith('claude', 'opus-5', { hops: [A] });
    expect(results).toEqual([expect.objectContaining({ kind: 'confirmed' })]);
    expect(store['claude:opus-5']?.manual_override).toEqual({ hops: [A] });
  });

  it('persists a newly admitted backup without rewriting unrelated membership', async () => {
    const claude = target('claude', 'opus-5', [A], ['claude']);
    const { api, store } = writes({ 'claude:opus-5': claude.chain });
    api.getVibeAgent = vi.fn(async () => ({ ok: true, agent: full(brief('claude', 'claude', 'opus-5')) }));
    const live = { ...claude, membership: [A, B] };
    const results = await saveSetupRoutes([A, B], [live], api, { dirty: true });
    expect(api.putAgentChain).toHaveBeenCalledWith('claude', 'opus-5', { hops: [A, B] });
    expect(results).toEqual([expect.objectContaining({ kind: 'confirmed' })]);
    expect(store['claude:opus-5']?.manual_override?.hops).toEqual([A, B]);
  });

  it('writes the whole shared route onto an assistant that had a route of its own', async () => {
    // The screen says all three assistants use one default model, so an assistant that
    // arrived with a different chain is moved onto the shared one rather than keeping
    // the part of it that happened to overlap.
    const claude = target('claude', 'opus-5', [A], ['claude']);
    const codex = target('codex', 'gpt-5', [C], ['codex']);
    const { api, store } = writes({ 'claude:opus-5': claude.chain, 'codex:gpt-5': codex.chain });
    const results = await saveSetupRoutes([A, C], [claude, codex], api, { dirty: true });
    expect(api.putAgentChain).toHaveBeenCalledWith('claude', 'opus-5', { hops: [A, C] });
    expect(api.putAgentChain).toHaveBeenCalledWith('codex', 'gpt-5', { hops: [A, C] });
    expect(results.every((row) => row.kind === 'confirmed')).toBe(true);
    expect(store['codex:gpt-5']?.manual_override?.hops).toEqual([A, C]);
  });

  it('reordering an automatic chain persists a manual override and reads it back', async () => {
    const claude = target('claude', 'opus-5', [A, B], ['claude'], 'automatic');
    const { api, store } = writes({ 'claude:opus-5': claude.chain });
    api.getVibeAgent = vi.fn(async () => ({ ok: true, agent: full(brief('claude', 'claude', 'opus-5')) }));
    const results = await saveSetupRoutes([B, A], [claude], api, { dirty: true });
    expect(api.previewAgentChain).toHaveBeenCalledWith('claude', 'opus-5', { manual_override: { hops: [B, A] } });
    expect(api.putAgentChain).toHaveBeenCalledWith('claude', 'opus-5', { hops: [B, A] });
    expect(results).toEqual([expect.objectContaining({ kind: 'confirmed' })]);
    expect(store['claude:opus-5']?.manual_override).toEqual({ hops: [B, A] });
    expect(store['claude:opus-5']?.route_origin).toBe('manual');
  });

  it('keeps the first write and retries only the outstanding target after a later failure', async () => {
    const claude = target('claude', 'opus-5', [A, B], ['claude']);
    const codex = target('codex', 'gpt-5', [A, B], ['codex']);
    const { api, store } = writes({
      'claude:opus-5': claude.chain,
      'codex:gpt-5': codex.chain,
    }, { failOn: 'codex:gpt-5' });
    api.getVibeAgent = vi.fn(async (name: string) => ({
      ok: true,
      agent: full(brief(name, name === 'codex' ? 'codex' : 'claude', name === 'codex' ? 'gpt-5' : 'opus-5')),
    }));
    const shared = [B, A];
    const first = await saveSetupRoutes(shared, [claude, codex], api, { dirty: true });
    expect(first[0]).toEqual(expect.objectContaining({ kind: 'confirmed' }));
    expect(first[1]).toEqual(expect.objectContaining({ kind: 'failed' }));
    expect(store['claude:opus-5']?.manual_override?.hops).toEqual([B, A]);
    expect(store['codex:gpt-5']?.manual_override?.hops).toEqual([A, B]);

    const retried = writes(store);
    retried.api.getVibeAgent = api.getVibeAgent;
    retried.api.listAgents = api.listAgents;
    const second = await retrySetupRoutes(shared, [claude, codex], first, retried.api);
    expect(retried.api.putAgentChain).toHaveBeenCalledTimes(1);
    expect(retried.api.putAgentChain).toHaveBeenCalledWith('codex', 'gpt-5', { hops: [B, A] });
    expect(second[0]?.kind).toBe('confirmed');
    expect(second[1]?.kind).toBe('confirmed');
  });

  // A retry is separated from the hydration it was built on by however long the
  // person spent looking at the failure, so the identity behind the target is the
  // most likely thing to have moved — and the write it is about to repeat is the
  // one thing that cannot be taken back afterwards.
  describe('a target whose identity moved while the failure was on screen', () => {
    const outstanding = (chain: AgentChain) => {
      const claude = target('claude', 'opus-5', [A, B], ['claude']);
      const fixture = writes({ 'claude:opus-5': chain });
      return { claude, ...fixture };
    };
    const failedBefore = [{ key: targetKey('claude', 'opus-5'), kind: 'failed' as const, error: 'boom' }];

    it('writes nothing when the backend has left hub', async () => {
      const { claude, api } = outstanding(target('claude', 'opus-5', [A, B], ['claude']).chain);
      api.getVibeAgent = vi.fn(async () => ({ ok: true, agent: full(brief('claude', 'claude', 'opus-5')) }));
      api.listAgents = vi.fn(async () => [
        { ...supply('claude', [{ name: 'claude', model: 'opus-5' }]), mode: 'direct' as const },
      ]);
      const results = await retrySetupRoutes([B, A], [claude], failedBefore, api);
      expect(results).toEqual([expect.objectContaining({ kind: 'reconcile' })]);
      expect(api.previewAgentChain).not.toHaveBeenCalled();
      expect(api.putAgentChain).not.toHaveBeenCalled();
      expect(api.putAgentModels).not.toHaveBeenCalled();
    });

    it('writes nothing when the Agent now names another model', async () => {
      const { claude, api } = outstanding(target('claude', 'opus-5', [A, B], ['claude']).chain);
      api.getVibeAgent = vi.fn(async () => ({ ok: true, agent: full(brief('claude', 'claude', 'sonnet-4')) }));
      const results = await retrySetupRoutes([B, A], [claude], failedBefore, api);
      expect(results).toEqual([expect.objectContaining({ kind: 'reconcile' })]);
      expect(api.previewAgentChain).not.toHaveBeenCalled();
      expect(api.putAgentChain).not.toHaveBeenCalled();
      expect(api.putAgentModels).not.toHaveBeenCalled();
    });

    it('reports a failed identity read as failed, and still writes nothing', async () => {
      const { claude, api } = outstanding(target('claude', 'opus-5', [A, B], ['claude']).chain);
      api.listAgents = vi.fn(async () => { throw new Error('agents unreachable'); });
      const results = await retrySetupRoutes([B, A], [claude], failedBefore, api);
      expect(results).toEqual([expect.objectContaining({ kind: 'failed', error: 'agents unreachable' })]);
      expect(api.putAgentChain).not.toHaveBeenCalled();
    });
  });

  it('classifies a confirmed desired override as skip on retry', () => {
    const claude = target('claude', 'opus-5', [A, B], ['claude']);
    const desired = [B, A];
    const current = chainOf('claude', 'opus-5', desired, 'manual');
    expect(classifyRetry(claude, current, desired)).toBe('skip');
    expect(classifyRetry(claude, claude.chain, desired)).toBe('retry');
  });

  // A machine that has just installed OpenCode answers with an empty catalog and an
  // Agent that already names the model it was installed with. The backend validates a
  // route override against that catalog, so the preview is refused for a model nobody
  // has had the chance to add — which is every first route this step tries to save.
  describe('an open-menu catalog that does not yet name the Agent\'s model', () => {
    // What the server offers for an id: its protocol, and the suppliers it is
    // offered with. Both are the server's answer — for OpenCode the protocol is
    // derived per model, so an Anthropic-family model states `anthropic` and
    // nothing in the browser may decide otherwise.
    const candidate = (
      id: string,
      native_protocol?: NativeProtocol,
      suppliers: ModelCandidateSupplier[] = [],
    ): ModelCandidate => ({
      id,
      display_name: id.toUpperCase(),
      reasoning_efforts: [],
      suppliers,
      origin: 'provider',
      ...(native_protocol ? { native_protocol } : {}),
    });

    const opencodeWrites = (
      modelId: string,
      catalog: BackendModel[],
      candidates: ModelCandidate[],
    ) => {
      const calls: string[] = [];
      const store: Record<string, AgentChain> = {
        [`opencode:${modelId}`]: chainOf('opencode', modelId, [], 'automatic'),
      };
      let listed = catalog;
      const api = {
        getVibeAgent: vi.fn(async (name: string) => ({ ok: true, agent: full(brief(name, 'opencode', modelId)) })),
        listAgents: vi.fn(async () => [{
          ...supply('opencode', [{ name: 'opencode', model: modelId }]),
          menu_kind: 'open' as const,
          catalog_models: listed,
        }]),
        getAgentModelCandidates: vi.fn(async () => {
          calls.push('candidates');
          return { builtin: [], providers: candidates, in_list: [] };
        }),
        getAgentChain: vi.fn(async (backend: AgentChain['backend'], model: string) => {
          const current = store[`${backend}:${model}`];
          if (!current) throw new Error(`missing ${backend} ${model}`);
          return current;
        }),
        // The backend's own check, as the route override validator states it: a model
        // the catalog does not name cannot carry hops.
        previewAgentChain: vi.fn(async (backend: AgentChain['backend'], model: string, body: { manual_override: { hops: RouteHop[] } | null }) => {
          calls.push('preview');
          if (!listed.some((row) => row.id === model)) throw new Error(`unknown model ${model}`);
          return chainOf(backend, model, body.manual_override?.hops ?? [], 'manual');
        }),
        putAgentChain: vi.fn(async (backend: AgentChain['backend'], model: string, body: { hops: RouteHop[] }) => {
          const next = chainOf(backend, model, body.hops, 'manual');
          store[`${backend}:${model}`] = next;
          return { chain: next };
        }),
        putAgentModels: vi.fn(async (_backend: AgentChain['backend'], body: BackendModelsPut) => {
          calls.push('models');
          listed = body.models;
          return supply('opencode', [{ name: 'opencode', model: 'glm-4.6' }]);
        }),
      };
      return { api, calls, store };
    };
    // The first route this machine ever saves: nothing is chained yet, and the person
    // has just put one source under the model their Agent already names.
    const live = (modelId: string) => ({
      ...target('opencode', modelId, [], ['opencode'], 'automatic'),
      membership: [A],
    });

    it('reconciles the saved model into the catalog before the preview, and the save lands', async () => {
      const { api, calls, store } = opencodeWrites('glm-4.6', [], [candidate('glm-4.6', 'openai_responses')]);
      const results = await saveSetupRoutes([A], [live('glm-4.6')], api, { dirty: true });
      expect(calls).toEqual(['candidates', 'models', 'preview']);
      expect(api.putAgentModels).toHaveBeenCalledWith('opencode', {
        baseline: [],
        models: [expect.objectContaining({
          id: 'glm-4.6', display_name: 'GLM-4.6', origin: 'provider',
          native_protocol: 'openai_responses', locked: false, routeable: true,
        })],
        // An offered id nothing supplies yet still states its projection: empty
        // is the agreement, and the guard it arms is what refuses this write if
        // something starts supplying the id before it commits.
        expected_suppliers: { 'glm-4.6': [] },
      });
      expect(results).toEqual([expect.objectContaining({ kind: 'confirmed' })]);
      expect(store['opencode:glm-4.6']?.manual_override).toEqual({ hops: [A] });
    });

    // The reason the row is the server's candidate rather than one built here. An
    // Anthropic-family model driven through OpenCode speaks the Anthropic protocol;
    // a row built locally would default to Responses and the config would be quietly
    // wrong for every later turn, which is worse than the refusal it replaced.
    it('adopts an Anthropic-family model with the protocol the server states', async () => {
      const id = 'claude-sonnet-4-5';
      const { api, store } = opencodeWrites(id, [], [candidate(id, 'anthropic')]);
      const results = await saveSetupRoutes([A], [live(id)], api, { dirty: true });
      expect(api.putAgentModels).toHaveBeenCalledWith('opencode', {
        baseline: [],
        models: [expect.objectContaining({ id, native_protocol: 'anthropic' })],
        expected_suppliers: { [id]: [] },
      });
      expect(results).toEqual([expect.objectContaining({ kind: 'confirmed' })]);
      expect(store[`opencode:${id}`]?.manual_override).toEqual({ hops: [A] });
    });

    // The other half of the same agreement. The server matches an addition
    // against inventory at commit time, and only an addition that states which
    // projection it was made against can be refused when that inventory moved;
    // one that states none is filed as a hand-written row, which promised
    // nothing, and commits a catalog entry nobody agreed to. The projection is
    // the candidate's own — its supplier names a different model id than the one
    // being added, so a rebuild from anything but the candidate cannot produce it.
    it('states the suppliers the candidate was offered with, not a projection rebuilt here', async () => {
      const supplier: ModelCandidateSupplier = {
        source_id: 'src_a', source_name: 'Relay', model_id: 'glm-4.6-air',
      };
      const { api, store } = opencodeWrites('glm-4.6', [], [candidate('glm-4.6', 'openai_responses', [supplier])]);
      const results = await saveSetupRoutes([A], [live('glm-4.6')], api, { dirty: true });
      expect(api.putAgentModels).toHaveBeenCalledWith('opencode', expect.objectContaining({
        expected_suppliers: { 'glm-4.6': [{ source_id: 'src_a', model_id: 'glm-4.6-air' }] },
      }));
      expect(results).toEqual([expect.objectContaining({ kind: 'confirmed' })]);
      expect(store['opencode:glm-4.6']?.manual_override).toEqual({ hops: [A] });
    });

    // Nobody has an authoritative answer for this id, so nobody invents one: the
    // save refuses exactly as it did before, and the person is sent to the catalog.
    it.each([
      ['no candidate names the model', [] as ModelCandidate[]],
      ['the candidate states no protocol', [candidate('claude-sonnet-4-5')]],
    ])('writes no catalog when %s', async (_label, candidates) => {
      const id = 'claude-sonnet-4-5';
      const { api, calls } = opencodeWrites(id, [], candidates);
      const results = await saveSetupRoutes([A], [live(id)], api, { dirty: true });
      expect(api.putAgentModels).not.toHaveBeenCalled();
      expect(calls).toEqual(['candidates', 'preview']);
      expect(results).toEqual([expect.objectContaining({ kind: 'failed', error: `unknown model ${id}` })]);
    });

    it('reads no candidates and writes no catalog when it already names the model', async () => {
      const held: BackendModel = {
        id: 'glm-4.6', display_name: 'GLM 4.6', origin: 'manual', models_dev_id: null,
        context_window: null, max_output_tokens: null, input_modalities: [], output_modalities: [],
        supports_tools: null, supports_reasoning: null, reasoning_efforts: [],
        native_protocol: 'openai_responses', locked: false, routeable: true,
      };
      const { api, calls } = opencodeWrites('glm-4.6', [held], [candidate('glm-4.6', 'openai_responses')]);
      const results = await saveSetupRoutes([A], [live('glm-4.6')], api, { dirty: true });
      expect(api.getAgentModelCandidates).not.toHaveBeenCalled();
      expect(api.putAgentModels).not.toHaveBeenCalled();
      expect(calls).toEqual(['preview']);
      expect(results).toEqual([expect.objectContaining({ kind: 'confirmed' })]);
    });

    // Why the adoption sits where it does. Another Settings surface moved this
    // target's chain after hydration, so the save reconciles — and reconciling
    // has to mean nothing was persisted. The catalog addition is a write like
    // the chain write, so the decision that refuses both has to happen before
    // either; deciding afterwards and undoing it would answer an ordering rule
    // with a rollback.
    it('writes nothing at all, catalog included, when the chain moved after hydration', async () => {
      const { api, calls, store } = opencodeWrites('glm-4.6', [], [candidate('glm-4.6', 'openai_responses')]);
      store['opencode:glm-4.6'] = chainOf('opencode', 'glm-4.6', [C], 'manual');
      const results = await saveSetupRoutes([A], [live('glm-4.6')], api, { dirty: true });
      expect(results).toEqual([expect.objectContaining({ kind: 'reconcile' })]);
      expect(api.putAgentModels).not.toHaveBeenCalled();
      expect(api.putAgentChain).not.toHaveBeenCalled();
      expect(calls).toEqual([]);
    });
  });
});
