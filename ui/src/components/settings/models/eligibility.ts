// Per-Agent source eligibility — read from the server, never re-derived (spec
// §4.4).
//
// This replaces `isSourceEligible` in `menus/identifiers.ts`, a UI mirror of the
// backend predicate that was escalated at review time and is deleted with this
// module. The mirror could not be right: eligibility depends on the
// `subscription_hub_experimental` flag and on recorded consent, neither of which
// the UI can see, so it was guaranteed to drift and to offer rows the live API
// rejects. Contract v3 publishes `sources.eligibility` on
// `GET /api/models/agents`, and this file is the only place the UI reads it.
import type { AgentSupply, EligibilityReasonKey, ProcessAvailabilityReason, Source } from './types';

export type Eligibility = { eligible: boolean; reasonKey: EligibilityReasonKey | null };

const ELIGIBLE: Eligibility = { eligible: true, reasonKey: null };
const INELIGIBLE: Eligibility = { eligible: false, reasonKey: null };

/**
 * Whether `sourceId` may serve this Agent, and why not when it may not.
 *
 * Total over every id, including one the payload does not mention:
 * - `sources === null` — Direct mode. Hub eligibility is undefined there and no
 *   Hub surface (order drawer, model menu, mapping targets) is reachable.
 * - listed in `eligibility` — the server's answer, verbatim.
 * - absent from `eligibility` but present in `order` — the server validated it
 *   into the order and §4.4 forbids an ineligible id there, so it is eligible
 *   with no reason to render. Covers a server that omits the optional field and
 *   the race where a source is created between the `/sources` and `/agents`
 *   reads.
 * - otherwise ineligible with no reason: graying a row out is better than
 *   offering an 启用 the PUT would reject with `invalid_source_order`.
 */
export function eligibilityOf(agent: Pick<AgentSupply, 'sources'>, sourceId: string): Eligibility {
  const sources = agent.sources;
  if (!sources) return INELIGIBLE;
  const entry = sources.eligibility?.find((e) => e.source_id === sourceId);
  if (entry) return entry.eligible ? ELIGIBLE : { eligible: false, reasonKey: entry.reason_key ?? null };
  return sources.order.includes(sourceId) ? ELIGIBLE : INELIGIBLE;
}

/** Whether this source can be LAUNCHED for this Agent right now, and why not. */
export type ProcessAvailability = { runnable: boolean; reasonKey: ProcessAvailabilityReason | null };

const RUNNABLE: ProcessAvailability = { runnable: true, reasonKey: null };

/**
 * The second half of 「can this source take the next turn」, and the half the source
 * itself cannot answer: `native_cli` sources are launched by rewriting the CLI's own
 * config, so one whose CLI is not installed on this machine is unrunnable while its
 * credential, its models and its `state` all read perfectly healthy.
 *
 * Per (source, BACKEND), like eligibility beside it — the server computes it from
 * `_unavailable_native_sources(config, backend)`, so the same source is runnable for
 * one Agent and not for another. That is why it is read here off the Agent's own
 * inventory and can never live on the backend-agnostic source row.
 *
 * Total over every id, and the default is RUNNABLE rather than blocked: an absent
 * row, a server that omits the optional field, and Direct mode all mean 「nothing is
 * claimed」, and the surfaces this feeds (the chain's dim chip, the drawer's
 * 「nothing runnable」 warning) would otherwise invent an outage out of silence.
 */
export function processAvailabilityOf(
  agent: Pick<AgentSupply, 'sources'>,
  sourceId: string,
): ProcessAvailability {
  const reasonKey = agent.sources?.eligibility?.find((e) => e.source_id === sourceId)?.process_availability_reason;
  return reasonKey ? { runnable: false, reasonKey } : RUNNABLE;
}

/** The subset of `sources` this Agent may use — the menu/mapping surfaces' input. */
export function eligibleSources(sources: Source[], agent: Pick<AgentSupply, 'sources'>): Source[] {
  return sources.filter((s) => eligibilityOf(agent, s.id).eligible);
}
