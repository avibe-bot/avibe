// One owner for the question five surfaces were each answering on their own:
// 「will the next turn actually run?」
//
// The class this closes is an EMPTINESS test standing in for an ADEQUACY test. Five
// sites asked `collection.length === 0` and read a non-empty collection as success —
// AdoptionNote, both creation dialogs' auto-close timers, the order drawer's warning,
// and `connectOutcome`. Three different arrays, one question, five answers, and the
// review had named two of them. So the remedy is not five guards: it is one function
// that returns a CLOSED verdict, and no caller left holding a length.
//
// The honesty rule that shapes the union: what today's payload cannot prove is
// `indeterminate`, never optimistic. Two fields would make more of it provable —
// `skipped_by` beside `adopted_by` (the eligible-but-skipped complement, which
// `_adopted_by` filters out server-side) and process availability at backend grain
// (contracts v4 carries it only on `agent-chain`). Both are server-side and out of
// this lane. Until they land, the verdicts that depend on them stay indeterminate and
// the surfaces keyed on them stay quiet — which is the correct behaviour for a
// confirmation that would otherwise be able to lie. Their arrival is pure wiring:
// `adoptionVerdict` already takes the complement, and `orderSufficiency` already asks
// one predicate about each source.
import { isUnhealthy } from './supply';
import type { AdoptedBy, AgentBackend, AgentSupply, Source } from './types';

/**
 * The closed verdict. Not every grain can produce every member — each function
 * documents its own subset — but there is exactly one vocabulary.
 *
 * `adopted_none` is the empty-membership case at either grain: no backend adopted the
 * source, or no source is enabled for the backend. It is deliberately distinct from
 * `nothing_runnable`, because the remedy is 「add one」 rather than 「fix one」.
 */
export type Sufficiency =
  | { kind: 'covered' }
  | { kind: 'partly_skipped'; backends: AgentBackend[] }
  | { kind: 'nothing_runnable' }
  | { kind: 'adopted_none' }
  | { kind: 'indeterminate' };

/**
 * The eligible-but-skipped complement of `adopted_by`.
 *
 * Typed locally on purpose: `types.ts` mirrors the FROZEN contracts and never runs
 * ahead of them. When the field ships, this type moves there and the parameter below
 * starts being fed — no logic changes, and the tests that already cover the branch
 * stop being the only thing exercising it.
 */
export type SkippedBy = {
  backend: AgentBackend;
  /** v2's only cause: the backend keeps a `custom` order, which the server never
   *  extends. An INELIGIBLE backend is not 「skipped」 — it was never a candidate. */
  reason: 'custom-order-omission';
};

/**
 * Did the new source actually reach everyone it should have?
 *
 * Returns `adopted_none` | `partly_skipped` | `covered` | `indeterminate`.
 */
export function adoptionVerdict(
  adoptedBy: readonly AdoptedBy[] | null | undefined,
  skippedBy?: readonly SkippedBy[] | null,
): Sufficiency {
  if (!adoptedBy) return { kind: 'indeterminate' };
  // Empty is a closed fact — the server sent the list and nobody is on it.
  if (adoptedBy.length === 0) return { kind: 'adopted_none' };
  // Non-empty proves someone took it and nothing about who did not.
  if (!skippedBy) return { kind: 'indeterminate' };
  if (skippedBy.length === 0) return { kind: 'covered' };
  return { kind: 'partly_skipped', backends: skippedBy.map((s) => s.backend) };
}

/**
 * Can this backend's enabled order serve the next turn?
 *
 * Returns `adopted_none` | `nothing_runnable` | `covered` | `indeterminate`. Takes the
 * ids rather than the agent so the drawer can ask about the order the user is CURRENTLY
 * editing, which is the order the warning is about.
 *
 * Known v4 caveat: `isUnhealthy` reads the source's own state, and a healthy
 * `native_cli` source can still be unrunnable at chain grain (the CLI process is not
 * installed on this machine). v4 publishes that fact only on `agent-chain`, whose grain
 * is (agent, model). So `covered` here means 「a source reports itself able to serve」,
 * which is the strongest claim this payload supports; the verdict tightens on its own
 * the day availability arrives at backend grain.
 */
export function orderSufficiency(
  orderIds: readonly string[] | null | undefined,
  sources: readonly Source[] | null | undefined,
): Sufficiency {
  if (!orderIds) return { kind: 'indeterminate' };
  if (orderIds.length === 0) return { kind: 'adopted_none' };
  if (!sources) return { kind: 'indeterminate' };

  const byId = new Map(sources.map((s) => [s.id, s]));
  const resolved = orderIds.map((id) => byId.get(id)).filter((s): s is Source => s !== undefined);
  if (resolved.some((s) => !isUnhealthy(s.state))) return { kind: 'covered' };
  // Every id we could resolve is down. If some could not be resolved, the two reads
  // disagree and an unknown source is not a broken one.
  return resolved.length === orderIds.length ? { kind: 'nothing_runnable' } : { kind: 'indeterminate' };
}

/** Outcomes that own a warning string under `settings.models.supply.*`. */
export const SUPPLY_WARNINGS = ['degraded', 'waiting', 'interrupted', 'noSources', 'nothingRunnable'] as const;
export type SupplyWarning = (typeof SUPPLY_WARNINGS)[number];

/**
 * What to tell the user right after they switch a backend to the Hub.
 *
 * `indeterminate` is not a warning and not a success claim — it is 「the switch took」
 * and nothing more, which is all a caller without the source inventory may say.
 */
export type ConnectOutcome = 'connected' | 'failed' | 'indeterminate' | SupplyWarning;

export const isSupplyWarning = (outcome: ConnectOutcome): outcome is SupplyWarning =>
  (SUPPLY_WARNINGS as readonly string[]).includes(outcome);

export function connectOutcome(agent: AgentSupply, sources: readonly Source[] | null | undefined): ConnectOutcome {
  // The PATCH echoed something other than hub: the switch did not take.
  if (agent.mode !== 'hub') return 'failed';

  const verdict = orderSufficiency(agent.sources?.order, sources);
  // Its own remedy, and it outranks any grade — an empty order has nothing to grade.
  if (verdict.kind === 'adopted_none') return 'noSources';

  switch (agent.supply_status) {
    case 'interrupted':
    case 'waiting':
    case 'degraded':
      return agent.supply_status;
    // The server resolved the selection against the order and found it fine. Trust it
    // over our own read of the inventory: it graded the model, we only see statuses.
    case 'ok':
      return 'connected';
    default:
      break;
  }

  // `supply_status: null` means the server had no model to resolve, not that the order
  // is fine. It is silence, so the order's own runnability is the only fact left.
  if (verdict.kind === 'nothing_runnable') return 'nothingRunnable';
  return verdict.kind === 'covered' ? 'connected' : 'indeterminate';
}
