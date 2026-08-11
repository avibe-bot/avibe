import { describe, expect, it } from 'vitest';

import { combineSourceOrderReads } from './sourceOrderComposition';
import type { AgentSupply, Source } from './types';

const source = (id: string): Source => ({
  id,
  last_discovered_at: null,
  kind: 'api_key',
  vendor: 'custom',
  display_name: id,
  protocol: 'openai_chat',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [],
});

const agent = (order: string[]): AgentSupply => ({
  backend: 'claude',
  cli_present: true,
  mode: 'hub',
  menu_kind: 'fixed',
  sources: { order, eligibility: order.map((sourceId) => ({ source_id: sourceId, eligible: true })) },
});

describe('combineSourceOrderReads', () => {
  it('detects an Agent projection newer than the Source inventory', () => {
    const composition = combineSourceOrderReads(agent(['src_a', 'src_new']), [source('src_a')]);

    expect(composition.missingOrderedIds).toEqual(['src_new']);
    expect(composition.missingInventoryIds).toEqual([]);
    expect(composition.hasHole).toBe(true);
  });

  it('detects a Source inventory newer than the Agent projection', () => {
    const composition = combineSourceOrderReads(agent(['src_a']), [source('src_a'), source('src_new')]);

    expect(composition.missingOrderedIds).toEqual([]);
    expect(composition.missingInventoryIds).toEqual(['src_new']);
    expect(composition.hasHole).toBe(true);
  });

  it('partitions a consistent eligible inventory without a hole', () => {
    const composition = combineSourceOrderReads(agent(['src_a']), [source('src_a')]);
    expect(composition.missingOrderedIds).toEqual([]);
    expect(composition.missingInventoryIds).toEqual([]);
    expect(composition.hasHole).toBe(false);
    expect(composition.available.map(({ id }) => id)).toEqual(['src_a']);
  });
});
