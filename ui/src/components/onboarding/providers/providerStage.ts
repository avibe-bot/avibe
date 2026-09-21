// What the provider stage is showing, derived from what the server said.
//
// Kept pure and separate from the screen because these are the decisions the
// screen is easy to get wrong and hard to inspect once it renders: which two
// brands occupy the stage, whether a card is a fact or an offer, what the
// summary sentence counts, and which of the action's states the footer button
// is in. None of it needs React, so none of it is written in React.
//
// Identity is the catalog vendor id throughout. A migration row calls Gemini
// `google` and Qwen `alibaba-cn`; a source calls them what it was created with.
// Resolving both through `providerIdentity` is what stops the same provider
// occupying two slots.
import {
  groupMigrationCandidates,
  groupSelectable,
} from '@/components/settings/models/migrationGrouping';
import { isImportableKey } from '@/components/settings/models/migrationScan';
import type {
  AgentBackend,
  MigrationScan,
  Source,
} from '@/components/settings/models/types';
import {
  SETUP_PRIMARY_VENDORS,
  providerBrandLabel,
  providerVendorId,
  setupPrimaryRank,
} from '@/components/settings/providers/providerIdentity';
import type { TranslationKey } from '@/i18n/types';
import type { SetupAction } from '../setupFlow';

/** The stage draws three cards; the third is always the way to add more. */
export const PROVIDER_SLOT_COUNT = 2;

export type ProviderSlotKind =
  /** A source already exists. A fact, not a choice. */
  | 'connected'
  /** A scan found credentials Model Hub could take over, pending consent. */
  | 'detected'
  /** Nothing is known yet; the card offers to add this brand. */
  | 'empty';

export type ProviderSlot = {
  /** Catalog vendor id — the stable identity for artwork, label and selection. */
  vendor: string;
  label: string;
  kind: ProviderSlotKind;
  /** Masked credential for the card's second line; never secret material. */
  mask: string | null;
  /**
   * The backends whose consent this card carries, empty unless detected.
   * One entry per consent group the card's credentials sit in — the migration
   * feature expands each to its linked group when the card is toggled, so this
   * stays the entry point rather than the closure.
   */
  backends: AgentBackend[];
};

const byPrimaryRank = (left: ProviderSlot, right: ProviderSlot): number => {
  const leftRank = setupPrimaryRank(left.vendor);
  const rightRank = setupPrimaryRank(right.vendor);
  if (leftRank === rightRank) return 0;
  if (leftRank === null) return 1;
  if (rightRank === null) return -1;
  return leftRank - rightRank;
};

/**
 * Providers a scan found that setup could take over.
 *
 * Only rows inside a group this entry point can actually consent to: a linked
 * group holding a subscription sign-in or a blocker is reviewed in Settings,
 * not offered a card here. A row whose provider the server did not name has no
 * brand slot to fill — it still appears in the import dialog, where the masked
 * detail identifies it.
 */
function detectedProviders(scan: MigrationScan | null): ProviderSlot[] {
  if (!scan) return [];
  const byVendor = new Map<string, ProviderSlot>();
  for (const group of groupMigrationCandidates(scan.items, isImportableKey)) {
    if (!groupSelectable(group)) continue;
    for (const row of group.importRows) {
      const named = row.vendor?.trim();
      if (!named) continue;
      const vendor = providerVendorId(named);
      const existing = byVendor.get(vendor);
      if (existing) {
        if (!existing.backends.includes(group.backend)) existing.backends.push(group.backend);
        continue;
      }
      byVendor.set(vendor, {
        vendor,
        label: providerBrandLabel(named, row.display_name),
        kind: 'detected',
        mask: row.masked_credential?.trim() || null,
        backends: [group.backend],
      });
    }
  }
  return [...byVendor.values()].sort(byPrimaryRank);
}

/**
 * The two provider cards, most settled first.
 *
 * Already-added sources come first because they are facts; detected candidates
 * follow; and when the two together do not fill the stage, the named brands the
 * shortlist opens with stand in as offers. A stage that drew one card would read
 * as a list of one rather than as providers feeding a hub.
 */
export function providerSlots(input: {
  sources: readonly Source[];
  scan: MigrationScan | null;
}): ProviderSlot[] {
  const seen = new Set<string>();
  const slots: ProviderSlot[] = [];

  for (const source of input.sources) {
    const vendor = providerVendorId(source.vendor);
    if (seen.has(vendor)) continue;
    seen.add(vendor);
    slots.push({
      vendor,
      label: providerBrandLabel(source.vendor, source.display_name),
      kind: 'connected',
      mask: source.masked_credential?.trim() || null,
      backends: [],
    });
  }

  for (const detected of detectedProviders(input.scan)) {
    if (seen.has(detected.vendor)) continue;
    seen.add(detected.vendor);
    slots.push(detected);
  }

  for (const vendor of SETUP_PRIMARY_VENDORS) {
    if (slots.length >= PROVIDER_SLOT_COUNT) break;
    if (seen.has(vendor)) continue;
    seen.add(vendor);
    slots.push({ vendor, label: providerBrandLabel(vendor), kind: 'empty', mask: null, backends: [] });
  }

  return slots.slice(0, PROVIDER_SLOT_COUNT);
}

/** Whether a detected card is currently consented to. A connected card is never
 *  "selected": its fill states a fact, so a selection toggle cannot change it. */
export const slotSelected = (
  slot: ProviderSlot,
  selected: readonly AgentBackend[],
): boolean => slot.kind === 'detected'
  && slot.backends.length > 0
  && slot.backends.every((backend) => selected.includes(backend));

export type ProviderSummary =
  | { kind: 'none' }
  | { kind: 'added'; count: number; names: string[] }
  | { kind: 'selected'; count: number; names: string[] }
  | { kind: 'error' };

/**
 * The sentence under the stage.
 *
 * Added wins over selected: once a provider is really there, what setup has is
 * no longer an intention. The added count spans every source, not just the two
 * on the stage, because a person who added five through Add more has five.
 */
export function providerSummary(input: {
  slots: readonly ProviderSlot[];
  sources: readonly Source[];
  selected: readonly AgentBackend[];
  failed: boolean;
}): ProviderSummary {
  if (input.failed) return { kind: 'error' };
  const added = new Map<string, string>();
  for (const source of input.sources) {
    const vendor = providerVendorId(source.vendor);
    if (!added.has(vendor)) added.set(vendor, providerBrandLabel(source.vendor, source.display_name));
  }
  if (added.size > 0) return { kind: 'added', count: added.size, names: [...added.values()] };
  const selected = input.slots
    .filter((slot) => slotSelected(slot, input.selected))
    .map((slot) => slot.label);
  if (selected.length > 0) return { kind: 'selected', count: selected.length, names: selected };
  return { kind: 'none' };
}

export type ProviderActionKind =
  | 'import'
  | 'retryImport'
  | 'continue'
  | 'add'
  | 'connecting'
  | 'checking';

export type ProviderActionState = { kind: ProviderActionKind; count: number };

/**
 * Which of its states the primary action is in.
 *
 * Ordered by what the person can do next rather than by what is most
 * interesting: while the engine is coming up nothing else is possible, a
 * pending take-over is the shortest path to a working provider, and continuing
 * is only offered once something is really there to continue with.
 */
export function providerAction(input: {
  /** Rows the current selection would submit in one batch. */
  pendingCount: number;
  /** A batch failed and has not been retried. */
  importFailed: boolean;
  hasSource: boolean;
  /** The engine is installing or starting. */
  gatewayBusy: boolean;
  /** A source was written and its connection is being read back. */
  verifying: boolean;
}): ProviderActionState {
  if (input.gatewayBusy) return { kind: 'connecting', count: 0 };
  if (input.verifying) return { kind: 'checking', count: 0 };
  if (input.pendingCount > 0) {
    return { kind: input.importFailed ? 'retryImport' : 'import', count: input.pendingCount };
  }
  if (input.hasSource) return { kind: 'continue', count: 0 };
  return { kind: 'add', count: 0 };
}

const ACTION_LABEL = {
  import: 'onboarding.providers.actionImport',
  retryImport: 'onboarding.providers.actionRetryImport',
  continue: 'onboarding.providers.actionContinue',
  add: 'onboarding.providers.actionAdd',
  connecting: 'onboarding.providers.actionConnecting',
  checking: 'onboarding.providers.actionChecking',
} as const satisfies Record<ProviderActionKind, TranslationKey>;

const BUSY_ACTIONS = new Set<ProviderActionKind>(['connecting', 'checking']);

/** The C2 action the shell renders. The screen never draws the button itself. */
export function providerSetupAction(state: ProviderActionState): SetupAction {
  const busy = BUSY_ACTIONS.has(state.kind);
  return {
    labelKey: ACTION_LABEL[state.kind],
    ...(state.count > 0 ? { labelArgs: { count: state.count } } : {}),
    disabled: busy,
    busy,
    icon: busy ? 'spinner' : state.kind === 'add' ? 'none' : 'arrow-right',
  };
}
