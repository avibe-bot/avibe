import { describe, expect, it } from 'vitest';

import {
  initialSubscriptionChannel,
  nativeSubscriptionSlotTaken,
  recommendedSubscriptionChannel,
  subscriptionOptionOrder,
} from './subscriptionOptions';
import type { Source } from './types';

const subscription = (over: Partial<Source> = {}): Source => ({
  id: 'src_subscription',
  last_discovered_at: null,
  kind: 'subscription',
  vendor: 'anthropic',
  display_name: 'Subscription',
  protocol: 'anthropic',
  supply_channel: 'native_cli',
  billing: 'monthly',
  state: { status: 'standby' },
  models: [],
  ...over,
});

describe('add-subscription channel choice', () => {
  it('flips the recommendation and visible order by vendor', () => {
    expect(recommendedSubscriptionChannel('anthropic')).toBe('native_cli');
    expect(subscriptionOptionOrder('anthropic')).toEqual(['native_cli', 'hub']);
    expect(recommendedSubscriptionChannel('openai')).toBe('hub');
    expect(subscriptionOptionOrder('openai')).toEqual(['hub', 'native_cli']);
  });

  it('uses the recommended option while the native slot is free', () => {
    expect(initialSubscriptionChannel('anthropic', [])).toBe('native_cli');
    expect(initialSubscriptionChannel('openai', [])).toBe('hub');
  });

  it('keeps an occupied native row visible but selects the gateway option', () => {
    const sources = [subscription()];
    expect(nativeSubscriptionSlotTaken('anthropic', sources)).toBe(true);
    expect(initialSubscriptionChannel('anthropic', sources)).toBe('hub');
  });

  it('does not let another vendor or a gateway-held source occupy the slot', () => {
    const sources = [
      subscription({ vendor: 'openai' }),
      subscription({ id: 'src_hub', supply_channel: 'hub' }),
    ];
    expect(nativeSubscriptionSlotTaken('anthropic', sources)).toBe(false);
  });
});
