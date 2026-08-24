import { describe, expect, it } from 'vitest';

import { COLLAPSED_MODEL_LIMIT, collapsedModelRows, listedModelIds, modelChainKey, modelChainRequests, modelSupplyState } from './modelRows';
import type { AgentSupply } from './types';

const agent: AgentSupply = {
  backend: 'claude', cli_present: true, mode: 'hub', menu_kind: 'fixed', sources: { order: [], eligibility: [] },
  routes: { 'route-only': { hops: [] } }, builtin_models: ['builtin'], model_supply: [{ model_id: 'builtin', chain_length: 0, has_runnable_hop: false }],
  selected_model_id: null, selected_model_explicit: false, supply_status: null, named_agents: [], menu: null,
};

describe('modelRows', () => {
  it('lists backend menu models and every persisted Route row exactly once', () => {
    expect(listedModelIds(agent)).toEqual(['builtin', 'route-only']);
  });

  it('requests chains only for hub model rows', () => {
    expect(modelChainRequests([agent])).toEqual([
      { backend: 'claude', modelId: 'builtin' },
      { backend: 'claude', modelId: 'route-only' },
    ]);
    expect(modelChainKey('claude', 'builtin')).toBe('claude\u0000builtin');
  });

});

describe('collapsedModelRows', () => {
  const menu = Array.from({ length: 12 }, (_, index) => `model-${index + 1}`);
  const withEmptyRoutes = (count: number): AgentSupply => ({
    ...agent,
    builtin_models: menu,
    routes: {},
    model_supply: menu.map((modelId, index) => ({ model_id: modelId, chain_length: index < count ? 0 : 1, has_runnable_hop: index >= count })),
  });

  it.each([0, 2, 5, menu.length])('shows at most the first six rows regardless of supply state (%i unsupplied)', (unsupplied) => {
    expect(collapsedModelRows(withEmptyRoutes(unsupplied))).toEqual({
      visible: menu.slice(0, COLLAPSED_MODEL_LIMIT),
      hidden: menu.slice(COLLAPSED_MODEL_LIMIT),
    });
  });

  it('preserves the backend menu order and reveals every row when expanded', () => {
    expect(collapsedModelRows(withEmptyRoutes(2), true)).toEqual({ visible: menu, hidden: [] });
  });

  it('keeps a nonempty but wholly unrunnable Route classified while folding it beyond the limit', () => {
    const paused = {
      ...withEmptyRoutes(0),
      model_supply: menu.map((modelId, index) => ({
        model_id: modelId,
        chain_length: 1,
        has_runnable_hop: index !== menu.length - 1,
      })),
    };

    expect(modelSupplyState(paused, menu.at(-1) as string)).toBe('paused');
    expect(collapsedModelRows(paused).hidden).toContain(menu.at(-1));
  });

  it('classifies structural emptiness before the forced unrunnable reading', () => {
    expect(modelSupplyState(withEmptyRoutes(1), menu[0])).toBe('unconfigured');
  });

  it('uses the same fixed row limit for direct projections', () => {
    const result = collapsedModelRows({ ...withEmptyRoutes(0), mode: 'direct', model_supply: null });

    expect(result.visible).toEqual(menu.slice(0, COLLAPSED_MODEL_LIMIT));
  });
});
