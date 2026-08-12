import type { Source, SupplyChannel } from './types';

export type SubscriptionVendorCopy = 'claude' | 'chatgpt';

export const subscriptionVendorCopy = (vendor: string): SubscriptionVendorCopy =>
  vendor === 'openai' ? 'chatgpt' : 'claude';

export const nativeSubscriptionSlotTaken = (vendor: string, sources: Source[]): boolean =>
  sources.some(
    (source) =>
      source.kind === 'subscription'
      && source.supply_channel === 'native_cli'
      && source.vendor === vendor,
  );

export const recommendedSubscriptionChannel = (vendor: string): SupplyChannel =>
  vendor === 'openai' ? 'hub' : 'native_cli';

export const initialSubscriptionChannel = (vendor: string, sources: Source[]): SupplyChannel =>
  nativeSubscriptionSlotTaken(vendor, sources) ? 'hub' : recommendedSubscriptionChannel(vendor);

export const subscriptionOptionOrder = (vendor: string): SupplyChannel[] =>
  recommendedSubscriptionChannel(vendor) === 'hub'
    ? ['hub', 'native_cli']
    : ['native_cli', 'hub'];
