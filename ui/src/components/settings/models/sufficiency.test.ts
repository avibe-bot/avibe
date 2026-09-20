import { describe, expect, it } from 'vitest';

import { connectOutcome, orderSufficiency } from './sufficiency';
import type { AgentSupply, Source } from './types';

const source = (status: Source['state']['status']): Source => ({
  id: 'src_a', last_discovered_at: null, kind: 'api_key', vendor: 'anthropic', display_name: 'A', protocol: 'anthropic',
  supply_channel: 'hub', billing: 'metered', state: { status, retry_at: status === 'cooldown' ? '2099-01-01T00:00:00Z' : null, detail_key: status === 'needs_action' ? 'models.source.needs_action.oauth_expired' : null }, models: [],
});
const agent = (supply_status: AgentSupply['supply_status']): AgentSupply => ({
  backend: 'claude', cli_present: true, mode: 'hub', menu_kind: 'fixed', sources: { order: ['src_a'], eligibility: [{ source_id: 'src_a', eligible: true, in_current_model_chain: true, process_availability_reason: null }] },
  routes: {}, supply_status, model_supply: [], named_agents: [], builtin_models: [], menu: null,
});

describe('orderSufficiency', () => {
  it('distinguishes empty configuration from configured-but-blocked supply', () => {
    expect(orderSufficiency([], [], agent(null))).toEqual({ kind: 'adopted_none' });
    expect(orderSufficiency(['src_a'], [source('needs_action')], agent('interrupted'))).toEqual({ kind: 'nothing_runnable' });
  });
});

describe('connectOutcome', () => {
  it('uses the server rollup rather than recomputing routing', () => {
    expect(connectOutcome(agent('degraded'), [source('active')])).toBe('degraded');
    expect(connectOutcome(agent('waiting'), [source('cooldown')])).toBe('waiting');
  });
});
