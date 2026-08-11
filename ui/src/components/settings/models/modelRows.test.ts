import { describe, expect, it } from 'vitest';

import { collapsedModelRows, listedModelIds, modelChainKey, modelChainRequests, NOMINAL_MODEL_BASELINE } from './modelRows';
import type { AgentSupply } from './types';

const agent: AgentSupply = {
  backend: 'claude', mode: 'hub', menu_kind: 'fixed', sources: { order: [], eligibility: [] },
  routes: { 'route-only': { hops: [] } }, builtin_models: ['builtin'], model_supply: [{ model_id: 'builtin', chain_length: 0 }],
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
    model_supply: menu.map((modelId, index) => ({ model_id: modelId, chain_length: index < count ? 0 : 1 })),
  });

  it.each([0, 2, 5, menu.length])('keeps every unsupplied row plus the nominal baseline (%i unsupplied)', (unsupplied) => {
    const result = collapsedModelRows(withEmptyRoutes(unsupplied));
    const nominalCount = Math.min(NOMINAL_MODEL_BASELINE, menu.length - unsupplied);

    expect(result.visible).toHaveLength(unsupplied + nominalCount);
    expect(result.hidden).toHaveLength(menu.length - unsupplied - nominalCount);
    expect(result.visible).toEqual(menu.filter((_, index) => index < unsupplied + nominalCount));
  });

  it('preserves the backend menu order and reveals every row when expanded', () => {
    expect(collapsedModelRows(withEmptyRoutes(2), true)).toEqual({ visible: menu, hidden: [] });
  });

  it('treats direct rows as nominal because direct mode has no model_supply projection', () => {
    const result = collapsedModelRows({ ...withEmptyRoutes(0), mode: 'direct', model_supply: null });

    expect(result.visible).toEqual(menu.slice(0, NOMINAL_MODEL_BASELINE));
  });
});
