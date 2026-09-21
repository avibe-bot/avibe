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
import type { AgentBackend, MigrationScan, RouteHop } from '../settings/models/types';

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

/**
 * The screens this instance runs, in order.
 *
 * Model Hub is default-on (`is_model_hub_enabled()` defaults to `"1"` since #1917), so the
 * three-screen flow is the ordinary path. A deployment that explicitly turns the capability
 * off has no gateway to add providers to, and the flow drops that screen rather than
 * rendering an empty one or growing a third shape: screen 3 then keeps the per-assistant
 * connection actions it has today.
 *
 * While the capability is `pending` this returns the full sequence, and that is NOT the
 * wait: a returned sequence says which screens exist, not that the shell may enter them.
 * `setupNavigationReady` is the wait, and the shell must honor it — holding the user on the
 * introduction with the primary action disabled — because entering the providers screen on a
 * guess and then removing it when the read resolves to `disabled` recreates exactly the jump
 * the shorter sequence exists to prevent.
 */
export const setupScreenSequence = (capability: SetupCapability): readonly SetupScreenId[] =>
  capability === 'disabled'
    ? SETUP_SCREENS.filter((screen) => screen !== 'providers')
    : SETUP_SCREENS;

/** Whether the shell may leave the introduction. False until the capability read settles. */
export const setupNavigationReady = (capability: SetupCapability): boolean =>
  capability !== 'pending';

/** The screen a Back action leaves to, or `null` on the first one. */
export const setupBackTarget = (
  sequence: readonly SetupScreenId[],
  current: SetupScreenId,
): SetupScreenId | null => {
  const index = sequence.indexOf(current);
  return index > 0 ? sequence[index - 1] : null;
};
