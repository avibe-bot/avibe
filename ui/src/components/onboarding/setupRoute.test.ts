import { describe, expect, it, vi } from 'vitest';
import type { VibeAgentBrief, VibeAgentFull } from '../../context/ApiContext';
import type { AgentChain, AgentSupply, RouteHop } from '../settings/models/types';
import {
  classifyRetry,
  hydrateSetupRoutes,
  projectTargetHops,
  retrySetupRoutes,
  saveSetupRoutes,
  selectSetupRouteTarget,
  targetChanged,
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
    expect(projectTargetHops([B, A], claude.membership)).toEqual([B, A]);
  });

  it('does not treat a displayed union as a write when existing orders diverge', () => {
    const claude = target('claude', 'opus-5', [A, B], ['claude']);
    const codex = target('codex', 'gpt-5', [B, A], ['codex']);
    const union = unionRouteOrder([claude, codex]);
    expect(union).toEqual([A, B]);
    expect(targetChanged(union, claude)).toBe(false);
    expect(targetChanged(union, codex)).toBe(true);
  });

  it('projects disjoint native memberships instead of sharing every row', () => {
    const claude = target('claude', 'opus-5', [A, B], ['claude']);
    const codex = target('codex', 'gpt-5', [C, D], ['codex']);
    const union = unionRouteOrder([claude, codex]);
    expect(union).toEqual([A, B, C, D]);
    expect(projectTargetHops(union, claude.membership)).toEqual([A, B]);
    expect(projectTargetHops(union, codex.membership)).toEqual([C, D]);
    expect(targetChanged(union, claude)).toBe(false);
    expect(targetChanged(union, codex)).toBe(false);
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

  it('classifies a confirmed desired override as skip on retry', () => {
    const claude = target('claude', 'opus-5', [A, B], ['claude']);
    const desired = [B, A];
    const current = chainOf('claude', 'opus-5', desired, 'manual');
    expect(classifyRetry(claude, current, desired)).toBe('skip');
    expect(classifyRetry(claude, claude.chain, desired)).toBe('retry');
  });
});
