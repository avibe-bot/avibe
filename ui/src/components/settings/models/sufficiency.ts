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
// The honesty rule that shapes the union: what the payload cannot prove is
// `indeterminate`, never optimistic. Two server facts were missing when this module
// was written, and both have since landed — `skipped_by` beside `adopted_by` (the
// eligible-but-skipped complement, which `_adopted_by` filters out server-side) and
// `process_availability_reason` on `sources.eligibility` (whether the local machine
// can launch a source at all, at per-(source, backend) grain). Each is now read by
// the function that was already shaped to take it: `adoptionVerdict` takes the
// complement, `orderSufficiency` asks one predicate per source.
//
// The rule outlives them, because the arrival is what proves it was right: neither
// function grew a branch to accommodate the field, and a payload that still omits
// one keeps returning the same weaker verdict it always did. Silence stays
// `indeterminate` or the weaker claim, never a false alarm and never a guess.
import { processAvailabilityOf } from './eligibility';
import { isUnhealthy } from './supply';
import type { AdoptedBy, AgentBackend, AgentSupply, SkippedBy, Source } from './types';

/**
 * The closed verdict. Not every grain can produce every member — each function
 * documents its own subset — but there is exactly one vocabulary.
 *
 * `adopted_none` is the empty-membership case at either grain: no backend adopted the
 * source, or no source is enabled for the backend. It is deliberately distinct from
 * `nothing_runnable`, because the remedy is 「add one」 rather than 「fix one」.
 *
 * `skipped_all` is that same 「add one」 remedy with the orders NAMED. It is a separate
 * member rather than a field on `adopted_none` because only one grain can ever reach
 * it: `orderSufficiency` has no complement to read.
 */
export type Sufficiency =
  | { kind: 'covered' }
  | { kind: 'partly_skipped'; backends: AgentBackend[] }
  | { kind: 'skipped_all'; backends: AgentBackend[] }
  | { kind: 'nothing_runnable' }
  | { kind: 'adopted_none' }
  | { kind: 'indeterminate' };

/**
 * Did the new source actually reach everyone it should have?
 *
 * Returns `adopted_none` | `skipped_all` | `partly_skipped` | `covered` |
 * `indeterminate`.
 *
 * `adopted_by.length` decides how much of the answer is good news, and NOTHING else:
 * that array never says who was left out, at any length. So the complement is read on
 * both sides of the branch. Zero adopters with a non-empty complement is not a corner
 * case but the normal shape of an install where every eligible backend keeps a
 * hand-picked order — the exact case the field was added for, and the one where
 * naming the orders saves the most work. Deciding it from `adopted_by` alone would be
 * this module's own header defect, an emptiness test standing in for an adequacy one.
 */
export function adoptionVerdict(
  adoptedBy: readonly AdoptedBy[] | null | undefined,
  skippedBy?: readonly SkippedBy[] | null,
): Sufficiency {
  if (!adoptedBy) return { kind: 'indeterminate' };
  const skipped = skippedBy?.map((s) => s.backend) ?? [];
  // Empty is a closed fact — the server sent the list and nobody is on it. What it
  // is NOT is a reason to stop reading: 「nobody took it」 and 「these three orders
  // left it out」 share one remedy and differ entirely in where to go do it.
  if (adoptedBy.length === 0) {
    return skipped.length > 0 ? { kind: 'skipped_all', backends: skipped } : { kind: 'adopted_none' };
  }
  // Non-empty proves someone took it and nothing about who did not.
  if (!skippedBy) return { kind: 'indeterminate' };
  return skipped.length === 0 ? { kind: 'covered' } : { kind: 'partly_skipped', backends: skipped };
}

/**
 * Can this backend's enabled order serve the next turn?
 *
 * Returns `adopted_none` | `nothing_runnable` | `covered` | `indeterminate`. Takes the
 * ids rather than the agent's own order so the drawer can ask about the order the user
 * is CURRENTLY editing, which is the order the warning is about; the agent comes along
 * for the server facts that hang off it, never for its saved order.
 *
 * Two independent ways to be unable to serve, and the verdict needs BOTH: the
 * source's own health (`isUnhealthy`) and whether it can be launched here at all
 * (`processAvailabilityOf`). The second was the documented v4 caveat — a healthy
 * `native_cli` source this process cannot sign the CLI in with reports itself
 * perfectly able to serve — and it closed when `process_availability_reason` arrived
 * on `sources.eligibility` at exactly this grain, per (source, backend). `covered`
 * now means 「a source can be reached AND launched」; an agent whose payload omits
 * the field falls back to the old, weaker claim rather than to a false alarm.
 */
export function orderSufficiency(
  orderIds: readonly string[] | null | undefined,
  sources: readonly Source[] | null | undefined,
  agent: Pick<AgentSupply, 'sources'>,
): Sufficiency {
  if (!orderIds) return { kind: 'indeterminate' };
  if (orderIds.length === 0) return { kind: 'adopted_none' };
  if (!sources) return { kind: 'indeterminate' };

  const byId = new Map(sources.map((s) => [s.id, s]));
  const resolved = orderIds.map((id) => byId.get(id)).filter((s): s is Source => s !== undefined);
  const servable = (s: Source) => !isUnhealthy(s.state) && processAvailabilityOf(agent, s.id).runnable;
  if (resolved.some(servable)) return { kind: 'covered' };
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

  const verdict = orderSufficiency(agent.sources?.order, sources, agent);
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
