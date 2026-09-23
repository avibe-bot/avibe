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
import type { ParseKeys } from 'i18next';
import type { TranslationKey } from '@/i18n/types';
import type { AgentBackend, MigrationScan, RuntimeDependency } from '../settings/models/types';
import { foldRegionRead, type RegionRead } from '../settings/models/regionRead';
import { runtimeCanAttemptInstall, runtimeIsRunning } from '../settings/models/runtimeLifecycle';

/** The flow's order. A screen id, not an index: `Wizard.tsx` used to hold `'welcome' |
 *  'agents'`, and an index-based step cannot survive a screen being added or skipped. */
export const SETUP_SCREENS = ['intro', 'providers', 'assistants'] as const;
export type SetupScreenId = (typeof SETUP_SCREENS)[number];

/**
 * Which transition is playing, or `false` when none is. Named for the screen being
 * ENTERED, because the snapshot is taken from the screen being left: `providers` shrinks
 * the story cards into the destination row, `assistants` lifts that row into full cards,
 * and `intro` grows the row back into the story it came from.
 */
export type SetupHandoffTarget = SetupScreenId;

/** The leading glyph the shell draws inside the primary action. */
export type SetupActionIcon = 'none' | 'arrow-right' | 'spinner';

/** Written as a generic so the conditional distributes over the key union: the same test
 *  applied to `ParseKeys` directly matches the whole union at once and yields `never`. */
type PluralBase<Key> = Key extends `${infer Base}_other` ? Base : never;

/**
 * A C1 plural family, named by the base `t(base, { count })` resolves it under.
 *
 * `TranslationKey` is deliberately the leaves that resolve WITHOUT a count, so it cannot
 * name a counted label at all — and "Review 2 selected" is exactly a counted label. This
 * widens what a screen may claim by nothing: the base is derived from the `_other` form
 * C1 already requires in both bundles, so a string that has not shipped as a family is
 * still not nameable, and the count that makes it resolvable is required below rather
 * than left to the shell to guess.
 */
export type SetupCountedLabelKey = PluralBase<ParseKeys>;

/** Interpolation values for `labelKey`, e.g. `{ count: 2 }` or `{ name: 'Codex' }`. */
export type SetupActionArgs = Record<string, string | number>;

/**
 * One screen's claim on the shell's primary action. The shell owns the element — that is
 * what makes "the button never moves" a property of the tree rather than an agreement
 * between two stylesheets — so a screen describes the button and never renders it.
 *
 * `labelKey` is a key from C1. It is typed against the live bundle, so a screen cannot
 * name a string that has not shipped. A counted label is the second member: its family
 * resolves only with a count, so the count travels as part of the claim instead of being
 * something the shell hopes was passed. The fields and their meanings are the same in
 * both members, so a consumer reads `labelKey` and `labelArgs` without narrowing.
 */
export type SetupAction = {
  disabled: boolean;
  /** Renders the spinner glyph and suppresses the arrow. Distinct from `disabled`: a busy
   *  action is also disabled, but a disabled one is not necessarily busy. */
  busy: boolean;
  icon: SetupActionIcon;
} & (
  | { labelKey: TranslationKey; labelArgs?: SetupActionArgs }
  | { labelKey: SetupCountedLabelKey; labelArgs: SetupActionArgs & { count: number } }
);

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
   *  mutations, poll, claim actions, navigate or run authorization effects (C2).
   *  D11 bootstrap runs only on active provider entry with capability enabled;
   *  intro mount alone never seeds config or starts services. */
  active: boolean;
  handoff: SetupHandoffTarget | false;
  capability: SetupCapability;
  /** Fresh config.model_hub.enabled: persisted user intent, separate from deployment capability. */
  gatewayEnabled: boolean | null;
  /** Shell-owned authoritative runtime read. Host admission never changes capability
   *  or the screen sequence. Stale/error reads authorize no installation or Hub entry. */
  runtimeRead: RegionRead<RuntimeDependency>;
  /** Re-read config, then resume the existing bootstrap/runtime owner only if enabled.
   *  Preserve drafts and never re-enable a disabled configuration automatically. */
  onRetrySetup: () => void;
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
 * screen 3 and coming back must find the same selection and cumulative import count.
 * Anything the server already owns — installed CLIs, enabled backends, persisted
 * sources and model routes — is read, never mirrored here.
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
};

export const INITIAL_SETUP_FLOW_STATE: SetupFlowState = {
  providerSelection: { scan: null, selectedBackends: [] },
  importedCount: 0,
  addedThroughMore: [],
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
 * the authority. Anything less stays `pending`. Setup requires enabled capability;
 * disabled is a configuration boundary, never a Direct setup branch.
 */
export type SetupCapability = 'pending' | 'enabled' | 'disabled';

export const setupCapability = (modelHubEnabled: boolean | null): SetupCapability =>
  modelHubEnabled === null ? 'pending' : modelHubEnabled ? 'enabled' : 'disabled';

/** Setup has one fixed SETUP_SCREENS journey. Capability/runtime never remove a step.
 *  Disabled configuration is preserved and requires recovery, not Direct completion. */
export const setupNavigationReady = (capability: SetupCapability, gatewayEnabled: boolean | null): boolean =>
  capability === 'enabled' && gatewayEnabled === true;

/** Fresh install admission; unresolved manifests may still be installable. */
export const setupCanAttemptInstall = (
  capability: SetupCapability,
  gatewayEnabled: boolean | null,
  runtimeRead: RegionRead<RuntimeDependency>,
): boolean => setupNavigationReady(capability, gatewayEnabled) && foldRegionRead(runtimeRead, {
  loading: () => false,
  unread: () => false,
  degraded: () => false,
  ready: (runtime) => runtime.enabled !== false && runtimeCanAttemptInstall(runtime),
});

/** Running health is independent of install admission. Providers may continue with an
 *  existing running Hub even when new installation is unsupported. */
export const setupHubRunning = (runtimeRead: RegionRead<RuntimeDependency>): boolean =>
  foldRegionRead(runtimeRead, {
    loading: () => false,
    unread: () => false,
    degraded: () => false,
    ready: (runtime) => runtime.enabled !== false && runtimeIsRunning(runtime),
  });

/** A necessary mode/runtime gate, NOT a complete readiness verdict. C4 additionally
 *  requires the same candidate's route, credentials, application and permissions.
 *  Setup always requires Hub; post-setup Direct choices belong to Settings. */
export const setupCandidateAllowed = (
  capability: SetupCapability,
  gatewayEnabled: boolean | null,
  runtimeRead: RegionRead<RuntimeDependency>,
  mode: 'direct' | 'hub' | undefined,
): boolean => setupNavigationReady(capability, gatewayEnabled) && mode === 'hub' && setupHubRunning(runtimeRead);

/** The screen a Back action leaves to, or `null` on the first one. */
export const setupBackTarget = (
  sequence: readonly SetupScreenId[],
  current: SetupScreenId,
): SetupScreenId | null => {
  const index = sequence.indexOf(current);
  return index > 0 ? sequence[index - 1] : null;
};
