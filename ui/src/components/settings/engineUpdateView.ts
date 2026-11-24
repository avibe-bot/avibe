// The Model Hub engine's own updates, as the engine's row in Settings › Dependencies says them
// (docs/plans/model-hub-engine-channel.md, section 5; ui/design: settings/engine-updates/*).
//
// The backend reports twenty update states. A person reads one sentence and one button, so this
// fold is the only place a state is read: everything the row draws comes from its result.
import type { DependencyItem } from '@/context/ApiContext';

/** What the strip says. Several backend states share one sentence. */
export type EngineUpdateSentence =
  | 'current' | 'notChecked' | 'checking' | 'available' | 'updating' | 'waitingForIdle'
  | 'undone' | 'unfinished' | 'replaced' | 'needsAvibe' | 'cannotCheck' | 'withdrawing'
  | 'stopped' | 'cannotStart';

/** The strip's one button: what it does, and whether that work is running now. */
export type EngineUpdateButton = {
  does: 'check' | 'update';
  label: 'check' | 'update' | 'retry';
  busy?: boolean;
};

export type EngineUpdateView = { sentence: EngineUpdateSentence; button: EngineUpdateButton };

/** What this page knows that a fresh read does not. */
export type EngineUpdateContext = {
  /** The operation this page started and is still waiting on. */
  working?: 'check' | 'update' | null;
  /** This page's last update ended as `failed`: the read alone cannot tell that from a failed check. */
  updateFailed?: boolean;
};

/** `blocked` because the running build was withdrawn and the one build named to replace it cannot run here. */
const WITHDRAWN_REASONS = new Set(['engine_withdrawn', 'engine_withdrawn_without_fallback']);

/** States in which the engine's owner is at work now. */
const WORKING_STATES = new Set(['recovering', 'checking', 'updating', 'rolling_back', 'withdrawing']);

const CHECK: EngineUpdateButton = { does: 'check', label: 'check' };
const UPDATE: EngineUpdateButton = { does: 'update', label: 'update' };
const RETRY_UPDATE: EngineUpdateButton = { does: 'update', label: 'retry' };
const RETRY_CHECK: EngineUpdateButton = { does: 'check', label: 'retry' };

/**
 * Whether the engine's own update channel applies to this row. A build that ships no channel root
 * (`uncommissioned`), a manifest override (`opted_out`) and a runtime that reports no state keep
 * the row as it was.
 */
export const engineUpdatesApply = (dependency: Pick<DependencyItem, 'id' | 'update_state'>): boolean =>
  dependency.id === 'model-hub-engine'
  && dependency.update_state !== undefined
  && dependency.update_state !== 'uncommissioned'
  && dependency.update_state !== 'opted_out';

/** A build tag as the row shows it: `model-hub-engine-v8.0.63-2` reads `8.0.63-2`. */
export const engineBuildVersion = (release: string | null | undefined): string | null =>
  release ? release.replace(/^model-hub-engine-/, '').replace(/^v/i, '') : null;

/**
 * How soon the row should read itself again because the engine's owner will change it without
 * being asked, or `null` when it will not: every few seconds while the owner is at work; slowly
 * while an update waits for idle (the owner tries again every five minutes) or a build is offered
 * with routine updates on (the next routine pass installs it). Otherwise an open page would go on
 * saying "waiting" or "available" after the engine had updated.
 */
export const engineUpdateRereadMs = (
  dependency: Pick<DependencyItem, 'id' | 'update_state' | 'auto_update'>,
): number | null => {
  if (!engineUpdatesApply(dependency)) return null;
  if (WORKING_STATES.has(dependency.update_state ?? '')) return 3_000;
  if (dependency.update_state === 'deferred') return 30_000;
  if (dependency.update_state === 'available' && dependency.auto_update !== false) return 60_000;
  return null;
};

/** The twenty states as the sentences a person sees; `null` where the row stays as it was. */
export function engineUpdateView(dep: DependencyItem, context: EngineUpdateContext = {}): EngineUpdateView | null {
  if (!engineUpdatesApply(dep)) return null;
  if (context.working === 'check') return { sentence: 'checking', button: { ...CHECK, busy: true } };
  if (context.working === 'update') return { sentence: 'updating', button: { ...UPDATE, busy: true } };
  // A candidate is `available_release` and nothing else (`latest_version` and `has_update` serve
  // repair), and it is an offer only in `available`: a tag left beside a quiet state is not a
  // fresh decision, and one beside `unavailable`, `rejected` or `blocked` is a cache or a fallback.
  const offered = Boolean(dep.available_release) && dep.available_release !== dep.active_release;
  // `rolled_back` lasts only until the next check, so the last recorded result keeps these notices
  // until another update is tried. That record is history: it names no build, and a retry takes
  // whatever is eligible now, which need not be the build that failed.
  const undone: EngineUpdateView = { sentence: 'undone', button: RETRY_UPDATE };
  const unfinished: EngineUpdateView = { sentence: 'unfinished', button: RETRY_UPDATE };
  const settled = (): EngineUpdateView => {
    if (dep.last_result === 'rolled_back') return undone;
    if (dep.last_result === 'preflight_failed') return unfinished;
    // A withdrawal is said when it happens, not beside the switch: what happened, and that it works.
    if (dep.last_result === 'withdrawn_to_fallback') return { sentence: 'replaced', button: CHECK };
    return { sentence: dep.last_checked_at ? 'current' : 'notChecked', button: CHECK };
  };
  switch (dep.update_state) {
    case 'recovering':
    case 'checking':
      return { sentence: 'checking', button: { ...CHECK, busy: true } };
    case 'updating':
    case 'rolling_back':
      return { sentence: 'updating', button: { ...UPDATE, busy: true } };
    case 'withdrawing':
      return { sentence: 'withdrawing', button: { ...UPDATE, busy: true } };
    case 'available':
      return offered ? { sentence: 'available', button: UPDATE } : settled();
    case 'deferred':
      // Nothing failed: the button is the same "do it now" as when the build was first offered.
      return { sentence: 'waitingForIdle', button: UPDATE };
    case 'rolled_back':
      // A candidate that failed its check before the switch was never running: nothing was put back.
      return dep.last_result === 'preflight_failed' ? unfinished : undone;
    case 'failed':
      // A fresh read cannot tell a failed check from a failed update, so the safe next step is a check.
      return { sentence: 'unfinished', button: context.updateFailed ? RETRY_UPDATE : CHECK };
    case 'requires_avibe_update':
      return { sentence: 'needsAvibe', button: CHECK };
    case 'offline':
    case 'unavailable':
    case 'rejected':
    case 'waiting_for_controller':
      return { sentence: 'cannotCheck', button: RETRY_CHECK };
    case 'blocked':
      // Only the one build named with the withdrawal can replace it, never a newer release; a check
      // selects it as soon as it can run here (an engine that was stopped stays stopped until it is
      // needed). Any other cause is not cleared by a release, so that sentence promises nothing and
      // points at Diagnostics.
      return { sentence: WITHDRAWN_REASONS.has(dep.update_reason ?? '') ? 'stopped' : 'cannotStart', button: CHECK };
    case 'idle':
    case 'up_to_date':
    case 'applied':
    default:
      return settled();
  }
}
