// The subscription vendors the Web UI offers, and the custody facts about them.
//
// Two products name these vendors and neither is reachable from this file. The
// engine's `_OAUTH_ENDPOINTS` table decides which vendors a start may name, and
// the service's `_NATIVE_VENDOR_BACKENDS` route table decides which of those a
// sanctioned CLI can hold custody of; the server refuses a start nobody admits
// before any UI here could render one. The menu's job is narrower, and is the
// reason it lists at all: §1.4's whole chooser phase exists per vendor — the plan
// subtitle, which channel carries 推荐, the ToS note, the hint — and every one of
// those lines is client-owned brand copy. So a vendor appears here when the
// product offers it AND this file can say what choosing between its two channels
// means, and not before.
//
// `hub_only_subscription_vendors()` in `tests/scenario_harness/` derives the
// server's half of the same fact. The two are different questions — that one asks
// who owns custody, this one asks whether the chooser can speak for the vendor —
// so they are not the same list, and a vendor added to the engine's table is
// meant to need a decision here rather than to inherit one.
import type { Source, SupplyChannel } from './types';

/** Menu order is frame 13's order: the two subscriptions the chooser has a
 *  recommendation for, then the hub-held ones. It is not the engine table's order
 *  and not the API-key catalog's A–Z — nothing here sorts, the frame ranked them. */
export const SUBSCRIPTION_VENDORS = ['anthropic', 'openai', 'gemini', 'kimi', 'xai'] as const;
export type SubscriptionVendor = (typeof SUBSCRIPTION_VENDORS)[number];

/** The vendors §1.4's chooser phase is written for.
 *
 *  One table answers two questions that have one answer: whether a sanctioned CLI
 *  can hold this subscription natively, and whether the client has the per-vendor
 *  copy the chooser renders — subtitle, per-option consequence, ToS note, hint.
 *  They are kept together because they are only ever decided together: a vendor
 *  gains a chooser when Avibe sanctions a CLI for it and someone writes what the
 *  choice costs, and a vendor without either is hub-held and starts straight into
 *  its flow. Two tables would let them disagree, and the disagreement is not
 *  cosmetic — the gesture that allocates the provider tab lives in the menu for a
 *  vendor with no chooser and in the dialog for one with a chooser (PD-1).
 */
const SUBSCRIPTION_CHOOSER = {
  anthropic: { copy: 'claude', brand: 'Claude' },
  openai: { copy: 'chatgpt', brand: 'ChatGPT' },
} as const satisfies Record<string, { copy: SubscriptionVendorCopy; brand: string }>;

/** The copy family frame 04's chooser is written in, and the brand name its title
 *  interpolates. `null` is the answer for a hub-held vendor, and it is the reason
 *  this returns one: every line the chooser renders names a plan, a consequence of
 *  a channel, or a ToS note, and a chooser rendered for a vendor with none of them
 *  would interpolate an i18n key at the user. Returning `null` makes the type ask
 *  each consumer which case it is in. */
export type SubscriptionVendorCopy = 'claude' | 'chatgpt';

export type SubscriptionChooser = { copy: SubscriptionVendorCopy; brand: string };

export const subscriptionChooser = (vendor: string): SubscriptionChooser | null =>
  (SUBSCRIPTION_CHOOSER as Readonly<Record<string, SubscriptionChooser | undefined>>)[vendor] ?? null;

/** Whether a sanctioned CLI can hold this vendor's subscription natively, and so
 *  whether §1.4 has a channel choice to put in front of the user at all. */
export const hasNativeSubscriptionCustody = (vendor: string): boolean =>
  subscriptionChooser(vendor) !== null;

export const nativeSubscriptionSlotTaken = (vendor: string, sources: Source[]): boolean =>
  sources.some(
    (source) =>
      source.kind === 'subscription'
      && source.supply_channel === 'native_cli'
      && source.vendor === vendor,
  );

/** For a hub-held vendor the hub is not a fallback, it is the only custody there
 *  is — which is also what its start is posted with. */
export const recommendedSubscriptionChannel = (vendor: string): SupplyChannel => {
  if (!hasNativeSubscriptionCustody(vendor)) return 'hub';
  return vendor === 'openai' ? 'hub' : 'native_cli';
};

export const initialSubscriptionChannel = (vendor: string, sources: Source[]): SupplyChannel =>
  nativeSubscriptionSlotTaken(vendor, sources) ? 'hub' : recommendedSubscriptionChannel(vendor);

export const subscriptionOptionOrder = (vendor: string): SupplyChannel[] =>
  recommendedSubscriptionChannel(vendor) === 'hub'
    ? ['hub', 'native_cli']
    : ['native_cli', 'hub'];

/** Which channel the menu recommends, or `null` when the vendor has no native
 *  channel to recommend against. A hub-held row carries no badge: 网关推荐 over the
 *  only custody there is recommends nothing, and frame 13 draws a badge because
 *  both of its rows had a choice to rank. */
export const subscriptionMenuRecommendation = (
  vendor: SubscriptionVendor,
): 'native' | 'gateway' | null => {
  if (!hasNativeSubscriptionCustody(vendor)) return null;
  return recommendedSubscriptionChannel(vendor) === 'hub' ? 'gateway' : 'native';
};

/** The menu's rows: every offered vendor with the badge it earns. Derived from the
 *  vocabulary above so a vendor added there appears here with its custody already
 *  decided, rather than needing a second list kept in the same order. */
export const SUBSCRIPTION_MENU_ROWS = SUBSCRIPTION_VENDORS.map((vendor) => ({
  vendor,
  recommendation: subscriptionMenuRecommendation(vendor),
})) as ReadonlyArray<{ vendor: SubscriptionVendor; recommendation: 'native' | 'gateway' | null }>;
