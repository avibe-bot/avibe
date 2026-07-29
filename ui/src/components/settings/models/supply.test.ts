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
