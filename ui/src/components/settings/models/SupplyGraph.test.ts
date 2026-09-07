import { describe, expect, it } from 'vitest';

import { modelChainKey } from './modelRows';
import { degradedRegion, loadingRegion, readyRegion, unreadRegion } from './regionRead';
import { freshRuntimeProjection } from './runtimeLifecycle';
import { buildSupplyRelations as buildRelations } from './supplyRelations';
import type { AgentChain, AgentSupply, RuntimeDependency, Source } from './types';

const runtime: RuntimeDependency = {
  contract_version: 10,
  manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1.0.0', source_sha: 'fixture', assets: [] },
  status: { installed_version: '1.0.0', verified: true, listening: null, health: 'ok', last_check: null },
};
const buildSupplyRelations = (
  agents: AgentSupply[],
  sources: Source[],
  chains: Parameters<typeof buildRelations>[2],
) => buildRelations(agents, sources, chains, freshRuntimeProjection(readyRegion(runtime)));

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
  cli_present: true,
  mode: 'hub',
  menu_kind: 'fixed',
  selected_model_id: 'model-a',
  selected_model_explicit: true,
  sources: { order: ['native', 'relay', 'unused'], eligibility: [] },
  routes: { 'model-a': { hops: [{ source_id: 'native', model_id: 'model-a' }, { source_id: 'relay', model_id: 'model-a' }, { source_id: 'unused', model_id: 'model-a' }] } },
  supply_status: 'degraded',
  model_supply: [{ route_origin: "manual" as const, model_id: 'model-a', chain_length: 3, has_runnable_hop: true }],
  named_agents: [],
  builtin_models: ['model-a'],
  menu: null,
};

const chain = (current: string, headHealth: AgentChain['chain'][number]['health'] = 'healthy', headRunnable = true, reason: AgentChain['chain'][number]['reason'] = null): AgentChain => ({ manual_override: {hops:[{source_id:'native',model_id:'model-a'},{source_id:'relay',model_id:'model-a'},{source_id:'unused',model_id:'model-a'}]}, route_origin: "manual" as const,
  contract_version: 10,
  backend: 'claude',
  model_id: 'model-a',
  current: { source_id: current, model_id: 'model-a' },
  chain: [
    { source_id: 'native', model_id: 'model-a', channel: 'native_cli', health: headHealth, runnable: headRunnable, reason, retry_at: headHealth === 'cooldown' || headHealth === 'backoff' ? '2099-01-01T00:00:00Z' : null },
    { source_id: 'relay', model_id: 'model-a', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null },
    { source_id: 'unused', model_id: 'model-a', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null },
  ],
  supply_state: 'ok',
});

describe('buildSupplyRelations', () => {
  describe.each(['claude', 'codex', 'opencode'] as const)('%s effective routes', (backend) => {
    it.each(['automatic', 'passthrough', 'manual'] as const)('draws every %s supplier once from the effective chain', (origin) => {
      const projection: AgentChain = { ...chain('relay'), backend, route_origin: origin,
        chain: chain('relay').chain.map((hop) => ({ ...hop, channel: 'hub' })),
        manual_override: origin === 'manual' ? chain('relay').manual_override : null };
      const supplied: AgentSupply = { ...agent, backend,
        menu_kind: backend === 'opencode' ? 'open' : 'fixed', menu: { view: 'featured', checked: ['model-a'] },
        routes: origin === 'manual' ? agent.routes : {},
        model_supply: [{ model_id: 'model-a', route_origin: origin, chain_length: 3, has_runnable_hop: true }] };
      const sources = [source('native', 'hub'), source('relay', 'hub'), source('unused', 'hub'), source('off-route', 'hub')];
      expect(buildSupplyRelations([supplied], sources, { [modelChainKey(backend, 'model-a')]: readyRegion(projection) })).toEqual([
        { sourceId: 'native', backend, kind: 'connected_unused' },
        { sourceId: 'relay', backend, kind: origin === 'passthrough' ? 'passthrough' : 'gateway' },
        { sourceId: 'unused', backend, kind: 'connected_unused' },
      ]);
    });
  });

  it('unions all catalog chains, deduplicates sources and keeps mixed origins in normal supply ink', () => {
    const ids = Array.from({ length: 8 }, (_, index) => `model-${index}`);
    const supplied: AgentSupply = { ...agent, routes: {}, builtin_models: ids, model_supply: [] };
    const chains = Object.fromEntries(ids.map((modelId, index) => [modelChainKey('claude', modelId), readyRegion({
      ...chain('relay'), model_id: modelId, manual_override: null,
      route_origin: index === 7 ? 'automatic' as const : 'passthrough' as const,
    })]));
    expect(buildSupplyRelations([supplied], [source('relay', 'hub')], chains)).toEqual([
      { sourceId: 'relay', backend: 'claude', kind: 'gateway' },
    ]);
  });

  it('does not infer inherited relations from defaults, stale reads or another model identity', () => {
    const supplied: AgentSupply = { ...agent, routes: {} };
    const inherited: AgentChain = { ...chain('relay'), manual_override: null, route_origin: 'automatic' };
    const key = modelChainKey('claude', 'model-a');
    const sources = [source('relay', 'hub')];
    for (const read of [loadingRegion<AgentChain>(), unreadRegion<AgentChain>(),
      degradedRegion(inherited, 'refreshing', false), degradedRegion(inherited, 'read_failed', true),
      readyRegion({ ...inherited, backend: 'codex' as const }), readyRegion({ ...inherited, model_id: 'other' }),
      readyRegion({ ...inherited, current: null, chain: [], route_origin: null })]) {
      expect(buildSupplyRelations([supplied], sources, { [key]: read })).toEqual([]);
    }
    expect(buildRelations([supplied], sources, { [key]: readyRegion(inherited) }, null)).toEqual([]);
  });

  it('does not draw dormant overrides or stale chain entries outside the canonical catalog', () => {
    const supplied: AgentSupply = { ...agent, catalog_models: [] };
    expect(buildSupplyRelations([supplied], [source('relay', 'hub')], {
      [modelChainKey('claude', 'model-a')]: readyRegion(chain('relay')),
    })).toEqual([]);
  });

  it('keeps paused and takeover state ahead of passthrough ink', () => {
    const supplied: AgentSupply = { ...agent, routes: {} };
    const projection: AgentChain = { ...chain('relay', 'cooldown', false), manual_override: null, route_origin: 'passthrough',
      chain: chain('relay', 'cooldown', false).chain.map((hop) => ({ ...hop, channel: 'hub' })) };
    const relations = buildSupplyRelations([supplied], [source('native', 'hub'), source('relay', 'hub')], {
      [modelChainKey('claude', 'model-a')]: readyRegion(projection),
    });
    expect(relations).toEqual([
      { sourceId: 'native', backend: 'claude', kind: 'unavailable' },
      { sourceId: 'relay', backend: 'claude', kind: 'takeover' },
    ]);
  });

  it('uses a fresh empty chain over old manual membership and retains only known membership while unread', () => {
    const key = modelChainKey('claude', 'model-a');
    const sources = [source('relay', 'hub')];
    expect(buildSupplyRelations([agent], sources, { [key]: unreadRegion() })).toEqual([
      { sourceId: 'relay', backend: 'claude', kind: 'connected_unused' },
    ]);
    expect(buildSupplyRelations([agent], sources, { [key]: readyRegion({ ...chain('relay'), chain: [], current: null,
      manual_override: null, route_origin: null }) })).toEqual([]);
  });

  it('derives relation ink from configured routes and the exact current hop', () => {
    const sources = [source('native', 'native_cli'), source('relay', 'hub'), source('unused', 'hub')];
    const key = modelChainKey('claude', 'model-a');
    expect(buildSupplyRelations([agent], sources, { [key]: readyRegion(chain('native')) })).toEqual([
      { sourceId: 'native', backend: 'claude', kind: 'native' },
      { sourceId: 'relay', backend: 'claude', kind: 'connected_unused' },
      { sourceId: 'unused', backend: 'claude', kind: 'connected_unused' },
    ]);
    expect(buildSupplyRelations([agent], sources, { [key]: readyRegion(chain('relay', 'cooldown', false)) })[1]).toEqual(
      { sourceId: 'relay', backend: 'claude', kind: 'takeover' },
    );
    expect(buildSupplyRelations([agent], sources, { [key]: readyRegion(chain('relay', 'backoff', false, 'models.source.backoff.connection_failed')) })[1]).toEqual(
      { sourceId: 'relay', backend: 'claude', kind: 'takeover' },
    );
    expect(buildSupplyRelations([agent], sources, { [key]: readyRegion(chain('relay', 'cooldown', false, 'native_cli_unavailable')) })[1]).toEqual(
      { sourceId: 'relay', backend: 'claude', kind: 'gateway' },
    );
    for (const health of ['needs_action', 'error'] as const) {
      const reason = health === 'needs_action'
        ? 'models.source.needs_action.oauth_expired' as const
        : 'models.source.error.unclassified' as const;
      const relations = buildSupplyRelations([agent], sources, { [key]: readyRegion(chain('relay', health, false, reason)) });
      expect(relations[1]).toEqual(
        { sourceId: 'relay', backend: 'claude', kind: 'gateway' },
      );
      expect(relations[0]).toEqual({ sourceId: 'native', backend: 'claude', kind: 'unavailable' });
    }
    expect(buildSupplyRelations([agent], sources, { [key]: readyRegion(chain('relay')) })[0]).toEqual(
      { sourceId: 'native', backend: 'claude', kind: 'connected_unused' },
    );
    const staleCooldown = [source('native', 'native_cli'), source('relay', 'hub', 'cooldown'), source('unused', 'hub')];
    expect(buildSupplyRelations([agent], staleCooldown, { [key]: readyRegion(chain('relay')) })[1]).toEqual(
      { sourceId: 'relay', backend: 'claude', kind: 'gateway' },
    );
  });

  it('renders cooling relations as unavailable and draws no relations for direct backends', () => {
    const sources = [source('native', 'native_cli', 'cooldown'), source('relay', 'hub'), source('unused', 'hub')];
    expect(buildSupplyRelations([agent], sources, {} )[0]?.kind).toBe('unavailable');
    expect(buildSupplyRelations([{ ...agent, mode: 'direct', routes: null, sources: null }], sources, {})).toEqual([]);
    const stopped = { ...runtime, status: { ...runtime.status, health: 'not_started' as const } };
    expect(buildRelations([agent], sources, {}, freshRuntimeProjection(readyRegion(stopped)))).toEqual([]);
  });

  it('matches current and takeover relations by the exact mapped hop identity', () => {
    const mappedAgent: AgentSupply = {
      ...agent,
      routes: {
        'custom/model-a': {
          hops: [
            { source_id: 'native', model_id: 'model-a' },
            { source_id: 'relay', model_id: 'model-a' },
          ],
        },
      },
    };
    const mappedChain: AgentChain = {
      ...chain('relay', 'cooldown', false),
      model_id: 'custom/model-a',
    };

    expect(buildSupplyRelations(
      [mappedAgent],
      [source('native', 'native_cli'), source('relay', 'hub')],
      { [modelChainKey('claude', 'custom/model-a')]: readyRegion(mappedChain) },
    )).toContainEqual({ sourceId: 'relay', backend: 'claude', kind: 'takeover' });
  });
});
