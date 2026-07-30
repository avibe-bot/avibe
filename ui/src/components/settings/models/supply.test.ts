// The Models page's rules, tested where they live: as pure functions over the
// contract types. Each `it` names the frame or the acceptance criterion that
// decided the behaviour, because every one of these is a judgement someone can
// otherwise "simplify" back into a boolean.
import { describe, expect, it } from 'vitest';

import {
  attribution,
  chainChips,
  chainRoles,
  hasAttribution,
  isUnhealthy,
  needsAttention,
  pageStatus,
} from './supply';
import type { AgentSupply, RuntimeDependency, Source, SourceState } from './types';

const source = (id: string, state: SourceState, name = id): Source => ({
  id,
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: name,
  protocol: 'anthropic',
  supply_channel: 'hub',
  billing: 'metered',
  state,
  models: [],
});

/** A subscription served by rewriting the CLI's own config — no gateway, so no
 *  dependency on the engine (`model_hub.resolve()`'s native_cli branch). */
const nativeSource = (id: string, state: SourceState, name = id): Source => ({
  ...source(id, state, name),
  kind: 'subscription',
  supply_channel: 'native_cli',
});

const ACTIVE: SourceState = { status: 'active', retry_at: null, detail_key: null };
const STANDBY: SourceState = { status: 'standby', retry_at: null, detail_key: null };
const COOLING: SourceState = {
  status: 'cooldown',
  retry_at: '2026-07-30T09:00:00Z',
  detail_key: 'models.source.cooldown.timeout',
};
const EXHAUSTED: SourceState = {
  status: 'cooldown',
  retry_at: '2026-07-31T00:00:00Z',
  detail_key: 'models.source.cooldown.quota_exhausted',
};
const DEAD: SourceState = { status: 'needs_action', retry_at: null, detail_key: 'models.source.needs_action.oauth_expired' };

const hubAgent = (over: Partial<AgentSupply> = {}): AgentSupply => ({
  backend: 'claude',
  mode: 'hub',
  menu_kind: 'fixed',
  selected_by_agent: null,
  selected_model_id: 'claude-opus-4-6',
  current: { model_id: 'claude-opus-4-6', source_id: 'src_a', channel: 'hub' },
  sources: { policy: 'follow', order: ['src_a', 'src_b'], eligibility: [] },
  supply_status: 'ok',
  model_supply: [],
  named_agents: [],
  mappings: [],
  menu: null,
  builtin_models: [],
  standard_vendors: null,
  ...over,
});

const directAgent = (): AgentSupply =>
  hubAgent({
    backend: 'opencode',
    mode: 'direct',
    current: null,
    sources: null,
    supply_status: null,
    model_supply: null,
  });

/** The server's per-(source, backend) verdict that this machine cannot launch the
 *  source. `eligible: true` on purpose — the source MAY serve this backend, which
 *  is what makes the second question a separate one. */
const cannotLaunch = (...ids: string[]) =>
  ids.map((id) => ({ source_id: id, eligible: true, process_availability_reason: 'native_cli_unavailable' as const }));

/** The same verdict on a source the selected model does not come from at all.
 *  `in_current_model_chain` is a claim about the ROUTE, built from the eligible
 *  sources carrying the model BEFORE health or runnability narrows them. */
const cannotLaunchOffRoute = (...ids: string[]) =>
  cannotLaunch(...ids).map((e) => ({ ...e, in_current_model_chain: false }));

const runtime = (health: RuntimeDependency['status']['health']): RuntimeDependency => ({
  manifest: { name: 'cliproxyapi', version: '1.0.0', source_sha: 'sha', assets: [] },
  status: { installed_version: '1.0.0', verified: true, listening: null, health, last_check: null },
});

describe('isUnhealthy', () => {
  it('counts exactly the two serving statuses as healthy (§4.5)', () => {
    expect(isUnhealthy(ACTIVE)).toBe(false);
    expect(isUnhealthy(STANDBY)).toBe(false);
    expect(isUnhealthy(COOLING)).toBe(true);
    expect(isUnhealthy(DEAD)).toBe(true);
    expect(isUnhealthy({ status: 'error', retry_at: null, detail_key: 'models.source.error.unclassified' })).toBe(true);
  });
});

describe('needsAttention', () => {
  // V6 01 draws a timed-out relay with a GRAY sub-line; V6 04 draws an exhausted
  // subscription with a GOLD one. Gold means "a person has to do something".
  it('stays gray for weather and turns gold for money (V6 01 vs V6 04)', () => {
    expect(needsAttention(COOLING)).toBe(false);
    expect(needsAttention({ status: 'cooldown', retry_at: null, detail_key: 'models.source.cooldown.rate_limited' })).toBe(
      false,
    );
    expect(needsAttention(EXHAUSTED)).toBe(true);
  });

  it('is gold for every status that never heals unattended', () => {
    expect(needsAttention(DEAD)).toBe(true);
    expect(needsAttention({ status: 'error', retry_at: null, detail_key: 'models.source.error.unclassified' })).toBe(true);
  });

  it('leaves a healthy source alone', () => {
    expect(needsAttention(ACTIVE)).toBe(false);
  });
});

describe('chainChips', () => {
  const sources = [source('src_a', ACTIVE, 'ChatGPT Plus'), source('src_b', COOLING, 'relay.example')];

  it('numbers the chain in this backend’s own order and marks 当前', () => {
    const chips = chainChips(hubAgent(), sources);
    expect(chips.map((c) => [c.position, c.label, c.tone])).toEqual([
      [1, 'ChatGPT Plus', 'current'],
      [2, 'relay.example', 'neutral'],
    ]);
  });

  // V6 01: an unhealthy source BELOW 当前 is a warning about the next failover,
  // not a record of one, so it keeps full contrast and only takes the gold dot.
  it('does not dim an unhealthy source the resolver has not reached (V6 01)', () => {
    const chips = chainChips(hubAgent(), sources);
    expect(chips[1]).toMatchObject({ tone: 'neutral', unhealthy: true });
  });

  // V6 04: the same shape AFTER the failover — 当前 moved to position 2, so
  // position 1 is now a record of what was skipped.
  it('dims an unhealthy source the resolver has walked past (V6 04)', () => {
    const agent = hubAgent({
      current: { model_id: 'claude-opus-4-6', source_id: 'src_b', channel: 'hub' },
    });
    const chips = chainChips(agent, [source('src_a', EXHAUSTED, 'ChatGPT Plus'), source('src_b', ACTIVE, 'relay.example')]);
    expect(chips.map((c) => c.tone)).toEqual(['skipped', 'current']);
  });

  it('keeps an unresolvable id visible under its bare id', () => {
    const chips = chainChips(hubAgent(), [source('src_a', ACTIVE)]);
    expect(chips[1]).toMatchObject({ sourceId: 'src_b', label: 'src_b', unhealthy: false });
  });

  // AC-7: a Direct backend has no Hub order at all.
  it('draws nothing in Direct mode (AC-7)', () => {
    expect(chainChips(directAgent(), sources)).toEqual([]);
  });

  // The second reason a position gets stepped over, and the one the source itself
  // cannot report: its credential, its models and its state all read perfectly
  // healthy, and the CLI that would serve it is not usable on this machine.
  it('marks a healthy source this machine cannot launch, without calling it unhealthy', () => {
    const agent = hubAgent({
      sources: { policy: 'follow', order: ['src_a', 'src_b'], eligibility: cannotLaunch('src_b') },
    });
    const chips = chainChips(agent, [source('src_a', ACTIVE), nativeSource('src_b', ACTIVE)]);
    expect(chips[1]).toMatchObject({ unhealthy: false, unavailable: true });
  });

  it('dims it once the resolver has walked past it, exactly like a broken one', () => {
    const agent = hubAgent({
      current: { model_id: 'claude-opus-4-6', source_id: 'src_b', channel: 'hub' },
      sources: { policy: 'follow', order: ['src_a', 'src_b'], eligibility: cannotLaunch('src_a') },
    });
    const chips = chainChips(agent, [nativeSource('src_a', ACTIVE), source('src_b', ACTIVE)]);
    expect(chips.map((c) => c.tone)).toEqual(['skipped', 'current']);
  });

  // One row, ONE reason. The two are not mutually exclusive server-side, and the
  // row renders a single dot, so health takes the tie: it is the actionable one,
  // and its gold dot is named by the source row's own state chip.
  it('states one reason per row, and health wins the tie', () => {
    const agent = hubAgent({
      sources: { policy: 'follow', order: ['src_a', 'src_b'], eligibility: cannotLaunch('src_b') },
    });
    const chips = chainChips(agent, [source('src_a', ACTIVE), nativeSource('src_b', DEAD)]);
    expect(chips[1]).toMatchObject({ unhealthy: true, unavailable: false });
  });

  it('reads a payload that says nothing as launchable', () => {
    // An older server omits the optional field entirely. Silence must not draw a
    // dim chain — every position would look stepped over.
    const agent = hubAgent({ sources: { policy: 'follow', order: ['src_a', 'src_b'] } });
    const chips = chainChips(agent, [source('src_a', ACTIVE), nativeSource('src_b', ACTIVE)]);
    expect(chips.map((c) => c.unavailable)).toEqual([false, false]);
  });

  // The marker names a CAUSE for the failover, so it may only be spent on a
  // position the failover was ever going to consider. This one does not carry the
  // selected model: the resolver walks past it with the CLI signed in and the state
  // green, and pointing at a remedy that changes nothing about the route is worse
  // than pointing at nothing.
  it('does not blame availability for a position the model never came from', () => {
    const agent = hubAgent({
      current: { model_id: 'claude-opus-4-6', source_id: 'src_b', channel: 'hub' },
      sources: { policy: 'follow', order: ['src_a', 'src_b'], eligibility: cannotLaunchOffRoute('src_a') },
    });
    const chips = chainChips(agent, [nativeSource('src_a', ACTIVE), source('src_b', ACTIVE)]);
    expect(chips[0]).toMatchObject({ unavailable: false, unhealthy: false, tone: 'neutral' });
  });

  it('keeps the marker when the server claims nothing about the route', () => {
    // `in_current_model_chain` is null with nothing selected and absent from a server
    // that predates the field. Silence is not exclusion: with no selected model the
    // order IS the route, so this position really was stepped over for this reason.
    const agent = hubAgent({
      selected_model_id: null,
      sources: {
        policy: 'follow',
        order: ['src_a', 'src_b'],
        eligibility: cannotLaunch('src_b').map((e) => ({ ...e, in_current_model_chain: null })),
      },
    });
    const chips = chainChips(agent, [source('src_a', ACTIVE), nativeSource('src_b', ACTIVE)]);
    expect(chips[1]).toMatchObject({ unavailable: true });
  });

  it('still dims an off-route source that is genuinely broken', () => {
    // The gate is on the availability disjunct only. 「Cannot serve right now」 is
    // true of the source wherever it sits, and its gold dot is an early warning
    // about the next failover rather than a reading of this one's route.
    const agent = hubAgent({
      current: { model_id: 'claude-opus-4-6', source_id: 'src_b', channel: 'hub' },
      sources: { policy: 'follow', order: ['src_a', 'src_b'], eligibility: cannotLaunchOffRoute('src_a') },
    });
    const chips = chainChips(agent, [nativeSource('src_a', DEAD), source('src_b', ACTIVE)]);
    expect(chips[0]).toMatchObject({ unhealthy: true, unavailable: false, tone: 'skipped' });
  });
});

describe('chainRoles', () => {
  const sources = [source('src_a', ACTIVE), source('src_b', COOLING)];

  it('enrolls every id a Hub chain lists', () => {
    const { enrolled, displaced } = chainRoles([hubAgent()], sources);
    expect([...enrolled].sort()).toEqual(['src_a', 'src_b']);
    expect([...displaced]).toEqual([]);
  });

  it('displaces only what a resolver has already fallen off', () => {
    const agent = hubAgent({ current: { model_id: 'm', source_id: 'src_b', channel: 'hub' } });
    const { displaced } = chainRoles([agent], [source('src_a', EXHAUSTED), source('src_b', ACTIVE)]);
    expect([...displaced]).toEqual(['src_a']);
  });

  it('ignores Direct-mode backends entirely', () => {
    const { enrolled } = chainRoles([directAgent()], sources);
    expect(enrolled.size).toBe(0);
  });

  // `skipped` now has a second cause, and this set is shared with the page pill —
  // so the widening has to be shown to stay inside the surface that asked for it.
  it('displaces a source the machine cannot launch, and the pill still says nothing', () => {
    const agent = hubAgent({
      current: { model_id: 'm', source_id: 'src_b', channel: 'hub' },
      sources: { policy: 'follow', order: ['src_a', 'src_b'], eligibility: cannotLaunch('src_a') },
    });
    const inventory = [nativeSource('src_a', ACTIVE), nativeSource('src_b', ACTIVE)];
    expect([...chainRoles([agent], inventory).displaced]).toEqual(['src_a']);
    // `pageStatus` reads `displaced` only through `state.status === 'cooldown'`, and
    // an unlaunchable source is healthy by construction — the two cannot overlap.
    expect(pageStatus(inventory, [agent], runtime('ok'))).toEqual({ tone: 'ok', kind: 'ok', hubCount: 1 });
  });
});

describe('attribution (AC-9)', () => {
  it('names the Agents whose own rollup says so, and no others', () => {
    const agent = hubAgent({
      named_agents: [
        { name: 'claude', effective_model_id: 'claude-opus-4-6', supply_status: 'interrupted' },
        { name: 'pm', effective_model_id: 'claude-sonnet-4-6', supply_status: 'waiting' },
        { name: 'reviewer', effective_model_id: 'claude-opus-4-6', supply_status: 'ok' },
      ],
    });
    expect(attribution(agent)).toEqual({ interrupted: ['claude'], waiting: ['pm'], unassignedModels: [] });
  });

  // The other half of AC-9: a ticked model nobody runs is attributed to the MODEL
  // and to no Agent — the failure a per-backend rollup gets wrong.
  it('attributes an empty chain under an unassigned model to the model alone', () => {
    const agent = hubAgent({
      named_agents: [{ name: 'claude', effective_model_id: 'claude-opus-4-6', supply_status: 'ok' }],
      model_supply: [
        { model_id: 'claude-opus-4-6', chain_length: 2 },
        { model_id: 'claude-haiku-4-5', chain_length: 0 },
      ],
    });
    expect(attribution(agent)).toEqual({
      interrupted: [],
      waiting: [],
      unassignedModels: ['claude-haiku-4-5'],
    });
  });

  it('never double-counts a model an Agent does run', () => {
    const agent = hubAgent({
      named_agents: [{ name: 'claude', effective_model_id: 'claude-haiku-4-5', supply_status: 'interrupted' }],
      model_supply: [{ model_id: 'claude-haiku-4-5', chain_length: 0 }],
    });
    expect(attribution(agent)).toEqual({ interrupted: ['claude'], waiting: [], unassignedModels: [] });
  });

  it('reports nothing for a Direct backend', () => {
    expect(hasAttribution(attribution(directAgent()))).toBe(false);
  });
});

describe('pageStatus', () => {
  it('reports the engine only when a Hub backend depends on it', () => {
    const sources = [source('src_a', ACTIVE)];
    expect(pageStatus(sources, [hubAgent()], runtime('down'))).toEqual({ tone: 'warn', kind: 'engineDown' });
    expect(pageStatus(sources, [directAgent()], runtime('down'))).toEqual({ tone: 'neutral', kind: 'none' });
  });

  // 「on Hub」 is not the same question as 「needs the engine」. A native_cli source is
  // launched by rewriting the CLI's own config — no gateway, and resolve() swallows
  // an engine-sync failure there on purpose — so this Agent keeps working and the
  // pill must not tell its owner otherwise.
  it('stays quiet with the engine down when the whole chain is served natively', () => {
    const sources = [nativeSource('src_a', ACTIVE), nativeSource('src_b', STANDBY)];
    expect(pageStatus(sources, [hubAgent()], runtime('down'))).toEqual({ tone: 'ok', kind: 'ok', hubCount: 1 });
  });

  it('reports the engine as soon as one enrolled source is served through it', () => {
    const sources = [nativeSource('src_a', ACTIVE), source('src_b', STANDBY)];
    expect(pageStatus(sources, [hubAgent()], runtime('down'))).toEqual({ tone: 'warn', kind: 'engineDown' });
  });

  it('ignores a hub-channel source no chain has enrolled', () => {
    const sources = [nativeSource('src_a', ACTIVE), nativeSource('src_b', ACTIVE), source('src_orphan', ACTIVE)];
    expect(pageStatus(sources, [hubAgent()], runtime('down'))).toEqual({ tone: 'ok', kind: 'ok', hubCount: 1 });
  });

  // An empty order routes nothing through the engine either — and it is the state
  // the row now reports as a coming failure, not as a Direct fallback.
  it('says nothing about the engine for a Hub backend with no enabled source', () => {
    const agent = hubAgent({ sources: { policy: 'custom', order: [], eligibility: [] }, current: null });
    expect(pageStatus([source('src_a', ACTIVE)], [agent], runtime('down'))).toEqual({
      tone: 'ok',
      kind: 'ok',
      hubCount: 1,
    });
  });

  it('puts an interrupted Agent above a waiting one', () => {
    const agents = [
      hubAgent({ supply_status: 'waiting' }),
      hubAgent({ backend: 'codex', supply_status: 'interrupted' }),
    ];
    expect(pageStatus([source('src_a', ACTIVE)], agents, runtime('ok'))).toEqual({
      tone: 'warn',
      kind: 'interrupted',
      count: 1,
    });
  });

  // ── The grain the pill counts at ─────────────────────────────────────
  // `supply_status` answers for ONE route; the attribution line answers per named
  // Agent, and the pill uses the same noun 「Agent」. So the pill has to count what
  // the line names, or the two contradict each other on one screen.

  it('counts interrupted named Agents, not the backends they sit on', () => {
    const agent = hubAgent({
      supply_status: 'ok',
      named_agents: [
        { name: 'claude', effective_model_id: 'claude-opus-4-6', supply_status: 'interrupted' },
        { name: 'pm', effective_model_id: 'claude-sonnet-4-6', supply_status: 'interrupted' },
        { name: 'reviewer', effective_model_id: 'claude-haiku-4-5', supply_status: 'ok' },
      ],
    });
    expect(pageStatus([source('src_a', ACTIVE)], [agent], runtime('ok'))).toEqual({
      tone: 'warn',
      kind: 'interrupted',
      count: 2,
    });
  });

  it('does not let the route-level rollup speak over the per-Agent projection', () => {
    // The finer projection wins in BOTH directions — a coarser 供给中断 cannot
    // headline a page whose every named Agent is fine.
    const agent = hubAgent({
      supply_status: 'interrupted',
      named_agents: [{ name: 'claude', effective_model_id: 'claude-opus-4-6', supply_status: 'ok' }],
    });
    expect(pageStatus([source('src_a', ACTIVE)], [agent], runtime('ok'))).toEqual({
      tone: 'ok',
      kind: 'ok',
      hubCount: 1,
    });
  });

  it('keeps the warning for a Hub backend that publishes no named Agent', () => {
    // No enabled Agent on this backend, so there is no finer projection to read —
    // the row's own verdict is the finest thing displayed, and must still be heard.
    const agent = hubAgent({ supply_status: 'interrupted', named_agents: [] });
    expect(pageStatus([source('src_a', ACTIVE)], [agent], runtime('ok'))).toEqual({
      tone: 'warn',
      kind: 'interrupted',
      count: 1,
    });
  });

  // V6 01: a cooling relay nobody has fallen off still reads 一切正常. The source
  // row reports its own health; the pill speaks when the outage costs a turn.
  it('stays green while an unhealthy source is below every 当前 (V6 01)', () => {
    const sources = [source('src_a', ACTIVE), source('src_b', COOLING)];
    expect(pageStatus(sources, [hubAgent()], runtime('ok'))).toEqual({ tone: 'ok', kind: 'ok', hubCount: 1 });
  });

  // V6 04: the same cooling source, one failover later.
  it('reports the handled failover once a chain has fallen off the source (V6 04)', () => {
    const sources = [source('src_a', EXHAUSTED, 'ChatGPT Plus'), source('src_b', ACTIVE)];
    const agent = hubAgent({ current: { model_id: 'm', source_id: 'src_b', channel: 'hub' } });
    expect(pageStatus(sources, [agent], runtime('ok'))).toEqual({
      tone: 'warn',
      kind: 'cooldown',
      source: sources[0],
      others: 0,
    });
  });

  it('reports a dead source any chain lists, even at the tail', () => {
    const sources = [source('src_a', ACTIVE), source('src_b', DEAD, 'Anthropic API Key')];
    expect(pageStatus(sources, [hubAgent()], runtime('ok'))).toMatchObject({ kind: 'needsAction', others: 0 });
  });

  it('ignores a dead source no chain lists', () => {
    const sources = [source('src_a', ACTIVE), source('src_b', ACTIVE), source('src_orphan', DEAD)];
    expect(pageStatus(sources, [hubAgent()], runtime('ok'))).toEqual({ tone: 'ok', kind: 'ok', hubCount: 1 });
  });

  it('counts the rest of a bad batch into the same pill', () => {
    const agent = hubAgent({ sources: { policy: 'follow', order: ['src_a', 'src_b', 'src_c'], eligibility: [] } });
    const sources = [source('src_a', ACTIVE), source('src_b', DEAD), source('src_c', DEAD)];
    expect(pageStatus(sources, [agent], runtime('ok'))).toMatchObject({ kind: 'needsAction', others: 1 });
  });

  it('says nothing at all when no backend is on the Hub', () => {
    expect(pageStatus([source('src_a', DEAD)], [directAgent()], null)).toEqual({ tone: 'neutral', kind: 'none' });
  });
});

// `connectOutcome` and `isSupplyWarning` moved to `sufficiency.ts` (and their tests
// with them): deciding whether the next turn can run is one question, and this module
// answered it with `order.length` while four other sites answered it with a length of
// their own. What is left here is the per-source predicate that verdict consumes.
