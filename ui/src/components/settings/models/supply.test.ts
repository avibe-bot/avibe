import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import {
  attribution,
  hasAttribution,
  healthyButUnrunnable,
  isUnhealthy,
  needsAttention,
} from './supply';
import type { AgentSupply, Source, SourceState } from './types';

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
const DEAD: SourceState = {
  status: 'needs_action',
  retry_at: null,
  detail_key: 'models.source.needs_action.oauth_expired',
};

const hubAgent = (over: Partial<AgentSupply> = {}): AgentSupply => ({
  backend: 'claude',
  mode: 'hub',
  menu_kind: 'fixed',
  selected_by_agent: 'claude',
  selected_model_id: 'claude-opus-4-6',
  selected_model_explicit: true,
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
    selected_by_agent: null,
    selected_model_id: null,
    selected_model_explicit: false,
    sources: null,
    supply_status: null,
    model_supply: null,
  });

const cannotLaunch = (...ids: string[]) =>
  ids.map((id) => ({
    source_id: id,
    eligible: true,
    process_availability_reason: 'native_cli_unavailable' as const,
  }));

const cannotLaunchOffRoute = (...ids: string[]) =>
  cannotLaunch(...ids).map((eligibility) => ({ ...eligibility, in_current_model_chain: false }));

describe('isUnhealthy', () => {
  it('treats only active and standby as healthy', () => {
    expect(isUnhealthy(ACTIVE)).toBe(false);
    expect(isUnhealthy(STANDBY)).toBe(false);
    expect(isUnhealthy(COOLING)).toBe(true);
    expect(isUnhealthy(DEAD)).toBe(true);
    expect(isUnhealthy({ status: 'error' })).toBe(true);
  });
});

describe('needsAttention', () => {
  it('distinguishes self-healing cooldowns from billing decisions', () => {
    expect(needsAttention(COOLING)).toBe(false);
    expect(needsAttention(EXHAUSTED)).toBe(true);
  });

  it('includes every status that never heals unattended', () => {
    expect(needsAttention(DEAD)).toBe(true);
    expect(needsAttention({ status: 'error' })).toBe(true);
    expect(needsAttention(ACTIVE)).toBe(false);
  });
});

describe('healthyButUnrunnable', () => {
  const agentWith = (eligibility: NonNullable<AgentSupply['sources']>['eligibility']) =>
    hubAgent({ sources: { policy: 'follow', order: ['src_a', 'src_b'], eligibility } });

  it('retracts a healthy row promise this machine cannot keep', () => {
    expect(healthyButUnrunnable(agentWith(cannotLaunch('src_b')), nativeSource('src_b', ACTIVE))).toBe(true);
  });

  it('leaves the segment to health when the source is not serving', () => {
    const agent = agentWith(cannotLaunch('src_b'));
    expect(healthyButUnrunnable(agent, nativeSource('src_b', COOLING))).toBe(false);
    expect(healthyButUnrunnable(agent, nativeSource('src_b', DEAD))).toBe(false);
  });

  it('says nothing about a source this machine can launch', () => {
    expect(healthyButUnrunnable(agentWith([]), nativeSource('src_b', ACTIVE))).toBe(false);
    expect(healthyButUnrunnable(hubAgent({ sources: null }), nativeSource('src_b', ACTIVE))).toBe(false);
  });

  it('does not read route membership as process availability', () => {
    expect(
      healthyButUnrunnable(agentWith(cannotLaunchOffRoute('src_c')), nativeSource('src_c', ACTIVE)),
    ).toBe(true);
  });

  it('is absent from the ordering-only drawer', () => {
    const drawer = readFileSync(join(__dirname, 'SourceOrderDrawer.tsx'), 'utf8');
    expect(drawer).not.toMatch(/healthyButUnrunnable|processAvailabilityOf/);
  });
});

describe('attribution (AC-9)', () => {
  it('names only the Agents whose own rollup is affected', () => {
    const agent = hubAgent({
      named_agents: [
        { name: 'claude', effective_model_id: 'claude-opus-4-6', supply_status: 'interrupted' },
        { name: 'pm', effective_model_id: 'claude-sonnet-4-6', supply_status: 'waiting' },
        { name: 'reviewer', effective_model_id: 'claude-opus-4-6', supply_status: 'ok' },
      ],
    });
    expect(attribution(agent)).toEqual({ interrupted: ['claude'], waiting: ['pm'], unassignedModels: [] });
  });

  it('attributes an empty unassigned chain to the model alone', () => {
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

  it('never double-counts a model an Agent runs', () => {
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
