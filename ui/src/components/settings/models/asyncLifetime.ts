// Async ownership rules extracted from the Models components so interleavings can
// be exercised directly: which request may land, when a drawer re-seeds, and
// whether a connect-flow transition may change an already terminal view.
import type { AgentMenu, AgentSupply, OAuthFlow } from './types';

// ── Latest async result ────────────────────────────────────────────────────
/**
 * Owns request ordering and the only path that may land a result. Callers start
 * reads; they never reconstruct whether a response is still current.
 */
export const createLatestAsyncAuthority = <T>(land: (value: T) => void) => {
  let latestRequest = 0;

  return {
    run: async (read: () => Promise<T>): Promise<'landed' | 'stale'> => {
      const request = ++latestRequest;
      try {
        const value = await read();
        if (request !== latestRequest) return 'stale';
        land(value);
        return 'landed';
      } catch (error) {
        if (request !== latestRequest) return 'stale';
        throw error;
      }
    },
  };
};

// ── The drawers' seed ──────────────────────────────────────────────────────
// A drawer seeds its editable state from the server's saved state, and needs to
// know when that saved state MOVED. Comparing the props object is useless (every
// page refresh builds a new one), so each drawer reduces the fields it seeds from
// to a signature and compares that. The separator is NUL because these parts are
// ids: a separator that cannot occur inside one is what keeps ['a','b'] from
// colliding with ['a b'].
const sig = (parts: readonly (string | number | boolean)[]): string => parts.join('\u0000');

/** 来源顺序 drawer: the per-backend policy + ordered subset it edits. */
export const savedSourcesKey = (agent: AgentSupply): string =>
  sig([agent.sources?.policy ?? 'follow', ...(agent.sources?.order ?? [])]);

/** 映射 drawer: the stored overrides its draft is seeded from. */
export const savedMappingsKey = (agent: AgentSupply): string =>
  sig((agent.mappings ?? []).flatMap((m) => [m.builtin_id, m.target_model_id, m.enabled]));

/** OpenCode 模型菜单 drawer: the stored view + checked identifiers. */
export const savedMenuKey = (menu: AgentMenu | null | undefined): string =>
  sig([menu?.view ?? 'featured', ...(menu?.checked ?? [])]);

/** What a drawer remembers about the saved state it last seeded from. */
export type SeedState = { seeded: boolean; baseline: string | null };

export const initialSeedState: SeedState = { seeded: false, baseline: null };

/**
 * Whether a drawer holding `state` must re-seat itself now that the server's
 * saved state reads `authoritative`.
 *
 * Seeds on open, and again whenever the saved state MOVED under it. Keying on
 * open alone is not enough: the page unmounts a drawer when it closes, so a
 * drawer closed and reopened while its own PUT is in flight seeds from the
 * pre-save props and then never learns that the save landed — and its next edit
 * diffs against that superseded snapshot and writes the old state back over the
 * save the user already saw succeed.
 *
 * A refetch that changed nothing is still inert, which is the whole reason this
 * compares content rather than the props object: that is what protects an edit in
 * flight from a background page refresh. When the saved state genuinely moved,
 * re-seating is the only honest option — a draft built on a base the server has
 * replaced can only be written back as a regression.
 */
export const seedStep = (state: SeedState, authoritative: string): { state: SeedState; reseed: boolean } => {
  if (state.seeded && state.baseline === authoritative) return { state, reseed: false };
  return { state: { seeded: true, baseline: authoritative }, reseed: true };
};

// ── The OAuth dialog's terminal ordering ───────────────────────────────────
/**
 * Every way flow state can reach the connect dialog. A `tick` is the dialog's
 * own deadline check; `reset` starts a new attempt from an unsettled view.
 */
export type FlowEvent =
  | { kind: 'reset' }
  | { kind: 'response'; flow: OAuthFlow }
  | { kind: 'tick'; overdue: boolean };

/**
 * What the caller must DO about an event. The side effects (toast, refetch,
 * auto-close, stop polling) stay in the component; only the decision lives here.
 *
 *   continue  not finished, keep polling
 *   succeed   first terminal success: fire the connected side effects, once
 *   fail      first terminal failure: show the flow's error
 *   timeout   the deadline passed with the flow unfinished
 *   ignore    stale arrival — the dialog already finished
 */
export type FlowAction = 'continue' | 'succeed' | 'fail' | 'timeout' | 'ignore';

/** What the dialog shows, plus the latch that says it has already finished. */
export type FlowView = { flow: OAuthFlow | null; settled: boolean };

export const flowStep = (view: FlowView, event: FlowEvent): { view: FlowView; action: FlowAction } => {
  if (event.kind === 'reset') return { view: { flow: null, settled: false }, action: 'continue' };
  // Nothing gets to change a finished flow — checked before the event is read, so
  // it holds for EVERY entry point (a poll still in flight when success landed, a
  // paste submit racing that poll, and the deadline tick, which used to stamp
  // `failed` over a success on a dialog left open because nothing adopted the
  // source).
  if (view.settled) return { view, action: 'ignore' };
  if (event.kind === 'tick') {
    if (!event.overdue) return { view, action: 'continue' };
    return {
      view: { flow: view.flow ? { ...view.flow, state: 'failed' } : null, settled: true },
      action: 'timeout',
    };
  }
  const flow = event.flow;
  if (flow.state === 'success') return { view: { flow, settled: true }, action: 'succeed' };
  // `settled` means terminal, not successful: a failed flow is just as finished,
  // and a later arrival has just as little business reopening it.
  if (flow.state === 'failed' || flow.state === 'cancelled') return { view: { flow, settled: true }, action: 'fail' };
  return { view: { flow, settled: false }, action: 'continue' };
};

export type FlowAuthority = {
  current: () => FlowView;
  transition: (event: FlowEvent) => ReturnType<typeof flowStep>;
};

/**
 * Owns the complete view, including its terminal latch, and is the only path
 * that may land a new one. Keeping only `flow` at the landing site would let a
 * caller silently drop `settled` and reopen an already terminal flow.
 */
export const createFlowAuthority = (land: (view: FlowView) => void): FlowAuthority => {
  let current: FlowView = { flow: null, settled: false };

  return {
    current: () => current,
    transition: (event) => {
      const step = flowStep(current, event);
      current = step.view;
      land(current);
      return step;
    },
  };
};

/** Whether an action ends the flow, so the caller stops polling. */
export const isDone = (action: FlowAction): boolean => action !== 'continue';
