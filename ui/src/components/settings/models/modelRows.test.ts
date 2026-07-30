import { describe, expect, it } from 'vitest';

import {
  agentNeedsModelSelection,
  listedModelIds,
  manualModelSources,
  modelChainKey,
  modelIssueCount,
  modelNeedsAction,
  orderedRouteSources,
  type ModelChainRead,
} from './modelRows';
import type { AgentSupply, RuntimeDependency, Source } from './types';

const agent = (over: Partial<AgentSupply> = {}): AgentSupply => ({
  backend: 'claude',
  mode: 'hub',
  menu_kind: 'fixed',
  selected_by_agent: null,
  selected_model_id: 'current-model',
  current: null,
  sources: { policy: 'follow', order: [], eligibility: [] },
  supply_status: 'ok',
  model_supply: [{ model_id: 'supply-model', chain_length: 1 }],
  named_agents: [],
  mappings: [{ builtin_id: 'mapped-model', target_model_id: 'target-model', enabled: true }],
  menu: null,
  builtin_models: ['builtin-model', 'current-model'],
  standard_vendors: null,
  ...over,
});

const read = (over: Partial<Extract<ModelChainRead, { kind: 'ready' }>['chain']> = {}): ModelChainRead => ({
  kind: 'ready',
  chain: {
    contract_version: 4,
    backend: 'claude',
    model_id: 'builtin-model',
    supply_state: 'ok',
    chain: [{
      source_id: 'src_a',
      channel: 'hub',
      via_mapping: false,
      resolved_model_id: null,
      health: 'healthy',
      runnable: true,
      reason: null,
      retry_at: null,
    }],
    ...over,
  },
});

const runtime = (health: RuntimeDependency['status']['health']): RuntimeDependency => ({
  manifest: { name: 'cliproxyapi', version: '1', source_sha: 'a', assets: [] },
  status: { health, verified: health === 'ok' },
});

const source = (id: string): Source => ({
  id,
  kind: 'api_key',
  vendor: 'custom',
  display_name: id,
  protocol: 'openai_compatible',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active' },
  models: [],
});

describe('model row projection', () => {
  it('keeps every server-published model surface without duplicates', () => {
    expect(listedModelIds(agent())).toEqual(['builtin-model', 'current-model', 'supply-model', 'mapped-model']);
  });

  it('keeps every eligible source in the route picker, including sources outside this agent order', () => {
    const ordered = orderedRouteSources(
      agent({
        sources: {
          policy: 'follow',
          order: ['src_b'],
          eligibility: [
            { source_id: 'src_a', eligible: true },
            { source_id: 'src_b', eligible: true },
            { source_id: 'src_c', eligible: false },
          ],
        },
      }),
      [source('src_a'), source('src_b'), source('src_c')],
    );
    expect(ordered.map((item) => item.id)).toEqual(['src_b', 'src_a']);
  });

  it('only offers API-key inventories for manual models', () => {
    const subscription = { ...source('src_subscription'), kind: 'subscription' as const };
    expect(manualModelSources([subscription, source('src_key')]).map((item) => item.id)).toEqual(['src_key']);
  });

  it('counts an interrupted model and the honest row-zero state independently', () => {
    const row = read({ supply_state: 'interrupted', chain: [] });
    const a = agent({ selected_model_id: null, builtin_models: ['builtin-model'], model_supply: [] });
    expect(agentNeedsModelSelection(a)).toBe(true);
    expect(modelIssueCount([a], { [modelChainKey('claude', 'builtin-model')]: row })).toBe(2);
  });

  it('attributes runtime failure only to a model whose current head uses the managed channel', () => {
    expect(modelNeedsAction(agent(), 'builtin-model', read(), runtime('down'))).toBe(true);
    const native = read({ chain: [{
      source_id: 'src_native',
      channel: 'native_cli',
      via_mapping: false,
      resolved_model_id: null,
      health: 'healthy',
      runnable: true,
      reason: null,
      retry_at: null,
    }] });
    expect(modelNeedsAction(agent(), 'builtin-model', native, runtime('down'))).toBe(false);
  });

  it('does not turn an automatic cooldown into manual work', () => {
    expect(modelNeedsAction(agent(), 'builtin-model', read({ supply_state: 'waiting' }))).toBe(false);
  });

  it('does not claim health when the live chain could not be read', () => {
    expect(modelNeedsAction(agent(), 'builtin-model', { kind: 'error' })).toBe(true);
  });
});
