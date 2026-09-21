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
  appliableItems,
  BACKEND_ORDER,
  blockedReasonKey,
  groupMigrationCandidates,
  groupSelectable,
  requiredBackends,
  takeableImportRows,
  type MigrationGroup,
  type MigrationSelection,
} from '@/components/settings/models/migrationGrouping';
import { isImportableKey } from '@/components/settings/models/migrationScan';
import { foldRegionRead, regionFailed, type RegionRead } from '@/components/settings/models/regionRead';
import { installAndStartStep } from '@/components/settings/models/runtimeLifecycle';
import type {
  AgentBackend,
  AgentSupply,
  MigrationItem,
  MigrationScan,
  RuntimeDependency,
  Source,
  SourceKind,
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
  /**
   * The card's stable identity — for the slot it occupies, its React key and its
   * `data-provider`. The catalog vendor id whenever there is one, and a detected
   * row's own id when the server named no provider for it: that row is still one
   * credential and still needs one slot of its own.
   */
  vendor: string;
  /**
   * The catalog brand whose mark and name this card wears, `null` for a detected
   * credential the server named no provider for. Distinct from `vendor` for exactly
   * that case: an unnamed credential has an identity but no brand, and drawing one
   * from its backend or its key prefix would be a guess — the same guess the import
   * dialog refuses to make, where such a row is identified by its masked detail.
   */
  brand: string | null;
  label: string;
  kind: ProviderSlotKind;
  /**
   * The card's second line when there is a credential to describe: the separate
   * mask when the server sends one, and for a detected row the masked detail it has
   * always carried when it does not. Never secret material.
   */
  mask: string | null;
  /**
   * How a connected source arrives, and whose sign-in it is. `null` for anything
   * that is not connected.
   *
   * A subscription has no mask by construction — there is no key to mask — so a card
   * that described itself by its mask alone would fall through to the offer an EMPTY
   * card makes and invite a key for a provider already connected. What it says
   * instead is what Settings says about the same source.
   */
  supply: { kind: SourceKind; account: string | null } | null;
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
   *
   * Only groups this entry point can actually take over. A card whose credential
   * sits in a blocked group carries none, which is what makes it unselectable
   * everywhere at once — and a brand holding one blocked group beside an unrelated
   * selectable one carries only the selectable one, because consent to the second
   * was never consent to the first.
   */
  backends: AgentBackend[];
  /**
   * Why this card's credentials cannot be taken over from here, empty when they
   * can. The migration feature's own reasons, deduped, so the card and the review
   * explain one blocked group with one sentence.
   */
  reasons: TranslationKey[];
};

/**
 * Every group the scan offers setup, on setup's terms.
 *
 * Both scopes are the same predicate, and that is the point: setup opens from
 * importable API keys and may take over nothing else. A backend that also carries an
 * importable subscription store therefore appears — its key is right there, and a
 * screen that silently dropped it would leave a detected credential unexplained —
 * but blocked, for review in Settings. The server migrates a backend whole and
 * refuses a batch that omits any of its rows, so there is no half of it to take.
 *
 * One helper rather than the call spelled out five times, so the cards, the capsule's
 * count, the CTA's batch, the seeded consent and the reconciliation cannot drift into
 * answering the same question two ways.
 */
const setupGroups = (items: MigrationItem[]): MigrationGroup[] =>
  groupMigrationCandidates(items, isImportableKey, isImportableKey);

/**
 * Detected cards in the order the stage should spend its two slots on them.
 *
 * Actionable before blocked, then the shortlist's own order. Both stay visible —
 * whichever does not fit is in the add dialog's 已检测到 list — but the stage has
 * two slots and a card that can be acted on has the better claim to one.
 */
const byPrimaryRank = (left: ProviderSlot, right: ProviderSlot): number => {
  if (left.reasons.length !== right.reasons.length) {
    return (left.reasons.length > 0 ? 1 : 0) - (right.reasons.length > 0 ? 1 : 0);
  }
  const leftRank = setupPrimaryRank(left.vendor);
  const rightRank = setupPrimaryRank(right.vendor);
  if (leftRank === rightRank) return 0;
  if (leftRank === null) return 1;
  if (rightRank === null) return -1;
  return leftRank - rightRank;
};

/**
 * Every API key the scan found, and for each one whether setup may take it over.
 *
 * Discovery and permission are two projections of one scan, not two scans. A key
 * inside a group this entry point cannot consent to — a linked subscription
 * sign-in, a server blocker — is still a key that is on this machine, and a stage
 * that dropped it would answer 「we found your Anthropic key」 with an empty invitation
 * to add one. So every importable key gets a card; what a blocked group does not get
 * is a backend to consent to, which is the one thing that decides selection,
 * the batch and the counts.
 *
 * A row whose provider the server did not name keeps its own identity and is
 * labelled by its masked detail, exactly as the import dialog labels it. Guessing a
 * brand from the backend that happened to hold the key is the one thing neither
 * surface does.
 */
function detectedProviders(scan: MigrationScan | null): ProviderSlot[] {
  if (!scan) return [];
  const byIdentity = new Map<string, ProviderSlot>();
  for (const group of setupGroups(scan.items)) {
    const selectable = groupSelectable(group);
    const reasons = selectable ? [] : [...new Set(group.blockedRows.map(blockedReasonKey))];
    for (const row of group.importRows) {
      const named = row.vendor?.trim();
      const brand = named ? providerVendorId(named) : null;
      const identity = brand ?? row.id;
      const existing = byIdentity.get(identity);
      if (existing) {
        if (selectable && !existing.backends.includes(group.backend)) existing.backends.push(group.backend);
        for (const reason of reasons) {
          if (!existing.reasons.includes(reason)) existing.reasons.push(reason);
        }
        continue;
      }
      byIdentity.set(identity, {
        vendor: identity,
        brand,
        label: named ? providerBrandLabel(named, row.display_name) : row.masked_detail.trim(),
        kind: 'detected',
        // `masked_credential` is the newer separate field; `masked_detail` is the one
        // every server has always sent. Falling back to it is what stops a row from an
        // older server reading as a provider with nothing detected about it. An unnamed
        // row has no second line to fill: its masked detail is already its name, and
        // saying the same string twice on one card describes nothing.
        mask: named ? row.masked_credential?.trim() || row.masked_detail?.trim() || null : null,
        supply: null,
        pending: false,
        backends: selectable ? [group.backend] : [],
        // Copied, not shared: two rows of one blocked group are two cards, and a
        // reason later pushed onto one of them is not a reason on the other.
        reasons: [...reasons],
      });
    }
  }
  // A reason is why nothing can be done, so it only belongs on a card where nothing
  // can be. One vendor found in two backends — takeable in one, blocked in the other
  // — is a takeable card: pressing it takes over the group it may, and the group it
  // may not keeps its own reason in the review. A card that read 「not from here」 and
  // was pressable anyway would contradict itself.
  return [...byIdentity.values()]
    .map((slot) => (slot.backends.length > 0 && slot.reasons.length > 0 ? { ...slot, reasons: [] } : slot))
    .sort(byPrimaryRank);
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
      brand: vendor,
      label: providerBrandLabel(source.vendor, source.display_name),
      kind: 'connected',
      mask: source.masked_credential?.trim() || null,
      supply: { kind: source.kind, account: source.account_label?.trim() || null },
      pending: Boolean(source.verification_pending),
      backends: [],
      reasons: [],
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
    slots.push({
      vendor,
      brand: vendor,
      label: providerBrandLabel(vendor),
      kind: 'empty',
      mask: null,
      supply: null,
      pending: false,
      backends: [],
      reasons: [],
    });
  }

  return slots.slice(0, PROVIDER_SLOT_COUNT);
}

/**
 * Detected providers the stage is not already drawing.
 *
 * This is what the add dialog's 已检测到 tab lists, and whether it has anything to
 * list is what decides the tab exists at all: a tab repeating the two cards
 * immediately behind it offers nothing, and an empty tab is worse than an absent
 * one.
 *
 * What it subtracts is the detected cards, and only those. A connected card also
 * occupies its brand's slot — that collapse is presentation, and it is right — but
 * the scan has no knowledge of the Hub's inventory: it reads native stores, so a
 * row under a connected brand is a second credential until something compares the
 * two. Letting the stage's brand set answer that question is how a row the capsule
 * still counts becomes reachable from nowhere. Whether it is the same key is a
 * question about credentials, and the pane that lists it is where it is answered.
 */
export function unlistedDetected(input: {
  sources: readonly Source[];
  scan: MigrationScan | null;
}): ProviderSlot[] {
  const drawn = new Set(
    providerSlots(input).filter((slot) => slot.kind === 'detected').map((slot) => slot.vendor),
  );
  return detectedProviders(input.scan).filter((slot) => !drawn.has(slot.vendor));
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
    setupGroups(items)
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
 * selectable groups the cards and the dialog are built from — already only keys,
 * because that is setup's scope, and 「发现 N 个可导入的 API Key」 is what the
 * sentence says.
 *
 * The migration feature's own projection, because the standalone capsule in
 * Settings' wizard has to count the same way and cannot reach into this screen to
 * do it.
 */
export function offeredImportKeys(selection: MigrationSelection): MigrationItem[] {
  return takeableImportRows(selection.scan?.items ?? [], isImportableKey, isImportableKey);
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
  return setupGroups(items)
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

/** A backend's rows as an order-independent identity, so two scans can be asked
 *  whether they describe the same credentials rather than merely the same names. */
const backendRowIdentity = (items: readonly MigrationItem[], backend: AgentBackend): string =>
  items.filter((item) => item.backend === backend).map((item) => item.id).sort().join('\n');

/**
 * Consent a fresh scan still supports.
 *
 * Run whenever the scan is replaced, and deliberately conservative: consent was given
 * to rows that were on screen, so it survives only where the new scan asks the same
 * question. Three ways it stops doing that.
 *
 * A group that was imported, newly blocked or has lost its importable rows can no
 * longer be submitted at all, and a stale name left behind would keep the CTA
 * offering a batch the dialog would refuse to build.
 *
 * A group whose custody closure has grown can be submitted, and refused: the server
 * rejects a batch that migrates one backend while leaving a linked one out, so a
 * selection holding only part of a closure is a press that always fails.
 *
 * And a group whose rows have CHANGED is the quiet one — a key that appeared under an
 * already-consented backend would ride into the next batch on a decision made about
 * a different set of credentials. Nobody consented to that one, so the group goes
 * back for review rather than carrying its tick forward.
 */
export function reconcileSelection(
  selection: MigrationSelection,
  previous: MigrationScan | null,
): AgentBackend[] {
  const items = selection.scan?.items ?? [];
  const priorItems = previous?.items ?? [];
  const carried = new Set(selection.selectedBackends);
  const selectable = new Map(
    setupGroups(items)
      .filter(groupSelectable)
      .map((group) => [group.backend, group] as const),
  );
  return selection.selectedBackends.filter((backend) => {
    const group = selectable.get(backend);
    if (!group) return false;
    return [...group.required].every((linked) => (
      carried.has(linked)
      && backendRowIdentity(items, linked) === backendRowIdentity(priorItems, linked)
    ));
  });
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
 * The engine is ensured before this is asked, so what is left to choose is what to
 * ADOPT: the first present backend not already in hub mode, in the shared backend
 * order. The choice is not cosmetic — `resumeGatewayAdoption` is named for one
 * backend and returns success immediately when that one is already in hub mode, so
 * naming an adopted backend would adopt nothing while an unadopted CLI sat beside
 * it. When every present backend is already adopted, the first present one is that
 * same no-op and correctly reports there is nothing left to do.
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
  /** The read failed and asking again is worth doing. Distinct from `waiting`, which
   *  is a read still in flight: this one has nothing coming unless someone asks. The
   *  contract is explicit that a read error shows the runtime-unread copy *with
   *  retry* rather than the unsupported notice, and without this kind the two are
   *  the same silent `idle` card. */
  | { kind: 'unreadable' }
  /** The engine is missing or stopped and this screen may resume it. `runtime` is the
   *  authoritative snapshot the step was read from, carried so the resume starts from
   *  the state that authorized it rather than reading the same thing a second time. */
  | { kind: 'resume'; step: 'install' | 'start'; runtime: RuntimeDependency }
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
  // A read that failed, asked before anything else is decided from it. `loading` and
  // a refresh-degraded read are both a read in flight and stay quiet; a first read
  // that failed (`unread`) and a later one that failed over stale data
  // (`degraded/read_failed`) are not coming back on their own. Non-retryable is left
  // waiting on purpose: the shell owns the read, and a Retry that cannot help is a
  // worse answer than none.
  const unreadable = foldRegionRead(input.runtimeRead, {
    loading: () => false,
    ready: () => false,
    unread: (retryable) => retryable,
    degraded: (_stale, cause, retryable) => cause === 'read_failed' && retryable,
  });
  if (unreadable) return { kind: 'unreadable' };
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
  if (step === 'start') return { kind: 'resume', step: 'start', runtime };
  return setupCanAttemptInstall(input.capability, input.gatewayEnabled, input.runtimeRead)
    ? { kind: 'resume', step: 'install', runtime }
    : { kind: 'unsupported' };
}

/**
 * Whether the evidence above is an ANSWER, or still on its way to one.
 *
 * A retry is a request: it asks the shell to read the machine again, and the states
 * that read passes through on the way back are not answers. `beginRegionRead`
 * degrades the previous value to `refreshing`, and a configuration being re-read
 * reports `pending`/`null` before it reports what it found. `gatewayIntent` folds
 * every one of them to `waiting`, which is the right thing to DRAW and says nothing
 * about whether the request has been served — so a request spent on the first of
 * them is spent on the read's start and never sees its result.
 *
 * Three things are answers. A read that failed, which answers 「不知道」. A
 * configuration that came back off, which no runtime read can override. And a read
 * that landed.
 *
 * The failed read is asked FIRST, ahead of the configuration, because one load
 * failure usually takes both: the shell comes back with no capability, no saved
 * intent and an unread machine. That is a complete answer to the request — nothing
 * more is coming unless someone asks again — and filing it under 「configuration
 * unknown」 would leave the request pending on a read that is never arriving, to
 * fire on some unrelated later one. It is still distinguishable from a read in
 * flight: `beginRegionRead` maps a failure to `loading`, so a re-read over a
 * failure is `loading` and the failure itself is not.
 */
export function gatewayEvidenceSettled(input: {
  capability: SetupCapability;
  gatewayEnabled: boolean | null;
  runtimeRead: RegionRead<RuntimeDependency>;
}): boolean {
  if (regionFailed(input.runtimeRead)) return true;
  if (input.capability === 'disabled' || input.gatewayEnabled === false) return true;
  if (!setupNavigationReady(input.capability, input.gatewayEnabled)) return false;
  return foldRegionRead(input.runtimeRead, {
    loading: () => false,
    ready: () => true,
    unread: () => true,
    degraded: (_stale, cause) => cause === 'read_failed',
  });
}

export type ProviderActionKind =
  | 'import'
  | 'retryImport'
  | 'continue'
  | 'add'
  | 'retrySupply'
  | 'connecting'
  | 'checking';

/**
 * What the screen actually knows about the source inventory.
 *
 * An empty list and a list the screen could not read look identical once they are
 * both `Source[]`, and the difference is the whole question the primary action is
 * answering: offering 「添加」 against an answer nobody has is how a second copy of
 * a credential that already exists gets written. So the answer and the absence of
 * one are separate states here, and the absence is not silent — the contract's rule
 * for every setup supply read is that a failed result keeps Retry reachable.
 */
export type SupplyState =
  /** A read is in flight and nothing has landed yet. */
  | { kind: 'reading' }
  /** A read failed. Nothing is coming unless someone asks again. */
  | { kind: 'unreadable' }
  /** An authoritative answer, whichever way it went. */
  | { kind: 'read'; hasSource: boolean };

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
 * Every state that would WRITE additionally needs the engine to be serving, and
 * says so by staying visible and unpressable rather than by disappearing: adding a
 * source and taking over a key both go to the Hub, and one that is stopped, failed,
 * unsupported or simply not readable this second cannot take either. What went
 * wrong is on the gateway card with the recovery next to it, which is the one place
 * it can be acted on — so the footer keeps naming what the person came to do
 * instead of offering a press that reaches nothing.
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
  /** What is known about the inventory, not what happens to be in it. */
  supply: SupplyState;
  /** The engine is installing or starting. */
  gatewayBusy: boolean;
  /** The flow is admitted to the Hub and the Hub is serving it: the configuration
   *  prerequisite the next screen is gated on, plus the authoritative runtime read —
   *  never this screen's own attempt, and never one without the other. Writing and
   *  continuing both need exactly this, so both read the same field. */
  hubAdmitted: boolean;
  /** A source was written and its connection is being read back. */
  verifying: boolean;
}): ProviderActionState {
  if (input.gatewayBusy) return { kind: 'connecting', count: 0 };
  if (input.verifying) return { kind: 'checking', count: 0 };
  if (input.pendingCount > 0) {
    // Ahead of the inventory on purpose: a scan that arrived is an answer of its
    // own, and taking over a key it found does not depend on knowing what else is
    // already there. It does depend on the engine it would write to.
    return {
      kind: input.importFailed ? 'retryImport' : 'import',
      count: input.pendingCount,
      blocked: !input.hubAdmitted,
    };
  }
  if (input.supply.kind === 'reading') return { kind: 'checking', count: 0 };
  if (input.supply.kind === 'unreadable') return { kind: 'retrySupply', count: 0 };
  if (input.supply.hasSource) return { kind: 'continue', count: 0, blocked: !input.hubAdmitted };
  return { kind: 'add', count: 0, blocked: !input.hubAdmitted };
}

/** Kinds `providerAction` only ever produces from a positive pending count. */
type CountedActionKind = 'import' | 'retryImport';

/**
 * The counted labels, named by the base `t(base, { count })` resolves them under.
 *
 * C1 ships both as `_one`/`_other` families, which `TranslationKey` deliberately
 * cannot name — it is the leaves that resolve *without* count. C2's counted member
 * is what names them, and it requires the count in the same claim, which is why
 * these are a table of their own rather than a row in the one below.
 *
 * Typed as the claim rather than as `SetupCountedLabelKey` on purpose: the e2e
 * fidelity project compiles this module without the bundle augmentation, where the
 * derived family type collapses to `never` and nothing could satisfy it. That two
 * keys really are families is checked where the bundles are in scope —
 * `setupAction.types.test.ts` for the type and `providerStage.test.ts` for both locales.
 */
export const COUNTED_LABEL = {
  import: 'onboarding.providers.actionImport',
  retryImport: 'onboarding.providers.actionRetryImport',
} as const satisfies Record<CountedActionKind, SetupAction['labelKey']>;

/** Everything else: a label that resolves on its own, with no count to carry. */
export const PLAIN_LABEL = {
  continue: 'onboarding.providers.actionContinue',
  add: 'onboarding.providers.actionAdd',
  // The shared label, not a new one: the contract lets the active owner publish
  // Retry as the CTA while Continue is held, and this is that.
  retrySupply: 'common.retry',
  connecting: 'onboarding.providers.actionConnecting',
  checking: 'onboarding.providers.actionChecking',
} as const satisfies Record<Exclude<ProviderActionKind, CountedActionKind>, TranslationKey>;

const BUSY_ACTIONS = new Set<ProviderActionKind>(['connecting', 'checking']);

/** The arrow means the press leaves this screen. Adding a provider and asking for a
 *  read again both stay here, so neither wears it. */
const STAYS_HERE = new Set<ProviderActionKind>(['add', 'retrySupply']);

/** The C2 action the shell renders. The screen never draws the button itself. */
export function providerSetupAction(state: ProviderActionState): SetupAction {
  const busy = BUSY_ACTIONS.has(state.kind);
  const chrome: Pick<SetupAction, 'disabled' | 'busy' | 'icon'> = {
    disabled: busy || state.blocked === true,
    busy,
    icon: busy ? 'spinner' : STAYS_HERE.has(state.kind) ? 'none' : 'arrow-right',
  };
  // The two counted kinds carry their count, and nothing else carries one: a family
  // without a count does not resolve, and a plain key has nothing to interpolate.
  // Narrowed rather than asserted, so the pairing is the compiler's to check.
  return state.kind === 'import' || state.kind === 'retryImport'
    ? { ...chrome, labelKey: COUNTED_LABEL[state.kind], labelArgs: { count: state.count } }
    : { ...chrome, labelKey: PLAIN_LABEL[state.kind] };
}
