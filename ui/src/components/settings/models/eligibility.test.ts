// §4.4 eligibility is the server's answer. These cases pin the four branches the
// UI is allowed to take — the reason this module replaced the deleted
// `isSourceEligible` mirror rather than moving it.
import { describe, expect, it } from 'vitest';

import { eligibilityOf, eligibleSources, offCurrentModelChain } from './eligibility';
import type { AgentSupply, Source } from './types';

const source = (id: string): Source => ({
  id,
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: id,
  protocol: 'anthropic',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  last_discovered_at: null,
  models: [],
});

type Agent = Pick<AgentSupply, 'sources'>;

const agent = (sources: AgentSupply['sources']): Agent => ({ sources });

describe('eligibilityOf', () => {
  it('returns the server’s verdict verbatim, reason included', () => {
    const a = agent({
      policy: 'custom',
      order: [],
      eligibility: [
        { source_id: 'src_ok', eligible: true, reason_key: null },
        { source_id: 'src_no', eligible: false, reason_key: 'models.eligibility.subscription_wrong_client' },
      ],
    });
    expect(eligibilityOf(a, 'src_ok')).toEqual({ eligible: true, reasonKey: null });
    expect(eligibilityOf(a, 'src_no')).toEqual({
      eligible: false,
      reasonKey: 'models.eligibility.subscription_wrong_client',
    });
  });

  it('grays out an ineligible row whose reason the payload omitted', () => {
    const a = agent({ policy: 'custom', order: [], eligibility: [{ source_id: 'src_x', eligible: false }] });
    expect(eligibilityOf(a, 'src_x')).toEqual({ eligible: false, reasonKey: null });
  });

  // §4.4 forbids an ineligible id in the order, so the order itself is an answer
  // when the optional field is missing (or when a source is created between the
  // /sources and /agents reads).
  it('trusts the order when `eligibility` does not mention the id', () => {
    const a = agent({ policy: 'follow', order: ['src_in'] });
    expect(eligibilityOf(a, 'src_in')).toEqual({ eligible: true, reasonKey: null });
    expect(eligibilityOf(a, 'src_out')).toEqual({ eligible: false, reasonKey: null });
  });

  // AC-7: Hub eligibility is undefined for a Direct backend, and every Hub
  // surface that would consume it is unreachable there.
  it('is ineligible for everything in Direct mode (AC-7)', () => {
    expect(eligibilityOf(agent(null), 'src_anything')).toEqual({ eligible: false, reasonKey: null });
  });
});

describe('offCurrentModelChain', () => {
  // A claim about the ROUTE, not about the source: the server builds it from the
  // eligible sources carrying the selected model, before health or runnability
  // narrows them. Only an explicit `false` is a claim; `null` (nothing selected) and
  // an absent field are silence, and reading silence as exclusion would retract
  // markers the chain strip has always been right to draw.
  it('speaks only when the server says false', () => {
    const a = agent({
      policy: 'follow',
      order: ['src_on', 'src_off', 'src_unknown'],
      eligibility: [
        { source_id: 'src_on', eligible: true, in_current_model_chain: true },
        { source_id: 'src_off', eligible: true, in_current_model_chain: false },
        { source_id: 'src_unknown', eligible: true, in_current_model_chain: null },
      ],
    });
    expect(offCurrentModelChain(a, 'src_off')).toBe(true);
    expect(offCurrentModelChain(a, 'src_on')).toBe(false);
    expect(offCurrentModelChain(a, 'src_unknown')).toBe(false);
  });

  it('is silent for a row the payload never mentions, and in Direct mode', () => {
    expect(offCurrentModelChain(agent({ policy: 'follow', order: ['src_a'] }), 'src_a')).toBe(false);
    expect(offCurrentModelChain(agent(null), 'src_a')).toBe(false);
  });
});

describe('eligibleSources', () => {
  it('keeps inventory order and drops what the server refused', () => {
    const sources = [source('src_a'), source('src_b'), source('src_c')];
    const a = agent({
      policy: 'custom',
      order: ['src_c'],
      eligibility: [
        { source_id: 'src_a', eligible: true, reason_key: null },
        { source_id: 'src_b', eligible: false, reason_key: 'models.eligibility.subscription_wrong_client' },
      ],
    });
    expect(eligibleSources(sources, a).map((s) => s.id)).toEqual(['src_a', 'src_c']);
  });

  it('offers nothing at all in Direct mode', () => {
    expect(eligibleSources([source('src_a')], agent(null))).toEqual([]);
  });
});
