import { describe, expect, it } from 'vitest';

import { modelChainKey } from './modelRows';
import { buildSupplyRelations } from './supplyRelations';
import type { AgentChain, AgentSupply, Source } from './types';

const source = (id: string, channel: Source['supply_channel'], status: Source['state']['status'] = 'active'): Source => ({
  id,
  last_discovered_at: null,
  kind: channel === 'native_cli' ? 'subscription' : 'api_key',
  vendor: 'anthropic',
  display_name: id,
  protocol: 'anthropic',
  supply_channel: channel,
  billing: channel === 'native_cli' ? 'monthly' : 'metered',
  state: { status, retry_at: status === 'cooldown' ? '2099-01-01T00:00:00Z' : null, detail_key: null },
  models: [],
});

const agent: AgentSupply = {
  backend: 'claude',
  mode: 'hub',
  menu_kind: 'fixed',
  selected_model_id: 'model-a',
  selected_model_explicit: true,
  sources: { order: ['native', 'relay', 'unused'], eligibility: [] },
  routes: { 'model-a': { hops: [{ source_id: 'native', model_id: 'model-a' }, { source_id: 'relay', model_id: 'model-a' }, { source_id: 'unused', model_id: 'model-a' }] } },
  supply_status: 'degraded',
  model_supply: [{ model_id: 'model-a', chain_length: 3 }],
  named_agents: [],
  builtin_models: ['model-a'],
  menu: null,
};

const chain = (current: string, headHealth: AgentChain['chain'][number]['health'] = 'healthy', headRunnable = true, reason: AgentChain['chain'][number]['reason'] = null): AgentChain => ({
  contract_version: 5,
  backend: 'claude',
  model_id: 'model-a',
  current: { source_id: current, model_id: 'model-a' },
  chain: [
    { source_id: 'native', model_id: 'model-a', channel: 'native_cli', health: headHealth, runnable: headRunnable, reason, retry_at: headHealth === 'cooldown' ? '2099-01-01T00:00:00Z' : null },
    { source_id: 'relay', model_id: 'model-a', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null },
    { source_id: 'unused', model_id: 'model-a', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null },
  ],
  supply_state: 'ok',
});

describe('buildSupplyRelations', () => {
  it('derives relation ink from configured routes and the exact current hop', () => {
    const sources = [source('native', 'native_cli'), source('relay', 'hub'), source('unused', 'hub')];
    const key = modelChainKey('claude', 'model-a');
    expect(buildSupplyRelations([agent], sources, { [key]: { kind: 'ready', chain: chain('native') } })).toEqual([
      { sourceId: 'native', backend: 'claude', kind: 'native' },
      { sourceId: 'relay', backend: 'claude', kind: 'connected_unused' },
      { sourceId: 'unused', backend: 'claude', kind: 'connected_unused' },
    ]);
    expect(buildSupplyRelations([agent], sources, { [key]: { kind: 'ready', chain: chain('relay', 'cooldown', false) } })[1]).toEqual(
      { sourceId: 'relay', backend: 'claude', kind: 'takeover' },
    );
    expect(buildSupplyRelations([agent], sources, { [key]: { kind: 'ready', chain: chain('relay', 'cooldown', false, 'native_cli_unavailable') } })[1]).toEqual(
      { sourceId: 'relay', backend: 'claude', kind: 'gateway' },
    );
    for (const health of ['needs_action', 'error'] as const) {
      expect(buildSupplyRelations([agent], sources, { [key]: { kind: 'ready', chain: chain('relay', health, false) } })[1]).toEqual(
        { sourceId: 'relay', backend: 'claude', kind: 'gateway' },
      );
    }
    expect(buildSupplyRelations([agent], sources, { [key]: { kind: 'ready', chain: chain('relay') } })[0]).toEqual(
      { sourceId: 'native', backend: 'claude', kind: 'connected_unused' },
    );
    const staleCooldown = [source('native', 'native_cli'), source('relay', 'hub', 'cooldown'), source('unused', 'hub')];
    expect(buildSupplyRelations([agent], staleCooldown, { [key]: { kind: 'ready', chain: chain('relay') } })[1]).toEqual(
      { sourceId: 'relay', backend: 'claude', kind: 'gateway' },
    );
  });

  it('renders cooling relations as unavailable and draws no relations for direct backends', () => {
    const sources = [source('native', 'native_cli', 'cooldown'), source('relay', 'hub'), source('unused', 'hub')];
    expect(buildSupplyRelations([agent], sources, {} )[0]?.kind).toBe('unavailable');
    expect(buildSupplyRelations([{ ...agent, mode: 'direct', routes: null, sources: null }], sources, {})).toEqual([]);
  });
});
