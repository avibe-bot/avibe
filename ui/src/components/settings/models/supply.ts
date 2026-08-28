// Rules behind the Models page — deliberately outside the components, because
// each one is a judgement the UI is not free to improvise: which Agents a supply
// failure is actually about (AC-9), and whether a source is usable here. Rules
// deserve unit tests, and this repo's vitest setup has no DOM, so they are plain
// functions over the contract types and nothing else.
import { processAvailabilityOf } from './eligibility';
import type {
  AgentSupply,
  ModelSupply,
  NamedAgentSupply,
  Source,
  SourceState,
  SupplyStatus,
} from './types';

/**
 * Not serving normally. `active` and `standby` are the only two healthy statuses
 * (§4.5); `cooldown` heals itself, `needs_action` and `error` never do — but all
 * three mean "cannot take the next turn right now", which is the one question
 * every surface here asks.
 *
 * The single predicate behind the source row's state chip, its amber sub-line and
 * the chain's gold dot, so those three can never disagree about one source.
 */
export const isUnhealthy = (state: SourceState): boolean =>
  state.status !== 'active' && state.status !== 'standby';

/** The one cooldown reason that is about money rather than weather. */
const QUOTA_EXHAUSTED = 'models.source.cooldown.quota_exhausted';

/**
 * Worth reading, not just worth reporting — what earns the gold sub-line.
 *
 * `isUnhealthy` is too broad for that: V6 01 draws a timed-out relay with a GRAY
 * sub-line and V6 04 draws an exhausted subscription with a GOLD one, and the
 * difference is whether a human has anything to do about it. `needs_action` and
 * `error` wait on a person by definition; among the self-healing cooldowns only
 * 额度用完 does — it is out for the rest of the billing cycle, and topping up or
 * accepting metered spend is a decision. A rate limit or a network blip is weather:
 * the state chip already says 暂不可用, and colouring it too would make the one row
 * that needs reading look like the four that don't.
 */
export const needsAttention = (state: SourceState): boolean =>
  state.status === 'needs_action' || state.status === 'error' || state.detail_key === QUOTA_EXHAUSTED;

// What to say after putting a backend on the Hub used to live here as
// `connectOutcome`, deciding from `(agent.sources?.order ?? []).length === 0`. That
// is one of five sites that answered 「can the next turn run?」 with an emptiness
// test, so the decision moved to `sufficiency.ts`, which owns it for all five.
// Nothing here re-derives it; `isUnhealthy` above is the one predicate it consumes.

/**
 * Is this row's 供 … 全系 a promise this machine cannot keep — healthy, and still
 * not something the Agent's process can be launched against?
 *
 * A model count is a promise, and the two answers that can retract it compete for
 * one segment of copy. Health wins, exactly as it does in the strip above: a
 * cooling or broken source is the one the user can act on, and its state chip has
 * already raised the question. So this is only about the row that looks fine.
 *
 * Ungated by route membership, and deliberately so. A row in a source list is
 * answering 「what would I get by turning this on」. A 未启用 source sits outside
 * `agent.sources.order`, which is the only list `in_current_model_chain` is an
 * answer about, so a gate there would read an ORDER fact as a MODEL one.
 *
 * Per (source, backend), like `processAvailabilityOf` it reads — never a property
 * of the source row on its own.
 */
export const healthyButUnrunnable = (agent: Pick<AgentSupply, 'sources'>, source: Source): boolean =>
  !isUnhealthy(source.state) && !processAvailabilityOf(agent, source.id).runnable;

export type AgentGroupStatus = SupplyStatus | 'unconfigured' | 'unused';

/** Summarize the enabled Agents using one backend without inventing a backend selection. */
export const agentGroupStatus = (agents: NamedAgentSupply[]): AgentGroupStatus => {
  if (agents.length === 0) return 'unused';
  if (agents.every((agent) =>
    agent.effective_model_id === null || agent.route_reason === 'route_unconfigured'
  )) return 'unconfigured';

  const statuses = agents.map((agent) => agent.supply_status);
  if (statuses.every((status) => status === 'ok')) return 'ok';
  if (statuses.some((status) => status === 'ok' || status === 'degraded')) return 'degraded';
  if (statuses.some((status) => status === 'waiting')) return 'waiting';
  return 'interrupted';
};

// ── AC-9: who a supply problem is about ─────────────────────────────────
export type SupplyAttribution = {
  /** Named Agents whose effective model cannot be served at all. */
  interrupted: string[];
  /** Named Agents waiting for a cooling source to come back on its own. */
  waiting: string[];
  /** Selectable models with an empty chain that NO named Agent runs. */
  unassignedModels: string[];
};

const EMPTY_ATTRIBUTION: SupplyAttribution = { interrupted: [], waiting: [], unassignedModels: [] };

/**
 * Resolve a supply problem to the Agents it affects, from the server's per-Agent
 * projection — never by assuming every Agent on the backend is affected.
 *
 * That distinction IS AC-9: a source failing under a model the user ticked but
 * assigned to nobody must be attributed to the model **and no Agent**, while the
 * same failure under an Agent's effective model names exactly that Agent. Both
 * halves are read here, from `named_agents` (whose `supply_status` the resolver
 * computed) and `model_supply` (whose `chain_length: 0` is the honest 「ticked but
 * nothing supplies it」 state).
 *
 * A model an Agent does run is deliberately NOT reported as unassigned even when
 * its chain is empty — that Agent's own `supply_status` already carries it, and
 * saying both would double-count one failure.
 */
export function attribution(agent: AgentSupply): SupplyAttribution {
  const named = agent.named_agents ?? [];
  const modelSupply: ModelSupply[] = agent.model_supply ?? [];
  if (named.length === 0 && modelSupply.length === 0) return EMPTY_ATTRIBUTION;
  const withStatus = (status: SupplyStatus) =>
    named.filter((a) => a.supply_status === status).map((a) => a.name);
  const claimed = new Set(
    named.map((a) => a.effective_model_id).filter((id): id is string => typeof id === 'string'),
  );
  return {
    interrupted: withStatus('interrupted'),
    waiting: withStatus('waiting'),
    unassignedModels: modelSupply
      .filter((m) => m.chain_length === 0 && !claimed.has(m.model_id))
      .map((m) => m.model_id),
  };
}

export const hasAttribution = (a: SupplyAttribution): boolean =>
  a.interrupted.length > 0 || a.waiting.length > 0 || a.unassignedModels.length > 0;
