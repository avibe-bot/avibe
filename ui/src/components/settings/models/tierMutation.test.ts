import { describe, expect, it } from 'vitest';

import { createPendingWrites } from './asyncLifetime';
import { tierMutationPayload, type TierMutationIntent } from './tierMutation';
import type { Source } from './types';

const source = (tiers: string[]): Source => ({
  id: 'source-a',
  last_discovered_at: null,
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: 'Source A',
  protocol: 'anthropic',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [{ id: 'model-a', display_name: null, origin: 'manual', reasoning_efforts: tiers }],
});

describe('queued tier mutation payloads', () => {
  it('builds each full-list payload from the latest Source after dequeue', async () => {
    let latest = source(['high', 'medium', 'low']);
    const sent: string[][] = [];
    const writes = createPendingWrites(() => {});
    const enqueue = (intent: TierMutationIntent) => writes.track(latest.id, async () => {
      const payload = tierMutationPayload(latest, 'model-a', intent);
      if (!payload) throw new Error('missing model');
      sent.push(payload.next);
      latest = source(payload.next);
    });

    await Promise.all([
      enqueue({ kind: 'remove', tier: 'high' }),
      enqueue({ kind: 'remove', tier: 'low' }),
    ]);

    expect(sent).toEqual([
      ['medium', 'low'],
      ['medium'],
    ]);
  });
});
