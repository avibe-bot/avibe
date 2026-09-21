/**
 * C2 — the setup flow's interface contract.
 *
 * Proposed for `master` before the three-screen lanes fork, so a lane reads one shared
 * declaration instead of transcribing a shape out of a plan document. Everything here is
 * cross-lane by construction: the shell (L1) renders it, the providers screen (L2) and the
 * assistants screen (L3) implement against it. A deviation needs orchestrator sign-off
 * BEFORE the edit, not a reconciling commit afterwards.
 *
 * The binding prose that goes with these types — geometry and DOM hooks (C3), the entry
 * gate (C4), the stable dialog frame (C5) and the field-by-field data mapping (C6) — lives
 * in `docs/plans/setup-three-screen/contracts.md`. Copy lives in
 * `docs/plans/setup-three-screen/copy-contract.json` (C1).
 */
import type { Dispatch, SetStateAction } from 'react';
import type { TranslationKey } from '@/i18n/types';
import type { AgentBackend, MigrationScan, RouteHop, RuntimeDependency } from '../settings/models/types';
import { foldRegionRead, type RegionRead } from '../settings/models/regionRead';
import { runtimeCanAttemptInstall, runtimeIsRunning } from '../settings/models/runtimeLifecycle';

/** The flow's order. A screen id, not an index: `Wizard.tsx` used to hold `'welcome' |
 *  'agents'`, and an index-based step cannot survive a screen being added or skipped. */
export const SETUP_SCREENS = ['intro', 'providers', 'assistants'] as const;
export type SetupScreenId = (typeof SETUP_SCREENS)[number];

/**
 * Which transition is playing, or `false` when none is. Named for the screen being
 * ENTERED, because the snapshot is taken from the screen being left: `providers` shrinks
 * the story cards into the destination row, `assistants` lifts that row into full cards.
 */
export type SetupHandoffTarget = Exclude<SetupScreenId, 'intro'>;

/** The leading glyph the shell draws inside the primary action. */
export type SetupActionIcon = 'none' | 'arrow-right' | 'spinner';

/**
 * One screen's claim on the shell's primary action. The shell owns the element — that is
 * what makes "the button never moves" a property of the tree rather than an agreement
 * between two stylesheets — so a screen describes the button and never renders it.
 *
 * `labelKey` is a key from C1. It is typed against the live bundle, so a screen cannot
 * name a string that has not shipped.
 */
export type SetupAction = {
  labelKey: TranslationKey;
  /** Interpolation values for `labelKey`, e.g. `{ count: 2 }` or `{ name: 'Codex' }`. */
  labelArgs?: Record<string, string | number>;
  disabled: boolean;
  /** Renders the spinner glyph and suppresses the arrow. Distinct from `disabled`: a busy
   *  action is also disabled, but a disabled one is not necessarily busy. */
  busy: boolean;
  icon: SetupActionIcon;
};

/**
 * How the shell drives the active screen. The primary action is the shell's, so the click
 * has to be handed down; a screen decides what the click MEANS (import the batch, continue,
 * open the add dialog, enter the workspace).
 */
export type SetupScreenHandle = {
  activate: () => void;
};

export type SetupScreenProps = {
  /** Mounted does not mean active: hidden/inert screens retain drafts but must not start
   *  mutations, poll, claim actions, navigate or run authorization effects (C2). */
  active: boolean;
  handoff: SetupHandoffTarget | false;
  /** One shell-derived policy for navigation, admission and candidate paths. */
  policy: SetupPolicy;
  /** Ask the shell's existing bootstrap/read owner to retry in the current screen.
   *  This never navigates to a removed providers step or starts an install by itself. */
  onRetryRuntime: () => void;
  flowState: SetupFlowState;
  /** Pass the shell's React setter. Screens use functional updates to preserve changes
   *  made by other screens while an asynchronous operation was pending. */
  setFlowState: Dispatch<SetStateAction<SetupFlowState>>;
  /** Bound to this screen by the shell; ignore publications from an inactive screen or
   *  an obsolete activation. Only the active screen's handle may receive a click. */
  onActionChange: (action: SetupAction) => void;
  /** Request a screen change. The shell decides whether to play a handoff. */
  onNavigate: (screen: SetupScreenId) => void;
};

/** One scan snapshot plus whole-backend consent. The full scan is needed to expand
 *  required_backends transitively and expose blockers; never store only visible keys.
 *  selectedBackends is the union of complete, unblocked consent groups (C6), not a
 *  per-row selection. L2 must adapt the existing MigrationDialog owner to this controlled
 *  state before using it in setup; its shipped props do not yet accept this shape. */
export type SetupProviderSelection = {
  scan: MigrationScan | null;
  selectedBackends: AgentBackend[];
};

/**
 * State the SHELL owns, because it has to outlive any single screen: leaving screen 2 for
 * screen 3 and coming back must find the same selection, the same cumulative import count
 * and the same route order (handoff §5 "状态保留"). Anything the server already owns —
 * installed CLIs, enabled backends, persisted sources — is read, never mirrored here.
 */
export type SetupFlowState = {
  /** Ephemeral scan/consent draft. Replace on a fresh scan and reconcile whole groups;
   *  clear confirmed groups after apply. No credential material lives in this state. */
  providerSelection: SetupProviderSelection;
  /** Cumulative confirmed `MigrationApplyResult.applied`, counted once per submitted
   *  batch. It counts migration items, not necessarily keys or created sources (C6). */
  importedCount: number;
  /** Source ids added through the "Add more" card, which is what its badge counts. */
  addedThroughMore: string[];
  /** Model-ranked working draft: each row identifies BOTH source and upstream model.
   *  This is not a putAgentSources payload. C6/D4/D9 specify explicit Agent/menu-model
   *  targets and projection through each target's reviewed exact-hop membership. The
   *  shared mounted route owner retains target baselines and pending write receipts;
   *  this list alone cannot reconstruct them or justify a source-priority write.
   *  Hydrate only while clean; a dirty draft survives navigation and is cleared only
   *  after every intended write is read back. Empty means no hydrated draft, not no
   *  server route. C4 never gates entry on this client draft. */
  routeOrder: RouteHop[];
  /** Whether `routeOrder` holds an edit the server has not confirmed. Decides if a re-entry
   *  may hydrate; all intended writes must be confirmed before clearing it. */
  routeOrderDirty: boolean;
};

export const INITIAL_SETUP_FLOW_STATE: SetupFlowState = {
  providerSelection: { scan: null, selectedBackends: [] },
  importedCount: 0,
  addedThroughMore: [],
  routeOrder: [],
  routeOrderDirty: false,
};

/**
 * The capability read as a three-state, because two states cannot express "no authoritative
 * answer yet".
 *
 * The producer matters: `disabled` must mean an authoritative read that says the deployment
 * turned Model Hub off, never a request that failed. `useModelHubCapability()` catches its
 * own error and resolves to `false`, so it cannot distinguish the two and is NOT a safe
 * producer here; derive the value from the config the Wizard has already loaded
 * (`modelHubEnabledFromConfig`, after verifying capabilities.model_hub.enabled is a boolean;
 * that helper also returns false for missing fields). This is the same projection
 * `readOpencodeSetupRoutes` uses; a successful config read plus the explicit boolean supplies
 * the authority. Anything less stays `pending`, and `setupNavigationReady` keeps the user on the introduction until it
 * settles.
 */
export type SetupCapability = 'pending' | 'enabled' | 'disabled';

export const setupCapability = (modelHubEnabled: boolean | null): SetupCapability =>
  modelHubEnabled === null ? 'pending' : modelHubEnabled ? 'enabled' : 'disabled';

/** Deployment capability and host install admission are independent facts. Only ready
 *  RegionRead values authorize work. A stale snapshot retains layout during retry, never
 *  readiness; `unresolved` manifests are admitted by runtimeCanAttemptInstall. */
export type SetupPolicy = {
  capability: SetupCapability;
  runtimeRead: 'pending' | 'retry' | 'ready';
  installSupport: 'unknown' | 'admitted' | 'unsupported';
  hubRunning: boolean;
};

export const setupPolicy = (
  capability: SetupCapability,
  runtimeRead: RegionRead<RuntimeDependency>,
): SetupPolicy => {
  const project = (runtime: RuntimeDependency, read: SetupPolicy['runtimeRead']): SetupPolicy => ({
    capability,
    runtimeRead: read,
    installSupport: runtimeCanAttemptInstall(runtime) ? 'admitted' : 'unsupported',
    hubRunning: runtimeIsRunning(runtime),
  });
  return foldRegionRead<RuntimeDependency, SetupPolicy>(runtimeRead, {
    loading: () => ({ capability, runtimeRead: 'pending', installSupport: 'unknown', hubRunning: false }),
    unread: () => ({ capability, runtimeRead: 'retry', installSupport: 'unknown', hubRunning: false }),
    ready: (runtime) => project(runtime, 'ready'),
    degraded: (runtime, cause) => project(runtime, cause === 'refreshing' ? 'pending' : 'retry'),
  });
};

/** Admission for any setup path that might install; running health is a separate fact. */
export const setupCanAttemptInstall = (policy: SetupPolicy): boolean =>
  policy.capability === 'enabled' && policy.runtimeRead === 'ready' && policy.installSupport === 'admitted';

/** Unsupported installation does not disable an already-running Hub. Retained stale
 *  facts may hold the current layout while retrying, but setupNavigationReady and
 *  setupCandidatePath require fresh facts before progression/readiness. */
export const setupScreenSequence = (policy: SetupPolicy): readonly SetupScreenId[] =>
  policy.capability === 'disabled' || (policy.installSupport === 'unsupported' && !policy.hubRunning)
    ? SETUP_SCREENS.filter((screen) => screen !== 'providers')
    : SETUP_SCREENS;

/** Derive the rendered screen together with its sequence, never in a later effect.
 *  Late support resolution cannot render a removed screen for one frame. The shell
 *  uses this on every navigation request and commits the reconciled id on resolution. */
export const setupCurrentScreen = (policy: SetupPolicy, requested: SetupScreenId): SetupScreenId => {
  if (policy.capability === 'pending') return 'intro';
  return requested === 'providers' && !setupScreenSequence(policy).includes('providers')
    ? 'assistants'
    : requested;
};

/** Intro must be able to reach providers to bootstrap and read support. Downstream
 *  continuation is held on unread/error; Back and the shell's Retry remain available.
 *  Running/source/connection requirements for the active action still apply (C4/C6). */
export const setupNavigationReady = (policy: SetupPolicy, current: SetupScreenId): boolean =>
  policy.capability !== 'pending'
  && (current === 'intro' || policy.capability === 'disabled' || policy.runtimeRead === 'ready');

/** Select the existing configuration/readiness owner from persisted supply_mode.
 *  This is a policy path, not a readiness verdict, and performs no mode/credential writes. */
export const setupCandidatePath = (
  policy: SetupPolicy,
  mode: 'direct' | 'hub' | undefined,
): 'pending' | 'direct' | 'configure-hub' | 'hub' | 'hub-recovery' => {
  if (policy.capability === 'pending' || mode === undefined) return 'pending';
  if (mode === 'hub') {
    return policy.capability === 'enabled' && policy.runtimeRead === 'ready' && policy.hubRunning
      ? 'hub' : 'hub-recovery';
  }
  if (policy.capability === 'disabled') return 'direct';
  if (policy.runtimeRead !== 'ready') return 'pending';
  return policy.installSupport === 'unsupported' ? 'direct' : 'configure-hub';
};

/** The screen a Back action leaves to, or `null` on the first one. */
export const setupBackTarget = (
  sequence: readonly SetupScreenId[],
  current: SetupScreenId,
): SetupScreenId | null => {
  const index = sequence.indexOf(current);
  return index > 0 ? sequence[index - 1] : null;
};
