/**
 * C2 — the setup flow's interface contract.
 *
 * Frozen on `master` before the three-screen lanes fork, so a lane reads one shared
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
import type { TranslationKey } from '@/i18n/types';

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
  /** This screen is the current one. An inactive screen stays mounted but is `hidden` and
   *  `inert`, which is what makes back-navigation preserve its state without each screen
   *  having to remember anything. */
  active: boolean;
  handoff: SetupHandoffTarget | false;
  onActionChange: (action: SetupAction) => void;
  /** Request a screen change. The shell decides whether to play a handoff. */
  onNavigate: (screen: SetupScreenId) => void;
};

/**
 * State the SHELL owns, because it has to outlive any single screen: leaving screen 2 for
 * screen 3 and coming back must find the same selection, the same cumulative import count
 * and the same route order (handoff §5 "状态保留"). Anything the server already owns —
 * installed CLIs, enabled backends, persisted sources — is read, never mirrored here.
 */
export type SetupFlowState = {
  /** Migration item ids ticked for the next import batch. Emptied by a successful apply. */
  providerSelection: string[];
  /** Cumulative keys imported by THIS flow, which is what the completion line reports. */
  importedCount: number;
  /** Source ids added through the "Add more" card, which is what its badge counts. */
  addedThroughMore: string[];
  /** The route dialog's working PREFERENCE, as source ids, preferred first. Hydrated by
   *  MERGING every enabled backend's persisted order — the backend that will run establishes
   *  precedence, and ids found only on the others are appended in their own persisted order —
   *  because a write replaces that backend's enabled subset, so a preference missing a source
   *  another backend relies on would silently disable its supply. Empty means "not derived
   *  yet", never "no route".
   *
   *  Hydrate only before the first edit or while `routeOrderDirty` is false, and reconcile
   *  after a confirmed write. A dirty draft survives screen navigation — that is the reason
   *  this state lives in the shell — so leaving through the dialog's "add model source" exit
   *  and coming back must not throw the user's ordering away.
   *
   *  It is not an entry-gate input: C4 reads the server, because a gate resting on client
   *  draft state would block an installation that already has valid persisted routes. Nor is
   *  it what gets written verbatim — see C6 for the per-backend projection and its owner. */
  routeOrder: string[];
  /** Whether `routeOrder` holds an edit the server has not confirmed. Decides if a re-entry
   *  may hydrate; a confirmed write clears it. */
  routeOrderDirty: boolean;
};

export const INITIAL_SETUP_FLOW_STATE: SetupFlowState = {
  providerSelection: [],
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
 * (`modelHubEnabledFromConfig`, the same projection `readOpencodeSetupRoutes` uses), which is
 * authoritative because the shell does not render without a successful config read. Anything
 * less stays `pending`, and `setupNavigationReady` keeps the user on the introduction until it
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
