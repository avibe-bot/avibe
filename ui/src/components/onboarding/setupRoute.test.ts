import { describe, expect, it, vi } from 'vitest';
import type { VibeAgentBrief, VibeAgentFull } from '../../context/ApiContext';
import type {
  AgentChain,
  AgentSupply,
  BackendModelCandidates,
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
  repairMissingSetupModel,
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
  it('excludes disabled backends and their unique hops from the shared route', async () => {
    const claude = brief('claude', 'claude', 'opus-5');
    const codex = brief('codex', 'codex', 'gpt-5');
    const reads = {
      listVibeAgents: vi.fn(async () => ({ ok: true, agents: [claude, codex], default_agent_name: null })),
      getVibeAgent: vi.fn(async (name: string) => ({ ok: true, agent: full(name === 'claude' ? claude : codex) })),
      getAgentChain: vi.fn(async (backend: AgentChain['backend'], model: string) =>
        chainOf(backend, model, backend === 'claude' ? [A] : [C])),
    };
    const hydration = await hydrateSetupRoutes(reads, [supply('claude', []), supply('codex', [])], new Set(['claude']));
    expect(hydration.targets.map((row) => row.backend)).toEqual(['claude']);
    expect(hydration.union).toEqual([A]);
    expect(reads.getVibeAgent).not.toHaveBeenCalledWith('codex', expect.anything());
  });

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

  it('does not present a partial target list as a complete shared route', async () => {
    const claude = brief('claude', 'claude', 'opus-5');
    const codex = brief('codex', 'codex', 'gpt-5');
    await expect(hydrateSetupRoutes({
      listVibeAgents: vi.fn(async () => ({ ok: true, agents: [claude, codex], default_agent_name: null })),
      getVibeAgent: vi.fn(async (name: string) => ({ ok: true, agent: full(name === 'claude' ? claude : codex) })),
      getAgentChain: vi.fn(async (backend: AgentChain['backend'], model: string) => {
        if (backend === 'codex') throw new Error('unavailable');
        return chainOf(backend, model, [A]);
      }),
    }, [supply('claude', []), supply('codex', [])])).rejects.toThrow('onboarding.route.readFailed');
  });

  it('reports a model-less enabled assistant alongside existing route targets', async () => {
    const claude = brief('claude', 'claude', 'opus-5');
    const codex = { ...brief('codex', 'codex', 'gpt-5'), model: null };
    const hydration = await hydrateSetupRoutes({
      listVibeAgents: vi.fn(async () => ({ ok: true, agents: [claude, codex], default_agent_name: null })),
      getVibeAgent: vi.fn(async (name: string) => ({ ok: true, agent: full(name === 'claude' ? claude : codex) })),
      getAgentChain: vi.fn(async (backend: AgentChain['backend'], model: string) => chainOf(backend, model, [A])),
    }, [supply('claude', []), supply('codex', [])]);
    expect(hydration.targets).toHaveLength(1);
    expect(hydration.missingModels).toEqual([{ backend: 'codex', agentName: 'codex' }]);
  });

  it('also refuses a partial list when an enabled assistant detail is unreadable', async () => {
    const claude = brief('claude', 'claude', 'opus-5');
    const codex = brief('codex', 'codex', 'gpt-5');
    await expect(hydrateSetupRoutes({
      listVibeAgents: vi.fn(async () => ({ ok: true, agents: [claude, codex], default_agent_name: null })),
      getVibeAgent: vi.fn(async (name: string) => name === 'codex'
        ? { ok: false, agent: null }
        : { ok: true, agent: full(claude) }),
      getAgentChain: vi.fn(async (backend: AgentChain['backend'], model: string) => chainOf(backend, model, [A])),
    }, [supply('claude', []), supply('codex', [])])).rejects.toThrow('onboarding.route.readFailed');
  });
});

describe('saveSetupRoutes', () => {
  const writes = (
    chains: Record<string, AgentChain>,
    options: { failOn?: string } = {},
  ) => {
    const store = { ...chains };
    const agentModels: Record<string, string> = { claude: 'opus-5', codex: 'gpt-5', opencode: 'gpt-5' };
    const keyOf = (backend: string, model: string) => `${backend}:${model}`;
    return {
      store,
      api: {
        getVibeAgent: vi.fn(async (name: string) => ({
          ok: true,
          agent: full(brief(name, name as VibeAgentBrief['backend'], agentModels[name]!)),
        })),
        updateVibeAgent: vi.fn(async (name: string, payload: { model: string }) => {
          agentModels[name] = payload.model;
          return { ok: true, agent: full(brief(name, name as VibeAgentBrief['backend'], payload.model)) };
        }),
        listAgents: vi.fn(async () => [supply('claude', [{ name: 'claude', model: 'opus-5' }]), supply('codex', [{ name: 'codex', model: 'gpt-5' }])]),
        getAgentChain: vi.fn(async (backend: AgentChain['backend'], model: string) => {
          const current = store[keyOf(backend, model)];
          return current ?? chainOf(backend, model, [], 'automatic');
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
        getAgentModelCandidates: vi.fn(async (): Promise<BackendModelCandidates> => ({ builtin: [], providers: [], in_list: [] })),
        putAgentModels: vi.fn(async () => supply('claude', [{ name: 'claude', model: 'opus-5' }])),
      },
    };
  };

  it('confirms a lost chain response before assigning a missing Agent model', async () => {
    const { api, store } = writes({});
    let model: string | null = null;
    api.getVibeAgent = vi.fn(async () => ({
      ok: true, agent: { ...full(brief('claude', 'claude', 'opus-5')), model },
    }));
    api.updateVibeAgent = vi.fn(async () => {
      model = 'opus-5';
      throw new Error('response lost');
    });
    api.putAgentChain = vi.fn(async (backend, modelId, body) => {
      store[`${backend}:${modelId}`] = chainOf(backend, modelId, body.hops);
      throw new Error('response lost');
    });
    await expect(repairMissingSetupModel('claude', 'claude', 'opus-5', [A], api)).resolves.toBe('opus-5');
    expect(store['claude:opus-5']?.manual_override?.hops).toEqual([A]);
    expect(api.putAgentChain.mock.invocationCallOrder[0]).toBeLessThan(api.updateVibeAgent.mock.invocationCallOrder[0]!);
  });

  it('does not assign a missing Agent model when its route write is unconfirmed', async () => {
    const { api } = writes({});
    api.getVibeAgent = vi.fn(async () => ({
      ok: true, agent: { ...full(brief('claude', 'claude', 'opus-5')), model: null },
    }));
    api.putAgentChain = vi.fn(async () => { throw new Error('write failed'); });
    await expect(repairMissingSetupModel('claude', 'claude', 'opus-5', [A], api))
      .rejects.toThrow('onboarding.route.chainWriteFailed');
    expect(api.updateVibeAgent).not.toHaveBeenCalled();
  });

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
    expect(api.putAgentChain).toHaveBeenCalledWith('codex', 'opus-5', { hops: [A, C] });
    expect(results.every((row) => row.kind === 'confirmed')).toBe(true);
    expect(store['codex:opus-5']?.manual_override?.hops).toEqual([A, C]);
    expect(api.updateVibeAgent).toHaveBeenCalledWith('codex', { model: 'opus-5' });
  });

  it('makes Codex and OpenCode menu models follow the shared first hop while Claude keeps its menu entry', async () => {
    const first = hop('src_a', 'claude-opus-5-5');
    const order = [first, C, B];
    const targets = [
      target('claude', 'opus-5', [A], ['claude']),
      target('codex', 'gpt-5', [C], ['codex']),
      target('opencode', 'gpt-5', [C], ['opencode']),
    ];
    const { api, store } = writes(Object.fromEntries(targets.map((row) =>
      [`${row.backend}:${row.modelId}`, row.chain])));
    const models: Record<string, string> = { claude: 'opus-5', codex: 'gpt-5', opencode: 'gpt-5' };
    api.getVibeAgent = vi.fn(async (name: string) => ({
      ok: true, agent: full(brief(name, name as VibeAgentBrief['backend'], models[name]!)),
    }));
    api.updateVibeAgent = vi.fn(async (name: string, payload: { model: string }) => {
      models[name] = payload.model;
      return { ok: true, agent: full(brief(name, name as VibeAgentBrief['backend'], payload.model)) };
    });
    api.listAgents = vi.fn(async () => targets.map((row) => supply(row.backend, [{ name: row.backend, model: models[row.backend]! }])));
    const results = await saveSetupRoutes(order, targets, api);
    expect(results.map((row) => row.kind)).toEqual(['confirmed', 'confirmed', 'confirmed']);
    expect(models).toEqual({ claude: 'opus-5', codex: first.model_id, opencode: first.model_id });
    for (const backend of ['codex', 'opencode'] as const) {
      expect(store[`${backend}:${first.model_id}`]?.manual_override?.hops).toEqual(order);
      expect(store[`${backend}:gpt-5`]?.manual_override?.hops).toEqual([C]);
    }
    expect(store['claude:opus-5']?.manual_override?.hops).toEqual(order);
  });

  it('adds a Codex provider candidate without an OpenCode-only protocol before switching models', async () => {
    const codex = target('codex', 'gpt-5', [C], ['codex']);
    const { api, store } = writes({ 'codex:gpt-5': codex.chain });
    const preferred = hop('src_a', 'claude-opus-5-5');
    api.listAgents = vi.fn(async () => [{
      ...supply('codex', [{ name: 'codex', model: 'gpt-5' }]), catalog_models: [],
    }]);
    api.getAgentModelCandidates = vi.fn(async () => ({
      builtin: [], in_list: [], providers: [{
        id: preferred.model_id, display_name: null, reasoning_efforts: [], origin: 'provider' as const,
        suppliers: [{ source_id: preferred.source_id, source_name: 'Anthropic', model_id: preferred.model_id }],
      }],
    }));
    const results = await saveSetupRoutes([preferred, C], [codex], api);
    expect(results).toEqual([expect.objectContaining({ kind: 'confirmed' })]);
    expect(api.putAgentModels).toHaveBeenCalledWith('codex', expect.objectContaining({
      models: [expect.objectContaining({ id: preferred.model_id, origin: 'provider' })],
      expected_suppliers: { [preferred.model_id]: [preferred] },
    }));
    expect(store[`codex:${preferred.model_id}`]?.manual_override?.hops).toEqual([preferred, C]);
    expect(api.updateVibeAgent).toHaveBeenCalledWith('codex', { model: preferred.model_id });
  });

  it('keeps the old Agent model when writing the preferred chain fails', async () => {
    const codex = target('codex', 'gpt-5', [C], ['codex']);
    const { api } = writes({ 'codex:gpt-5': codex.chain }, { failOn: 'codex:opus-5' });
    const results = await saveSetupRoutes([A, C], [codex], api);
    expect(results[0]?.kind).toBe('failed');
    expect(api.updateVibeAgent).not.toHaveBeenCalled();
  });

  it('retries a failed model switch after the preferred chain was saved', async () => {
    const codex = target('codex', 'gpt-5', [C], ['codex']);
    const { api, store } = writes({ 'codex:gpt-5': codex.chain });
    api.updateVibeAgent.mockRejectedValueOnce(new Error('model patch denied'));
    const first = await saveSetupRoutes([A, C], [codex], api);
    expect(first[0]).toEqual(expect.objectContaining({ kind: 'failed', error: 'onboarding.route.modelSwitchFailed' }));
    expect(store['codex:opus-5']?.manual_override?.hops).toEqual([A, C]);
    expect(store['codex:gpt-5']?.manual_override?.hops).toEqual([C]);
    const second = await retrySetupRoutes([A, C], [codex], first, api);
    expect(second[0]?.kind).toBe('confirmed');
    expect(api.putAgentChain).toHaveBeenCalledTimes(1);
    expect(api.updateVibeAgent).toHaveBeenCalledTimes(2);
  });

  it('confirms a model switch whose response was lost after the write committed', async () => {
    const codex = target('codex', 'gpt-5', [C], ['codex']);
    const { api } = writes({ 'codex:gpt-5': codex.chain });
    const switchModel = api.updateVibeAgent;
    api.updateVibeAgent = vi.fn(async (name: string, payload: { model: string }) => {
      await switchModel(name, payload);
      throw new Error('connection closed after commit');
    });
    const results = await saveSetupRoutes([A, C], [codex], api);
    expect(results).toEqual([expect.objectContaining({ kind: 'confirmed' })]);
    expect(api.putAgentChain).toHaveBeenCalledExactlyOnceWith('codex', 'opus-5', { hops: [A, C] });
  });

  it('keeps a confirmed switched target during retry when its eligible route is a subset', async () => {
    const codex = target('codex', 'gpt-5', [C], ['codex']);
    const { api } = writes({ 'codex:gpt-5': codex.chain });
    api.listAgents = vi.fn(async () => [{ ...supply('codex', [{ name: 'codex', model: 'gpt-5' }]),
      sources: { order: ['src_a'], eligibility: [
        { source_id: 'src_a', eligible: true }, { source_id: 'src_b', eligible: false },
      ] } }]);
    const first = await saveSetupRoutes([A, C], [codex], api);
    expect(first[0]?.kind).toBe('confirmed');
    const second = await retrySetupRoutes([A, C], [codex], first, api);
    expect(second[0]?.kind).toBe('confirmed');
    expect(api.putAgentChain).toHaveBeenCalledTimes(1);
  });

  it('shares Hub keys while excluding backend-specific native subscription hops', async () => {
    const claude = target('claude', 'opus-5', [A], ['claude']);
    const codex = target('codex', 'gpt-5', [C], ['codex']);
    const { api, store } = writes({ 'claude:opus-5': claude.chain, 'codex:gpt-5': codex.chain });
    const shared = [A, C, D];
    api.listAgents = vi.fn(async () => [
      { ...supply('claude', []), sources: { order: ['src_a', 'src_c'], eligibility: [
        { source_id: 'src_a', eligible: true }, { source_id: 'src_b', eligible: false }, { source_id: 'src_c', eligible: true },
      ] } },
      { ...supply('codex', []), sources: { order: ['src_b', 'src_c'], eligibility: [
        { source_id: 'src_a', eligible: false }, { source_id: 'src_b', eligible: true }, { source_id: 'src_c', eligible: true },
      ] } },
    ]);
    const results = await saveSetupRoutes(shared, [claude, codex], api);
    expect(results.every((row) => row.kind === 'confirmed')).toBe(true);
    expect(store['claude:opus-5']?.manual_override?.hops).toEqual([A, D]);
    expect(store['codex:gpt-5']?.manual_override?.hops).toEqual([C, D]);
  });

  it('reconciles source eligibility drift before either the first save or a retry writes', async () => {
    const claude = brief('claude', 'claude', 'opus-5');
    const baselineSupply = { ...supply('claude', [{ name: 'claude', model: 'opus-5' }]),
      sources: { order: ['src_a', 'src_b'], eligibility: [
        { source_id: 'src_a', eligible: true }, { source_id: 'src_b', eligible: true },
      ] } };
    const hydration = await hydrateSetupRoutes({
      listVibeAgents: vi.fn(async () => ({ ok: true, agents: [claude], default_agent_name: 'claude' })),
      getVibeAgent: vi.fn(async () => ({ ok: true, agent: full(claude) })),
      getAgentChain: vi.fn(async () => chainOf('claude', 'opus-5', [A, C])),
    }, [baselineSupply]);
    const { api } = writes({ 'claude:opus-5': chainOf('claude', 'opus-5', [A, C]) });
    api.listAgents = vi.fn(async () => [{ ...baselineSupply,
      sources: { order: ['src_a'], eligibility: [
        { source_id: 'src_a', eligible: true }, { source_id: 'src_b', eligible: false },
      ] } }]);
    const first = await saveSetupRoutes([C, A], hydration.targets, api);
    expect(first[0]?.kind).toBe('reconcile');
    const second = await retrySetupRoutes([C, A], hydration.targets,
      [{ key: targetKey('claude', 'opus-5'), kind: 'failed', error: 'transient' }], api);
    expect(second[0]?.kind).toBe('reconcile');
    expect(api.previewAgentChain).not.toHaveBeenCalled();
    expect(api.putAgentChain).not.toHaveBeenCalled();
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
    }, { failOn: 'codex:sonnet-4' });
    const shared = [B, A];
    const first = await saveSetupRoutes(shared, [claude, codex], api, { dirty: true });
    expect(first[0]).toEqual(expect.objectContaining({ kind: 'confirmed' }));
    expect(first[1]).toEqual(expect.objectContaining({ kind: 'failed' }));
    expect(store['claude:opus-5']?.manual_override?.hops).toEqual([B, A]);
    expect(store['codex:gpt-5']?.manual_override?.hops).toEqual([A, B]);

    const retried = writes(store);
    retried.api.listAgents = api.listAgents;
    const second = await retrySetupRoutes(shared, [claude, codex], first, retried.api);
    expect(retried.api.putAgentChain).toHaveBeenCalledTimes(1);
    expect(retried.api.putAgentChain).toHaveBeenCalledWith('codex', 'sonnet-4', { hops: [B, A] });
    expect(second[0]?.kind).toBe('confirmed');
    expect(second[1]?.kind).toBe('confirmed');
  });

  it('rechecks a skipped target when the route draft changes before retry', async () => {
    const claude = target('claude', 'opus-5', [A, B], ['claude']);
    const codex = target('codex', 'gpt-5', [B, A], ['codex']);
    const { api, store } = writes({
      'claude:opus-5': claude.chain,
      'codex:gpt-5': codex.chain,
    }, { failOn: 'codex:opus-5' });
    const first = await saveSetupRoutes([A, B], [claude, codex], api);
    expect(first.map((row) => row.kind)).toEqual(['skipped', 'failed']);

    const retried = writes(store);
    const next = await retrySetupRoutes([B, A], [claude, codex], first, retried.api);
    expect(retried.api.putAgentChain).toHaveBeenCalledWith('claude', 'opus-5', { hops: [B, A] });
    expect(next.every((row) => row.kind === 'confirmed' || row.kind === 'skipped')).toBe(true);
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
      let agentModel = modelId;
      const api = {
        getVibeAgent: vi.fn(async (name: string) => ({ ok: true, agent: full(brief(name, 'opencode', agentModel)) })),
        updateVibeAgent: vi.fn(async (name: string, payload: { model: string }) => ({
          ok: true, agent: full(brief(name, 'opencode', agentModel = payload.model)),
        })),
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
          return { ...supply('opencode', [{ name: 'opencode', model: modelId }]), catalog_models: listed };
        }),
      };
      return { api, calls, store };
    };
    // The first route this machine ever saves: nothing is chained yet, and the person
    // has just put one source under the model their Agent already names.
    const live = (modelId: string) => ({
      ...target('opencode', modelId, [], ['opencode'], 'automatic'),
      membership: [hop('src_a', modelId)],
    });
    const desired = (modelId: string) => [hop('src_a', modelId)];

    it('reconciles the saved model into the catalog before the preview, and the save lands', async () => {
      const { api, calls, store } = opencodeWrites('glm-4.6', [], [candidate('glm-4.6', 'openai_responses')]);
      const results = await saveSetupRoutes(desired('glm-4.6'), [live('glm-4.6')], api, { dirty: true });
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
      expect(store['opencode:glm-4.6']?.manual_override).toEqual({ hops: desired('glm-4.6') });
    });

    // The reason the row is the server's candidate rather than one built here. An
    // Anthropic-family model driven through OpenCode speaks the Anthropic protocol;
    // a row built locally would default to Responses and the config would be quietly
    // wrong for every later turn, which is worse than the refusal it replaced.
    it('adopts an Anthropic-family model with the protocol the server states', async () => {
      const id = 'claude-sonnet-4-5';
      const { api, store } = opencodeWrites(id, [], [candidate(id, 'anthropic')]);
      const results = await saveSetupRoutes(desired(id), [live(id)], api, { dirty: true });
      expect(api.putAgentModels).toHaveBeenCalledWith('opencode', {
        baseline: [],
        models: [expect.objectContaining({ id, native_protocol: 'anthropic' })],
        expected_suppliers: { [id]: [] },
      });
      expect(results).toEqual([expect.objectContaining({ kind: 'confirmed' })]);
      expect(store[`opencode:${id}`]?.manual_override).toEqual({ hops: desired(id) });
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
      const results = await saveSetupRoutes(desired('glm-4.6'), [live('glm-4.6')], api, { dirty: true });
      expect(api.putAgentModels).toHaveBeenCalledWith('opencode', expect.objectContaining({
        expected_suppliers: { 'glm-4.6': [{ source_id: 'src_a', model_id: 'glm-4.6-air' }] },
      }));
      expect(results).toEqual([expect.objectContaining({ kind: 'confirmed' })]);
      expect(store['opencode:glm-4.6']?.manual_override).toEqual({ hops: desired('glm-4.6') });
    });

    // Nobody has an authoritative answer for this id, so nobody invents one: the
    // save refuses exactly as it did before, and the person is sent to the catalog.
    it.each([
      ['no candidate names the model', [] as ModelCandidate[]],
      ['the candidate states no protocol', [candidate('claude-sonnet-4-5')]],
    ])('writes no catalog when %s', async (_label, candidates) => {
      const id = 'claude-sonnet-4-5';
      const { api, calls } = opencodeWrites(id, [], candidates);
      const results = await saveSetupRoutes(desired(id), [live(id)], api, { dirty: true });
      expect(api.putAgentModels).not.toHaveBeenCalled();
      expect(calls).toEqual(['candidates']);
      expect(results).toEqual([expect.objectContaining({ kind: 'failed', error: 'onboarding.route.catalogFailed' })]);
    });

    it('reads no candidates and writes no catalog when it already names the model', async () => {
      const held: BackendModel = {
        id: 'glm-4.6', display_name: 'GLM 4.6', origin: 'manual', models_dev_id: null,
        context_window: null, max_output_tokens: null, input_modalities: [], output_modalities: [],
        supports_tools: null, supports_reasoning: null, reasoning_efforts: [],
        native_protocol: 'openai_responses', locked: false, routeable: true,
      };
      const { api, calls } = opencodeWrites('glm-4.6', [held], [candidate('glm-4.6', 'openai_responses')]);
      const results = await saveSetupRoutes(desired('glm-4.6'), [live('glm-4.6')], api, { dirty: true });
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
      const results = await saveSetupRoutes(desired('glm-4.6'), [live('glm-4.6')], api, { dirty: true });
      expect(results).toEqual([expect.objectContaining({ kind: 'reconcile' })]);
      expect(api.putAgentModels).not.toHaveBeenCalled();
      expect(api.putAgentChain).not.toHaveBeenCalled();
      expect(calls).toEqual([]);
    });
  });
});
