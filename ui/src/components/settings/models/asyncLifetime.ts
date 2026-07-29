// Two decisions that used to live inline in a component, and could therefore only
// be checked by running one: what a drawer does when the server's saved state
// arrives, and what the connect dialog does when a response arrives after it has
// already finished. Both answer the same question — does this async arrival still
// get to change what the user sees — and both got it wrong in a way no unit test
// could reach, which is why they are functions here instead of effect bodies there.
import type { AgentMenu, AgentSupply, OAuthFlow } from './types';

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
 * Every way a response or a timer can reach the connect dialog. A `tick` is the
 * dialog's own deadline check, which also wants to change what is shown.
 */
export type FlowEvent = { kind: 'response'; flow: OAuthFlow } | { kind: 'tick'; overdue: boolean };

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

/** Whether an action ends the flow, so the caller stops polling. */
export const isDone = (action: FlowAction): boolean => action !== 'continue';
