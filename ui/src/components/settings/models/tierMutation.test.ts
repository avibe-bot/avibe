import { describe, expect, it } from 'vitest';

import { createPendingWrites } from './asyncLifetime';
import { managedTierSource, MANAGED_TIER_SOURCES, tierMutationPayload, type TierMutationIntent } from './tierMutation';
import type { ReasoningEffortsSource, Source, SuppliedModel } from './types';

const source = (tiers: string[], provenance?: ReasoningEffortsSource | null): Source => ({
  id: 'source-a',
  last_discovered_at: null,
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: 'Source A',
  protocol: 'anthropic',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [{
    id: 'model-a',
    display_name: null,
    origin: 'manual',
    reasoning_efforts: tiers,
    // `undefined` here means the key is absent, which is the payload a server
    // that predates the field sends and the parser is expected to tolerate. The
    // v8 type has no shape for that, so the omission is asserted at the one key
    // it concerns rather than by loosening the whole fixture.
    ...(provenance === undefined
      ? ({} as Pick<SuppliedModel, 'reasoning_efforts_source'>)
      : { reasoning_efforts_source: provenance }),
  }],
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

  it.each(MANAGED_TIER_SOURCES)('builds no payload at all for a %s-declared model', (provenance) => {
    // Structural, not an affordance the editor has to remember to hide: a
    // refresh can land the rung while the editor is open, and this reads the
    // freshest Source the write is about to be sent against.
    const latest = source(['low', 'medium'], provenance);
    expect(tierMutationPayload(latest, 'model-a', { kind: 'add', tier: 'high' })).toBeNull();
    expect(tierMutationPayload(latest, 'model-a', { kind: 'remove', tier: 'low' })).toBeNull();
  });

  it.each([
    ['a user-declared list', 'user' as const],
    ['an explicitly unclaimed list', null],
    ['a server that predates the field', undefined],
  ])('still writes for %s', (_case, provenance) => {
    expect(tierMutationPayload(source(['low'], provenance), 'model-a', { kind: 'add', tier: 'high' }))
      .toEqual({ previous: ['low'], next: ['low', 'high'] });
  });
});

describe('managedTierSource', () => {
  it.each(MANAGED_TIER_SOURCES)('reports %s as the rung that locks the row', (provenance) => {
    expect(managedTierSource(provenance)).toBe(provenance);
  });

  it.each([
    ['user', 'user' as const],
    ['null', null],
    ['absent', undefined],
  ])('leaves %s editable', (_case, provenance) => {
    expect(managedTierSource(provenance)).toBeNull();
  });

  it('degrades a rung this build has never heard of to editable', () => {
    // Locking here would put the row behind a badge with no label and a rule
    // this build cannot state. Offering the edit and rendering the server's
    // refusal reaches the same outcome without the unverified claim.
    expect(managedTierSource('provider_pinned' as ReasoningEffortsSource)).toBeNull();
  });
});
