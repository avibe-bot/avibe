import { describe, expect, it, vi } from 'vitest';

import {
  applyOpenCodeMenuIntent,
  openCodeMenuIntent,
  readOpenCodeMenuBaseline,
  sameOpenCodeMenu,
} from './menuBaseline';
import type { AgentSupply, Source } from './types';

const source = (id: string): Source => ({
  id,
  last_discovered_at: null,
  kind: 'api_key',
  vendor: 'openai',
  display_name: id,
  protocol: 'openai_responses',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [],
});

const agent = (sourceIds: string[]): AgentSupply => ({
  backend: 'opencode',
  cli_present: true,
  mode: 'hub',
  menu_kind: 'open',
  sources: {
    order: sourceIds,
    eligibility: sourceIds.map((sourceId) => ({ source_id: sourceId, eligible: true })),
  },
  menu: { view: 'featured', checked: [] },
  routes: {},
  standard_vendors: ['openai'],
});

describe('readOpenCodeMenuBaseline', () => {
  it('regroups once when concurrent reads expose a source-membership hole', async () => {
    const getAgentSources = vi.fn()
      .mockResolvedValueOnce(agent(['src_a', 'src_new']))
      .mockResolvedValueOnce(agent(['src_a', 'src_new']));
    const readValue = vi.fn()
      .mockResolvedValueOnce([source('src_a')])
      .mockResolvedValueOnce([source('src_a'), source('src_new')]);

    const baseline = await readOpenCodeMenuBaseline({ getAgentSources }, { readValue });

    expect(baseline.sources.map(({ id }) => id)).toEqual(['src_a', 'src_new']);
    expect(getAgentSources).toHaveBeenCalledTimes(2);
    expect(readValue).toHaveBeenCalledTimes(2);
  });

  it('rejects a pair that still has a composition hole after regrouping', async () => {
    const getAgentSources = vi.fn().mockResolvedValue(agent(['src_a', 'src_new']));
    const readValue = vi.fn().mockResolvedValue([source('src_a')]);

    await expect(readOpenCodeMenuBaseline({ getAgentSources }, { readValue })).rejects.toThrow(
      'did not converge',
    );
  });
});

describe('OpenCode menu intent', () => {
  it('preserves unrelated current selections and ignores additions no longer selectable', () => {
    const intent = openCodeMenuIntent(
      ['openai/kept', 'openai/removed'],
      ['openai/kept', 'openai/added', 'openai/gone'],
    );

    expect(applyOpenCodeMenuIntent(
      ['openai/concurrent', 'openai/kept', 'openai/removed'],
      intent,
      new Set(['openai/added']),
    )).toEqual(['openai/concurrent', 'openai/kept', 'openai/added']);
  });

  it('compares the server menu as an ordered total value', () => {
    const expected = { view: 'featured' as const, checked: ['openai/a', 'openai/b'] };
    expect(sameOpenCodeMenu(expected, expected)).toBe(true);
    expect(sameOpenCodeMenu({ ...expected, checked: ['openai/b', 'openai/a'] }, expected)).toBe(false);
    expect(sameOpenCodeMenu(null, expected)).toBe(false);
  });
});
