// The supply JOURNEYS' decisions — which remedy a stopped source actually has,
// what the server's answer to a repair means, and whether 试跑 has anything real
// to run. Same shape and same reason as `supply.ts` / `sufficiency.ts`: each of
// these is a judgement the components are not free to improvise, this repo's
// vitest has no DOM, so they are plain functions over the contract types.
//
// §4.5 is what makes the first one load-bearing: `needs_action` 「carries a
// detail_key naming the cause, so the row can offer ONE TAP to fix it (re-auth,
// top up, replace key) instead of a dead-end error string」. A row that renders
// the cause and stops is the dead end that sentence forbids.
import type {
  AgentBackend,
  AgentSupply,
  ProbeResult,
  Source,
  SourceDetailKey,
  SourceRepaired,
  SourceState,
  SupplyGap,
} from './types';

/**
 * The two statuses a repair can recover from, and the exact set the server calls
 * `recovered` over (`state.status in {needs_action, error}`).
 *
 * Deliberately NOT `needsAttention`, which is wider by one: an 额度用完 cooldown
 * earns the gold sub-line but heals itself on the billing cycle, and offering it
 * a repair button would put a one-tap fix on the one blocker that does not need
 * one. Deliberately NOT `isUnhealthy` either, which is wider by all three
 * cooldowns. Three predicates, three different questions.
 */
export const wasBlocked = (state: SourceState): boolean =>
  state.status === 'needs_action' || state.status === 'error';

// ── Which remedy a source HAS (independent of whether it needs one) ──────
// These three mirror the server's own preconditions, one for one, so the menu
// offers exactly the actions the routes accept. The elective entries read these;
// the blocked row's inline button reads `repairAction` below.

/** `POST …/reauth` accepts subscriptions on BOTH channels — a hub-held grant is
 *  re-obtained by logging in again just as a native CLI login is. */
export const canReauth = (source: Source): boolean => source.kind === 'subscription';

/** `PUT …/credential` is hub-channel api_key only, and needs an existing
 *  credential to replace (`kind != api_key || channel != hub || !credential_ref`
 *  ⇒ `discovery_failed`). */
export const canReplaceKey = (source: Source): boolean =>
  source.kind === 'api_key' && source.supply_channel === 'hub' && Boolean(source.credential_ref);

/** `POST …/test` is the contract's ONE recovery endpoint (「v3 does not add a
 *  second 'recover' endpoint」) and rejects native sources, which have nothing to
 *  re-discover. */
export const canRetest = (source: Source): boolean => source.supply_channel === 'hub';

/** What a stopped row offers as its one tap. */
export type RepairKind = 'reauth' | 'replace_key' | 'retest';

/** Causes whose fix is a NEW credential, held by Avibe: the grant or the key
 *  itself stopped working, and only replacing it can help. */
const CREDENTIAL_CAUSES: ReadonlySet<SourceDetailKey> = new Set<SourceDetailKey>([
  'models.source.needs_action.oauth_expired',
  'models.source.needs_action.credential_revoked',
]);

/**
 * The one-tap remedy for a blocked source, or null when it genuinely has none.
 *
 * Two rules, no per-cause table:
 *
 *  1. The credential itself failed → replace it, by the route that owns that
 *     source's kind of credential (login for a subscription, key for an api_key).
 *  2. Anything else → the cause lives UPSTREAM (balance run out, account
 *     restricted, an unclassified failure). Avibe cannot fix it and must not
 *     pretend to; what it can do is stop guessing and re-check, which is exactly
 *     what the recovery test is for. The row's affordance is 「handled it —
 *     retry」, not 「top up here」, because no contract field carries a vendor
 *     billing URL and a button that cannot go there would be the dead end again.
 *
 * Returns null only where no route applies at all (a native source, whose
 * blockers are cleared by its own CLI). A healthy source has no remedy to offer
 * because it has no problem — the caller renders nothing, not a disabled button.
 */
export function repairAction(source: Source): RepairKind | null {
  if (!wasBlocked(source.state)) return null;
  const credentialFailed = Boolean(source.state.detail_key && CREDENTIAL_CAUSES.has(source.state.detail_key));
  if (credentialFailed) {
    if (canReauth(source)) return 'reauth';
    if (canReplaceKey(source)) return 'replace_key';
    return null;
  }
  return canRetest(source) ? 'retest' : null;
}

// ── What the server's repair answer MEANS ────────────────────────────────

/**
 * The verdict over a repair tail — the single owner of 「did that fix it?」, so
 * the toast, the dialog's closing behaviour and the gap report cannot disagree.
 *
 * Both inputs are the server's own: `recovered` is its judgement that the source
 * had been blocked, and `interrupted_pairs` is the output of the very guard that
 * would have refused the write. Neither is re-derived here — a client that
 * recomputed 「is anything stranded?」 from `/agents` would be answering a
 * different question (today's supply) than the one it renders.
 *
 * `gaps` outranks `repaired` on purpose: a forced replacement that fixed this
 * source while stranding another Agent's model is not a success story, and the
 * report is the whole reason the user was asked to confirm.
 */
export type RepairOutcome =
  | { kind: 'repaired' }
  | { kind: 'refreshed' }
  | { kind: 'gaps'; gaps: SupplyGap[] };

export const repairOutcome = (tail: SourceRepaired): RepairOutcome =>
  tail.interrupted_pairs.length > 0
    ? { kind: 'gaps', gaps: tail.interrupted_pairs }
    : tail.recovered
      ? { kind: 'repaired' }
      : { kind: 'refreshed' };

/**
 * Whether the journey may close itself.
 *
 * Provable from the tail alone, unlike the adoption auto-close next door: that
 * one stays dormant because `covered` needs a `skipped_by` the contract does not
 * carry yet, whereas 「nothing was stranded」 is a field the server sends. So this
 * closes on a clean repair and holds the dialog open on `gaps`, which is a
 * report the user has to read.
 */
export const repairSettles = (outcome: RepairOutcome): boolean => outcome.kind !== 'gaps';

// ── 试跑 (dry run) ───────────────────────────────────────────────────────

/**
 * Whether a dry run has anything real to run for this Agent.
 *
 * `none` covers direct mode (no `src_*` identity to report — the route refuses
 * with `direct_mode`, AC-7) and a null `current`, which is precisely
 * waiting/interrupted: there is no head, the page already says so with the
 * remedy attached, and a probe would only re-report it as a failure.
 *
 * No `model` in the request on purpose. `current.model_id` is the RESOLVED id
 * (post-mapping), while the route treats its `model` argument as the REQUESTED
 * one and maps it again — passing the head back would resolve twice and probe a
 * different model than the drawer displays. Omitted, the server uses the very
 * `_requested_model(agent)` that produced `current`, so the probe and the row
 * agree by construction rather than by a client-side guess.
 *
 * Note there is no `native` branch: 「don't fake a native test」 is honoured by
 * the SERVER, which answers a native_cli head with its CLI readiness and
 * `latency_ms: null` instead of sending a request. A client-side branch would
 * have to invent that readiness — the copy keys on the null latency instead.
 */
export type DryRunPlan = { kind: 'probe'; backend: AgentBackend } | { kind: 'none' };

/** A source's name for copy, falling back to its id — the same tolerance
 *  `chainChips` keeps for an id that no longer resolves. */
const nameOf = (sources: Source[], id: string): string =>
  sources.find((s) => s.id === id)?.display_name || id;

export function dryRunPlan(agent: AgentSupply): DryRunPlan {
  if (agent.mode !== 'hub') return { kind: 'none' };
  if (!agent.current) return { kind: 'none' };
  return { kind: 'probe', backend: agent.backend };
}

/**
 * The probe's own verdict, named for copy. `reachable` is the discriminator the
 * contract pins (`error` is null iff reachable), so nothing else is consulted.
 *
 * A null `latencyMs` is not a missing measurement, it is the answer for a head
 * the Hub does not carry the request for: the caller says 「可用」 without a
 * number rather than printing a zero it did not measure.
 */
export type DryRunOutcome =
  | { kind: 'ok'; sourceName: string; latencyMs: number | null }
  | { kind: 'failed'; sourceName: string; detailKey: SourceDetailKey | null };

export function dryRunOutcome(probe: ProbeResult, sources: Source[]): DryRunOutcome {
  const sourceName = nameOf(sources, probe.source_id);
  return probe.reachable
    ? { kind: 'ok', sourceName, latencyMs: probe.latency_ms }
    : { kind: 'failed', sourceName, detailKey: probe.error };
}
