import { describe, expect, it } from 'vitest';

import { evidenceChip, modelCapabilities, pendingChecks, probeTotal, type ProbeTarget } from './hostedTools';
import type { HostedEvidence, HostedToolCandidate } from './types';

const evidence = (overrides: Partial<HostedEvidence>): HostedEvidence => ({
  status: 'unknown',
  stale: null,
  basis: null,
  observed_at: null,
  detail: null,
  changes_at: null,
  ...overrides,
});

const AT = '2026-10-13T10:20:00Z';

const candidate = (sourceId: string, modelId: string, call: HostedEvidence): HostedToolCandidate => ({
  source_id: sourceId,
  model_id: modelId,
  call,
  replay: null,
  billing: 'metered',
  price: null,
  probe_estimate: null,
});

describe('hosted-tool evidence projection', () => {
  // Every evidence shape the status read can carry (hosted-tools-status.schema.json),
  // and the chip each one owes the user. A wire-stale record reads `unknown` for
  // routing, so a projection keyed on `status` alone would hide that it was
  // ever checked, and the engine-upgrade re-check would have no row to point at.
  const cases: [string, HostedEvidence, ReturnType<typeof evidenceChip>][] = [
    ['no record', evidence({}), null],
    ['verified on the current engine', evidence({ status: 'verified', basis: 'probe', observed_at: AT }), { kind: 'native', aged: false, at: AT }],
    ['verified, older than 30 days', evidence({ status: 'verified', stale: 'age', basis: 'probe', observed_at: AT }), { kind: 'native', aged: true, at: AT }],
    ['taken on another engine', evidence({ status: 'unknown', stale: 'wire', basis: 'probe', observed_at: AT }), { kind: 'engineUpgraded', aged: false, at: AT }],
    ['refused by a probe', evidence({ status: 'unsupported', basis: 'probe', observed_at: AT, detail: '400 · unsupported_tool_type' }), { kind: 'unsupported', aged: false, at: AT }],
    // An old refusal still routes, but it is counted in `reverify.aged`, so its
    // row has to say which record that count is about.
    ['refused by a probe older than 30 days', evidence({ status: 'unsupported', stale: 'age', basis: 'probe', observed_at: AT, detail: '400 · unsupported_tool_type' }), { kind: 'unsupported', aged: true, at: AT }],
    ['refused by a real request', evidence({ status: 'unsupported', basis: 'observed', observed_at: AT, detail: '400 · unsupported_tool_type' }), { kind: 'unsupported', aged: false, at: AT }],
  ];
  it('MH-HOSTED-UI-002: gives every evidence shape the chip it owes the user', () => {
    for (const [name, call, chip] of cases) expect(evidenceChip(call), name).toEqual(chip);
  });

  it('MH-HOSTED-UI-003: totals a check only from what the server estimates, and never counts unknown as zero', () => {
    const at = (estimate: HostedToolCandidate['probe_estimate']): ProbeTarget => ({
      tool: 'web_search',
      candidate: { ...candidate('src_relay001', 'gpt-6-astra', evidence({})), probe_estimate: estimate },
    });

    expect(probeTotal([at({ basis: 'list', usd: 0.02 }), at({ basis: 'included' }), at({ basis: 'list', usd: 0.03 })]))
      .toEqual({ basis: 'list', usd: 0.05 });
    // One unknown estimate makes the whole set's cost unknown.
    expect(probeTotal([at({ basis: 'list', usd: 0.02 }), at(null)])).toBeNull();
    // A check run only on subscriptions adds no charge.
    expect(probeTotal([at({ basis: 'included' })])).toEqual({ basis: 'included' });
  });

  it('MH-HOSTED-UI-013: offers to check every pair with no current answer, and never an image', () => {
    const verified = evidence({ status: 'verified', basis: 'probe', observed_at: AT });
    const status = {
      tools: [
        { name: 'web_search' as const, enabled: false, executor: null, bound: null, candidates: [
          candidate('src_relay001', 'gpt-6-astra', evidence({})),
          candidate('src_relay001', 'gpt-6-sol', verified),
          candidate('src_relay001', 'gpt-6-luna', evidence({ status: 'unknown', stale: 'wire', basis: 'probe', observed_at: AT })),
          candidate('src_relay001', 'gpt-6-terra', evidence({ status: 'verified', stale: 'age', basis: 'probe', observed_at: AT })),
        ] },
        { name: 'image_generation' as const, enabled: false, executor: null, bound: null, candidates: [candidate('src_relay001', 'gpt-6.1-sol', evidence({}))] },
      ],
      reverify: { engine_upgraded: 1, aged: 1 },
    };

    expect(pendingChecks(status).map(({ tool, candidate: c }) => `${tool}:${c.model_id}`))
      .toEqual(['web_search:gpt-6-astra', 'web_search:gpt-6-luna', 'web_search:gpt-6-terra']);
  });

  it('MH-HOSTED-UI-006: gives a Source model one capability per tool with evidence, in catalog order', () => {
    const verified = evidence({ status: 'verified', basis: 'probe', observed_at: AT });
    const refused = evidence({ status: 'unsupported', basis: 'observed', observed_at: AT, detail: '400 · unsupported_tool_type' });
    const status = {
      tools: [
        { name: 'web_search' as const, enabled: true, executor: null, bound: null, candidates: [candidate('src_relay001', 'gpt-6-astra', verified)] },
        { name: 'x_search' as const, enabled: true, executor: null, bound: null, candidates: [candidate('src_relay001', 'gpt-6-astra', refused)] },
        // Unchecked draws nothing: absence is the normal state, not a problem.
        { name: 'image_generation' as const, enabled: false, executor: null, bound: null, candidates: [candidate('src_relay001', 'gpt-6-astra', evidence({}))] },
      ],
      reverify: { engine_upgraded: 0, aged: 0 },
    };

    expect(modelCapabilities(status, 'src_relay001', 'gpt-6-astra').map(({ tool, chip }) => [tool, chip.kind]))
      .toEqual([['web_search', 'native'], ['x_search', 'unsupported']]);
    expect(modelCapabilities(status, 'src_relay001', 'gpt-6-sol')).toEqual([]);
  });
});
