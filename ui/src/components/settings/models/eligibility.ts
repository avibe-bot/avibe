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
import type { AgentSupply, EligibilityReasonKey, Source } from './types';

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

/** The subset of `sources` this Agent may use — the menu/mapping surfaces' input. */
export function eligibleSources(sources: Source[], agent: Pick<AgentSupply, 'sources'>): Source[] {
  return sources.filter((s) => eligibilityOf(agent, s.id).eligible);
}
