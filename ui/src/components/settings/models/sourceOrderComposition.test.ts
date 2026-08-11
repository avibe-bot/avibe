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
  mode: 'hub',
  menu_kind: 'fixed',
  sources: { order, eligibility: order.map((sourceId) => ({ source_id: sourceId, eligible: true })) },
});

describe('combineSourceOrderReads', () => {
  it('treats a create-window mismatch as a composition hole', () => {
    expect(combineSourceOrderReads(agent(['src_a', 'src_new']), [source('src_a')]).missingOrderedIds)
      .toEqual(['src_new']);
  });

  it('treats a delete-window mismatch as a composition hole', () => {
    expect(combineSourceOrderReads(agent(['src_a', 'src_deleted']), [source('src_a')]).missingOrderedIds)
      .toEqual(['src_deleted']);
  });

  it('partitions a consistent eligible inventory without a hole', () => {
    const composition = combineSourceOrderReads(agent(['src_a']), [source('src_a')]);
    expect(composition.missingOrderedIds).toEqual([]);
    expect(composition.available.map(({ id }) => id)).toEqual(['src_a']);
  });
});
