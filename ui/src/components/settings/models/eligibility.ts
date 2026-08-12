// Per-Agent source eligibility — read from the server, never re-derived (spec
// §4.4).
//
// This replaces `isSourceEligible` in `menus/identifiers.ts`, a UI mirror of the
// backend predicate that was escalated at review time and is deleted with this
// module. The mirror could not be right: eligibility is server-owned inventory
// state, so a client reconstruction was guaranteed to drift and offer rows the
// live API rejects. Contract v5 publishes `sources.eligibility` on
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
 *   Hub surface (order drawer, model menu, route targets) is reachable.
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
 * config, so one this process cannot sign that CLI in with is unrunnable while its
 * credential, its models and its `state` all read perfectly healthy.
 *
 * 「Unrunnable」 is NOT 「the executable is missing」, and nothing here may say it is:
 * `_default_native_cli_ready` also refuses when an `ANTHROPIC_*` / `OPENAI_*`
 * override, a stored API key, a custom base URL or a foreign `model_provider` has
 * claimed the CLI's sign-in — configurations where the CLI is installed and works
 * fine. So this reader carries the server's reason key and no diagnosis of its own,
 * and the copy it feeds names the sign-in rather than the binary.
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

/**
 * Does the server positively say this source does NOT carry the selected model?
 *
 * `in_current_model_chain` is a claim about the ROUTE, not about the source: the
 * server builds it from `resolution.matching_sources`, the eligible sources whose
 * inventory carries the selected model, BEFORE health or runnability narrows them
 * down. So a `false` means the resolver was never going to stop at this position,
 * whatever else is true of it or of this machine.
 *
 * Only an explicit `false` counts. The field is `null` when nothing is selected —
 * there is no route to be off — and absent from a server that predates it, and in
 * both of those the order itself is the route the resolver walks. Reading silence
 * as exclusion would retract a claim the strip has always been right to make.
 */
export function offCurrentModelChain(agent: Pick<AgentSupply, 'sources'>, sourceId: string): boolean {
  const entry = agent.sources?.eligibility?.find((e) => e.source_id === sourceId);
  return entry?.in_current_model_chain === false;
}

/** The subset of `sources` this Agent may use — the menu/route surfaces' input. */
export function eligibleSources(sources: Source[], agent: Pick<AgentSupply, 'sources'>): Source[] {
  return sources.filter((s) => eligibilityOf(agent, s.id).eligible);
}
