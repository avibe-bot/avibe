// Rules behind the Models page — deliberately outside the components, because
// each one is a judgement the UI is not free to improvise: which source in a
// chain is 当前, which chip the resolver has already walked past, which Agents a
// supply failure is actually about (AC-9), and what the header pill is allowed to
// claim. Rules deserve unit tests, and this repo's vitest setup has no DOM, so
// they are plain functions over the contract types and nothing else.
import { offCurrentModelChain, processAvailabilityOf } from './eligibility';
import type {
  AgentSupply,
  ModelSupply,
  RuntimeDependency,
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

// ── The Agent row's supply chain (design.pen 「V6 01」 / 「V6 04」) ─────────
export type ChainTone = 'current' | 'skipped' | 'neutral';

export type ChainChip = {
  sourceId: string;
  /** The source's display name, or the bare id when it no longer resolves. */
  label: string;
  /** 1-based position in this backend's order. */
  position: number;
  tone: ChainTone;
  /** Draws the gold dot: this source cannot serve right now. */
  unhealthy: boolean;
  /** Draws the muted dot: healthy, on the route, and still not launchable here —
   *  this process cannot sign the backend's CLI in with it. Never true at the same
   *  time as `unhealthy`: the row states ONE reason, the one in the way. */
  unavailable: boolean;
};

/**
 * `sources.order` as the numbered chain the Agent row draws.
 *
 * The `skipped` rule is what lets one branch reproduce both frames: V6 01 leaves
 * a cooling relay at position 3 fully legible, while V6 04 dims position 1 after
 * the failover moved 当前 to position 2. So a chip dims only when it is unhealthy
 * AND the resolver has already walked past it — an unhealthy source *after* 当前
 * is a warning about the next failover, not a record of one, and greying it out
 * would claim something that has not happened.
 *
 * A position can be stepped over for a second reason, and the strip has to draw it
 * or the frame contradicts itself: a `native_cli` source this process cannot sign
 * the CLI in with is skipped by the resolver while its own state reads `active`, so
 * without this the chain would show a healthy position 1 and 当前 sitting at 2 with
 * nothing between them to explain the move. It is ONE more disjunct in the same
 * 「walked past」 rule, and one more hue on the same dot — never a second dot and
 * never a second rule, because the question 「why did the resolver move on」 has one
 * answer per position. Health wins the tie: it is the one the user can act on, and
 * the source row next door already names it.
 *
 * That second disjunct is gated on route membership, and the gate is about what the
 * marker CLAIMS. 「Not launchable here」 names a CAUSE for the failover, and a
 * position the server reports as `in_current_model_chain: false` was going to be
 * walked past with the CLI signed in and the state green — it does not carry the
 * selected model at all. Marking it would blame the move on a remedy that changes
 * nothing. Health needs no such gate: 「cannot serve right now」 is true of the
 * source wherever it sits in whosever order. And the gate is on the Agent row only —
 * the all-sources drawer keeps the ungated fact, which is the surface the user goes
 * to in order to act on it.
 *
 * Returns [] in Direct mode: there is no Hub order to draw (AC-7).
 */
export function chainChips(agent: AgentSupply, sources: Source[]): ChainChip[] {
  const order = agent.sources?.order ?? [];
  const currentId = agent.current?.source_id ?? null;
  const currentIndex = currentId ? order.indexOf(currentId) : -1;
  return order.map((sourceId, i) => {
    const source = sources.find((s) => s.id === sourceId);
    const unhealthy = source ? isUnhealthy(source.state) : false;
    const unavailable =
      !unhealthy && !offCurrentModelChain(agent, sourceId) && !processAvailabilityOf(agent, sourceId).runnable;
    const tone: ChainTone =
      sourceId === currentId
        ? 'current'
        : (unhealthy || unavailable) && currentIndex >= 0 && i < currentIndex
          ? 'skipped'
          : 'neutral';
    return { sourceId, label: source?.display_name ?? sourceId, position: i + 1, tone, unhealthy, unavailable };
  });
}

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

/**
 * The supply verdict for every subject the page actually shows, at the FINEST grain
 * it shows one — the projection any page-level headline has to be derived from.
 *
 * `AgentSupply.supply_status` is a rollup for ONE route (「the current selection」 of
 * the Agent named by `selected_by_agent`), so counting backends by it answers a
 * different question than the page asks: a backend with three enabled Agents draws
 * 「claude、pm、codex 供给中断」 in its attribution line and would have had the header
 * say 「1 个 Agent 供给中断」 — a coarser count standing above a finer contradiction
 * the same screen displays, in the same noun.
 *
 * `named_agents` is that finer grain (AC-9: the ENABLED named Agents, each with its
 * own rollup), and `attribution()` already reads it. A backend that publishes none
 * falls back to its own rollup rather than to silence: the row still draws a supply
 * verdict of its own (「供给中断」 when no source is enabled), so dropping it here
 * would lose a warning the page is making — the fallback is per-subject too, never a
 * mix of grains for one subject.
 */
function displayedSupply(hubAgents: AgentSupply[]): (SupplyStatus | null)[] {
  return hubAgents.flatMap((agent) => {
    const named = agent.named_agents ?? [];
    return named.length > 0 ? named.map((a) => a.supply_status) : [agent.supply_status ?? null];
  });
}

export const hasAttribution = (a: SupplyAttribution): boolean =>
  a.interrupted.length > 0 || a.waiting.length > 0 || a.unassignedModels.length > 0;

// ── What a source outage is actually costing right now ──────────────────
export type ChainRoles = {
  /** Listed in at least one Hub Agent's order — an outage here will bite. */
  enrolled: Set<string>;
  /** A Hub resolver has already walked past it: the failover has happened. */
  displaced: Set<string>;
};

/**
 * Read the same chain the Agent rows draw, and answer the two questions the header
 * pill needs: is anything using this source, and has its outage already moved a
 * turn somewhere else.
 *
 * The pill cannot ask "is any source unhealthy" — V6 01 has a cooling relay at
 * position 3 and still reads 一切正常, because nothing was ever served from it.
 * The same shape becomes V6 04's 「已自动切换」 only once the cooling source is one
 * an Agent has fallen off. Position relative to 当前 is the whole difference, and
 * `chainChips` already computes it, so this reuses that judgement instead of
 * growing a second, driftable copy of it.
 */
export function chainRoles(agents: AgentSupply[], sources: Source[]): ChainRoles {
  const enrolled = new Set<string>();
  const displaced = new Set<string>();
  for (const agent of agents) {
    if (agent.mode !== 'hub') continue;
    for (const chip of chainChips(agent, sources)) {
      enrolled.add(chip.sourceId);
      if (chip.tone === 'skipped') displaced.add(chip.sourceId);
    }
  }
  return { enrolled, displaced };
}

// ── The page header's status pill ───────────────────────────────────────
/**
 * What the header pill says. A discriminated result rather than a translated
 * string: the ladder is a rule (worth testing), the copy is i18n (not this
 * module's business).
 */
export type PageStatus =
  | { tone: 'ok'; kind: 'ok'; hubCount: number }
  | { tone: 'neutral'; kind: 'none' }
  | { tone: 'warn'; kind: 'engineDown' }
  | { tone: 'warn'; kind: 'interrupted' | 'waiting'; count: number }
  | { tone: 'warn'; kind: 'needsAction' | 'cooldown'; source: Source; others: number };

/**
 * Worst-first, and every branch is a thing the user can act on:
 *
 *   engine down     nothing on Hub can run at all
 *   interrupted     an Agent has no source left and needs the user
 *   waiting         an Agent is stalled but a source will come back on its own
 *   needs_action    a source someone's chain lists is dead until re-auth / top-up
 *   cooldown        a source stepped aside; the chain covered for it  ← V6 04
 *   ok / none       nothing to report
 *
 * `cooldown` deliberately outranks nothing: it is the only branch that reports a
 * problem the system already handled, which is exactly the V6 04 moment ("额度用完
 * · 已自动切换，恢复后切回") and the reason the pill cannot just be a boolean.
 *
 * Both source-level branches are gated on `chainRoles`, because an unhealthy source
 * is not by itself news — V6 01 draws a cooling relay AND 「一切正常 · 2 个 Agent
 * 已接入中枢」, since every Agent is still served from the head of its chain. The
 * source rows already report their own health; the pill speaks only when the outage
 * is costing someone a turn (`displaced`) or is waiting on a human (`needs_action`
 * under a chain that lists it).
 */
export function pageStatus(
  sources: Source[],
  agents: AgentSupply[],
  runtime: RuntimeDependency | null,
): PageStatus {
  const hubAgents = agents.filter((a) => a.mode === 'hub');
  const { enrolled, displaced } = chainRoles(agents, sources);
  // 「Hub mode」 is not the same question as 「needs the engine」, and only the second
  // one licenses this branch. A `native_cli` source is launched by rewriting the
  // CLI's own config — `model_hub.resolve()` builds that launch with no gateway and
  // swallows an engine-sync failure outright ("Native launch is independent") — so a
  // Hub Agent whose order enrolls only native sources keeps running with the engine
  // down, and telling its owner that nothing works would be false. The engine
  // becomes load-bearing exactly when someone's chain lists a `hub`-channel source.
  // (A Direct-only install has nothing enrolled at all, so this covers it too.)
  const needsEngine = sources.some((s) => s.supply_channel === 'hub' && enrolled.has(s.id));
  if (needsEngine && runtime?.status.health !== 'ok') return { tone: 'warn', kind: 'engineDown' };

  const subjects = displayedSupply(hubAgents);
  const rollup = (status: SupplyStatus) => subjects.filter((s) => s === status).length;
  const interrupted = rollup('interrupted');
  if (interrupted > 0) return { tone: 'warn', kind: 'interrupted', count: interrupted };
  const waiting = rollup('waiting');
  if (waiting > 0) return { tone: 'warn', kind: 'waiting', count: waiting };

  const dead = sources.filter(
    (s) => (s.state.status === 'needs_action' || s.state.status === 'error') && enrolled.has(s.id),
  );
  if (dead.length > 0) return { tone: 'warn', kind: 'needsAction', source: dead[0], others: dead.length - 1 };
  const cooling = sources.filter((s) => s.state.status === 'cooldown' && displaced.has(s.id));
  if (cooling.length > 0) return { tone: 'warn', kind: 'cooldown', source: cooling[0], others: cooling.length - 1 };

  return hubAgents.length === 0
    ? { tone: 'neutral', kind: 'none' }
    : { tone: 'ok', kind: 'ok', hubCount: hubAgents.length };
}
