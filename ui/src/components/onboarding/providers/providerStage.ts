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
import type { ParseKeys } from 'i18next';

import {
  appliableItems,
  BACKEND_ORDER,
  groupMigrationCandidates,
  groupSelectable,
  requiredBackends,
  type MigrationSelection,
} from '@/components/settings/models/migrationGrouping';
import { isImportableKey } from '@/components/settings/models/migrationScan';
import { foldRegionRead, type RegionRead } from '@/components/settings/models/regionRead';
import { installAndStartStep } from '@/components/settings/models/runtimeLifecycle';
import type {
  AgentBackend,
  AgentSupply,
  MigrationItem,
  MigrationScan,
  RuntimeDependency,
  Source,
} from '@/components/settings/models/types';
import {
  SETUP_PRIMARY_VENDORS,
  providerBrandLabel,
  providerVendorId,
  setupPrimaryRank,
} from '@/components/settings/providers/providerIdentity';
import type { TranslationKey } from '@/i18n/types';
import {
  setupCanAttemptInstall,
  setupHubRunning,
  setupNavigationReady,
  type SetupAction,
  type SetupCapability,
} from '../setupFlow';

/** The stage draws three cards; the third is always the way to add more. */
export const PROVIDER_SLOT_COUNT = 2;

/**
 * Whether a source counts as connected.
 *
 * `active` and `standby` are the two healthy statuses; the shipped resolver already
 * routes through standby, so treating it as anything less would under-report a source
 * the gateway is in fact willing to use. A source still awaiting its first successful
 * model call is kept and disclosed as unverified rather than hidden — the credential
 * exists and the person did add it.
 */
export const usableSource = (source: Source): boolean =>
  source.state.status === 'active' || source.state.status === 'standby';

/**
 * How many of the sources added through Add more are still there.
 *
 * Filtered against the live source list rather than trusted from the flow state: the
 * badge describes what exists now, and a source removed in another tab must not keep
 * inflating it.
 */
export function addedThroughMoreCount(
  addedThroughMore: readonly string[],
  sources: readonly Source[],
): number {
  const usable = new Set(sources.filter(usableSource).map((source) => source.id));
  return new Set(addedThroughMore.filter((id) => usable.has(id))).size;
}

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
   * Connected, but no successful model call has been made with this credential yet.
   *
   * `save_unverified` is deliberate policy in both hosts of the key form, so a source
   * the person just added is routinely `standby` with a pending marker. It stays
   * usable — that is what `usableSource` already says — but a card that states a
   * working connection the server has never observed is a claim nobody made. This is
   * the same distinction `sourceStatePresentation` draws as 「已保存」 rather than
   * 「供应中」 on the Settings row.
   */
  pending: boolean;
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
        pending: false,
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

  for (const source of input.sources.filter(usableSource)) {
    const vendor = providerVendorId(source.vendor);
    if (seen.has(vendor)) continue;
    seen.add(vendor);
    slots.push({
      vendor,
      label: providerBrandLabel(source.vendor, source.display_name),
      kind: 'connected',
      mask: source.masked_credential?.trim() || null,
      pending: Boolean(source.verification_pending),
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
    slots.push({ vendor, label: providerBrandLabel(vendor), kind: 'empty', mask: null, pending: false, backends: [] });
  }

  return slots.slice(0, PROVIDER_SLOT_COUNT);
}

/**
 * Detected providers the stage is not already drawing.
 *
 * This is what the add dialog's 已检测到 tab lists, and whether it has anything to
 * list is what decides the tab exists at all: a tab repeating the two cards
 * immediately behind it offers nothing, and an empty tab is worse than an absent
 * one. Sources occupy too, so a brand already connected never comes back as
 * something to review.
 */
export function unlistedDetected(input: {
  sources: readonly Source[];
  scan: MigrationScan | null;
}): ProviderSlot[] {
  const shown = new Set(providerSlots(input).map((slot) => slot.vendor));
  return detectedProviders(input.scan).filter((slot) => !shown.has(slot.vendor));
}

/** Whether a detected card is currently consented to. A connected card is never
 *  "selected": its fill states a fact, so a selection toggle cannot change it. */
export const slotSelected = (
  slot: ProviderSlot,
  selected: readonly AgentBackend[],
): boolean => slot.kind === 'detected'
  && slot.backends.length > 0
  && slot.backends.every((backend) => selected.includes(backend));

/**
 * The rows the current consent would submit, counted the way the dialog counts them.
 *
 * Deliberately the takeover's own computation rather than anything of this screen's:
 * the number the CTA promises and the number the batch sends have to be one number,
 * and the only way to guarantee that is to run the same grouping the dialog runs —
 * scope the scan, drop groups this entry point cannot consent to, then take every
 * importable row of the consented backends.
 */
export function pendingImportRows(selection: MigrationSelection): MigrationItem[] {
  const items = selection.scan?.items ?? [];
  const consented = new Set(
    groupMigrationCandidates(items, isImportableKey)
      .filter((group) => groupSelectable(group) && selection.selectedBackends.includes(group.backend))
      .map((group) => group.backend),
  );
  return appliableItems(items, consented);
}

/**
 * The keys the capsule offers to review.
 *
 * Not every importable key in the scan: a key whose consent group is blocked by an
 * OAuth row or a blocker cannot be consented to from here, so advertising it would
 * open a review with nothing to press. The offer is therefore built from the same
 * selectable groups the cards and the dialog are built from, narrowed back to keys
 * because「发现 N 个可导入的 API Key」 is what the sentence says.
 */
export function offeredImportKeys(selection: MigrationSelection): MigrationItem[] {
  const items = selection.scan?.items ?? [];
  return groupMigrationCandidates(items, isImportableKey)
    .filter(groupSelectable)
    .flatMap((group) => group.importRows)
    .filter(isImportableKey);
}

/**
 * The consent a scan arrives already carrying.
 *
 * The server marks rows `selected`, and the uncontrolled dialog has always honoured
 * that — a group whose every linked importable row is marked opens ticked. A
 * controlled caller that built its selection only from what it previously held would
 * throw those defaults away on the first scan and show a stage where nothing is
 * chosen and a capsule announcing keys to import, which is two answers to one
 * question. Seeded once, on the first scan; every later scan is reconciled instead,
 * because by then the selection is the person's rather than the server's.
 */
export function defaultSelection(scan: MigrationScan | null): AgentBackend[] {
  const items = scan?.items ?? [];
  return groupMigrationCandidates(items, isImportableKey)
    .filter((group) => groupSelectable(group) && group.linkedImportRows.every((row) => row.selected))
    .map((group) => group.backend);
}

/**
 * Consent after a card was toggled.
 *
 * A card names an entry point, not a consent group: toggling it writes its whole
 * linked closure, because migrating one backend's copy of a shared credential file
 * migrates every backend that reads it. Consent to half of that does not exist, so
 * it is never representable here.
 */
export function toggleSlotSelection(
  selection: MigrationSelection,
  slot: ProviderSlot,
): AgentBackend[] {
  const items = selection.scan?.items ?? [];
  if (slot.kind !== 'detected' || slot.backends.length === 0) return [...selection.selectedBackends];
  const next = new Set(selection.selectedBackends);
  const on = slotSelected(slot, selection.selectedBackends);
  for (const backend of requiredBackends(items, slot.backends)) {
    if (on) next.delete(backend);
    else next.add(backend);
  }
  return [...next];
}

/**
 * Consent a fresh scan still supports.
 *
 * Run whenever the scan is replaced. A backend that was consented to and has since
 * been imported, blocked, or lost its importable rows is no longer something this
 * entry point can submit — and a stale name left in the selection would keep the
 * CTA offering a batch the dialog would refuse to build.
 */
export function reconcileSelection(selection: MigrationSelection): AgentBackend[] {
  const items = selection.scan?.items ?? [];
  const selectable = new Set(
    groupMigrationCandidates(items, isImportableKey)
      .filter(groupSelectable)
      .map((group) => group.backend),
  );
  return selection.selectedBackends.filter((backend) => selectable.has(backend));
}

export type ProviderSummary =
  | { kind: 'none' }
  | { kind: 'added'; count: number; names: string[] }
  | { kind: 'selected'; count: number; names: string[] }
  | { kind: 'error' };

/**
 * The sentence under the stage.
 *
 * Added wins over selected: once a provider is really there, what setup has is
 * no longer an intention. Both counts span everything, not just the two cards on
 * the stage — a person who added five through Add more has five, and one who
 * consented to a third provider inside the add dialog consented to three. The
 * sentence describes the flow's state, and the stage is only the part of it that
 * happens to be drawn.
 */
export function providerSummary(input: {
  scan: MigrationScan | null;
  sources: readonly Source[];
  selected: readonly AgentBackend[];
  failed: boolean;
}): ProviderSummary {
  if (input.failed) return { kind: 'error' };
  const added = new Map<string, string>();
  for (const source of input.sources.filter(usableSource)) {
    const vendor = providerVendorId(source.vendor);
    if (!added.has(vendor)) added.set(vendor, providerBrandLabel(source.vendor, source.display_name));
  }
  if (added.size > 0) return { kind: 'added', count: added.size, names: [...added.values()] };
  const selected = detectedProviders(input.scan)
    .filter((slot) => slotSelected(slot, input.selected))
    .map((slot) => slot.label);
  if (selected.length > 0) return { kind: 'selected', count: selected.length, names: selected };
  return { kind: 'none' };
}

/**
 * The backend whose adoption the screen resumes, or `null` when there is none.
 *
 * `resumeGatewayAdoption` is named for one backend and returns early — without
 * touching the runtime at all — when that backend is already in hub mode. So the
 * choice is not cosmetic: naming an already-adopted backend would skip the install
 * and start this screen exists to perform. Hence the first present backend that is
 * NOT already adopted, in the shared backend order, falling back to the first
 * present one when every row is already in hub mode and there is nothing to resume.
 *
 * Presence is the server's `cli_present`, read from a refreshed collection rather
 * than a cached list: a CLI installed while setup was open is exactly the case a
 * cached read would get wrong.
 */
export function adoptionBackend(agents: readonly AgentSupply[]): AgentBackend | null {
  const present = BACKEND_ORDER.filter((backend) =>
    agents.some((agent) => agent.backend === backend && agent.cli_present));
  const unadopted = present.find((backend) =>
    agents.find((agent) => agent.backend === backend)?.mode !== 'hub');
  return unadopted ?? present[0] ?? null;
}

export type GatewayIntent =
  /** Already serving. Nothing to do, and nothing to say beyond that. */
  | { kind: 'running' }
  /** No authoritative answer yet, or one that authorizes nothing. The shell owns
   *  the read and its recovery; the card waits rather than inventing a verdict. */
  | { kind: 'waiting' }
  /** The engine is missing or stopped and this screen may resume it. */
  | { kind: 'resume'; step: 'install' | 'start' }
  /** Installing here is not something this deployment can do. */
  | { kind: 'unsupported' };

/**
 * What the screen should do about the engine, from the read the shell handed down.
 *
 * Two rules the ordering encodes. Installing and starting are admitted separately:
 * `setupCanAttemptInstall` gates fresh installation only, so a Hub that is installed
 * but stopped still starts on a host where new installation is unsupported —
 * refusing to start it would strand a working engine on a technicality. And nothing
 * outside `ready` authorizes anything: a loading, unread or degraded read is not
 * evidence the engine is absent, and acting on it would install over a Hub that is
 * merely unreachable this second.
 *
 * Controller startup already ran the first recovery, which is why this resumes only
 * what that recovery demonstrably left undone rather than installing unconditionally.
 */
export function gatewayIntent(input: {
  capability: SetupCapability;
  gatewayEnabled: boolean | null;
  runtimeRead: RegionRead<RuntimeDependency>;
}): GatewayIntent {
  if (setupHubRunning(input.runtimeRead)) return { kind: 'running' };
  if (!setupNavigationReady(input.capability, input.gatewayEnabled)) return { kind: 'waiting' };
  const runtime = foldRegionRead(input.runtimeRead, {
    loading: () => null,
    unread: () => null,
    degraded: () => null,
    ready: (value: RuntimeDependency) => value,
  });
  // A runtime whose own persisted intent is off is a configuration boundary, and
  // the flow's one rule about those is that setup never silently re-enables them.
  if (!runtime || runtime.enabled === false) return { kind: 'waiting' };
  const step = installAndStartStep(runtime);
  if (step === 'complete') return { kind: 'running' };
  if (step === 'start') return { kind: 'resume', step: 'start' };
  return setupCanAttemptInstall(input.capability, input.gatewayEnabled, input.runtimeRead)
    ? { kind: 'resume', step: 'install' }
    : { kind: 'unsupported' };
}

export type ProviderActionKind =
  | 'import'
  | 'retryImport'
  | 'continue'
  | 'add'
  | 'connecting'
  | 'checking';

export type ProviderActionState = {
  kind: ProviderActionKind;
  count: number;
  /** The state is the right one to show and the wrong one to press. Distinct from a
   *  busy state, which is also unpressable but is going somewhere on its own. */
  blocked?: boolean;
};

/**
 * Which of its states the primary action is in.
 *
 * Ordered by what the person can do next rather than by what is most
 * interesting: while the engine is coming up nothing else is possible, a
 * pending take-over is the shortest path to a working provider, and continuing
 * is only offered once something is really there to continue with.
 *
 * Continuing additionally needs the engine to be serving. C4 says as much —
 * `setupCandidateAllowed` requires `setupHubRunning` — and the reason is not
 * procedural: the next screen picks a model per assistant out of what the Hub
 * supplies, so arriving there with a source and a stopped, failed or unsupported
 * engine is arriving at an empty screen with no way to tell why. The action keeps
 * saying 「继续」 and stops being pressable; what went wrong is on the gateway card,
 * with the recovery next to it.
 */
export function providerAction(input: {
  /** Rows the current selection would submit in one batch. */
  pendingCount: number;
  /** A batch failed and has not been retried. */
  importFailed: boolean;
  hasSource: boolean;
  /** The engine is installing or starting. */
  gatewayBusy: boolean;
  /** The engine is up and serving — the authoritative read, not the attempt. */
  gatewayRunning: boolean;
  /** A source was written and its connection is being read back. */
  verifying: boolean;
}): ProviderActionState {
  if (input.gatewayBusy) return { kind: 'connecting', count: 0 };
  if (input.verifying) return { kind: 'checking', count: 0 };
  if (input.pendingCount > 0) {
    return { kind: input.importFailed ? 'retryImport' : 'import', count: input.pendingCount };
  }
  if (input.hasSource) return { kind: 'continue', count: 0, blocked: !input.gatewayRunning };
  return { kind: 'add', count: 0 };
}

/** A plural family, named by the base `t` resolves it under. */
type PluralBase<Key> = Key extends `${infer Base}_other` ? Base : never;

/**
 * The label per action state — and the one place C1 and C2 do not meet.
 *
 * `TranslationKey` is text leaves that resolve *without* count (its own note says
 * so); a family shipped only as `_one`/`_other` is not one, even though
 * `t(base, { count })` resolves it. C1 ships both counted action labels as exactly
 * such families and C2 types `SetupAction.labelKey` as `TranslationKey` — so the
 * contract's copy cannot be named by the contract's type. Widening `labelKey` to
 * accept a plural base is the edit that removes the assertion below; it is
 * `setupFlow.ts`, so it is reported rather than made here.
 *
 * What the assertion gives up is checked back in `actionLabelResolves`: every key
 * in this table has to resolve in both shipped bundles, which is the guarantee
 * `TranslationKey` was providing and the only one that was ever at stake.
 */
export const ACTION_LABEL = {
  import: 'onboarding.providers.actionImport',
  retryImport: 'onboarding.providers.actionRetryImport',
  continue: 'onboarding.providers.actionContinue',
  add: 'onboarding.providers.actionAdd',
  connecting: 'onboarding.providers.actionConnecting',
  checking: 'onboarding.providers.actionChecking',
} as const satisfies Record<ProviderActionKind, TranslationKey | PluralBase<ParseKeys>>;

const BUSY_ACTIONS = new Set<ProviderActionKind>(['connecting', 'checking']);

/** The C2 action the shell renders. The screen never draws the button itself. */
export function providerSetupAction(state: ProviderActionState): SetupAction {
  const busy = BUSY_ACTIONS.has(state.kind);
  return {
    labelKey: ACTION_LABEL[state.kind] as TranslationKey,
    ...(state.count > 0 ? { labelArgs: { count: state.count } } : {}),
    disabled: busy || state.blocked === true,
    busy,
    icon: busy ? 'spinner' : state.kind === 'add' ? 'none' : 'arrow-right',
  };
}
