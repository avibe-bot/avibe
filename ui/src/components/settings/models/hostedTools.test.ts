import { describe, expect, it } from 'vitest';

import { chosenExecutor, evidenceChip, modelCapabilities } from './hostedTools';
import type { HostedEvidence, HostedToolCandidate, HostedToolStatus } from './types';

const evidence = (overrides: Partial<HostedEvidence>): HostedEvidence => ({
  status: 'unknown',
  stale: null,
  basis: null,
  observed_at: null,
  detail: null,
  ...overrides,
});

const AT = '2026-10-13T10:20:00Z';

const candidate = (sourceId: string, modelId: string, call: HostedEvidence): HostedToolCandidate => ({
  source_id: sourceId,
  model_id: modelId,
  call,
  replay: null,
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

  it('names only a verified choice as the executor, never a guessed automatic one', () => {
    const verified = evidence({ status: 'verified', basis: 'probe', observed_at: AT });
    const tool = (executor: HostedToolStatus['executor'], candidates: HostedToolCandidate[]): HostedToolStatus => ({
      name: 'web_search',
      enabled: true,
      executor,
      candidates,
    });
    const grok = candidate('src_grok0001', 'grok-4.6', verified);
    const relay = candidate('src_relay001', 'gpt-6-astra', verified);

    expect(chosenExecutor(tool({ source_id: 'src_grok0001', model_id: 'grok-4.6' }, [relay, grok]))).toBe(grok);
    // Automatic binds the first verified pair in Vibey's order, which this read
    // does not state (its candidates also list Sources outside that order).
    expect(chosenExecutor(tool(null, [relay, grok]))).toBeNull();
    // A saved choice that is no longer verified is not what the turn binds.
    expect(chosenExecutor(tool(
      { source_id: 'src_grok0001', model_id: 'grok-4.6' },
      [relay, candidate('src_grok0001', 'grok-4.6', evidence({ status: 'unknown', stale: 'wire', basis: 'probe', observed_at: AT }))],
    ))).toBeNull();
  });

  it('gives a Source model one capability per tool with evidence, in catalog order', () => {
    const verified = evidence({ status: 'verified', basis: 'probe', observed_at: AT });
    const refused = evidence({ status: 'unsupported', basis: 'observed', observed_at: AT, detail: '400 · unsupported_tool_type' });
    const status = {
      tools: [
        { name: 'web_search' as const, enabled: true, executor: null, candidates: [candidate('src_relay001', 'gpt-6-astra', verified)] },
        { name: 'x_search' as const, enabled: true, executor: null, candidates: [candidate('src_relay001', 'gpt-6-astra', refused)] },
        // Unchecked draws nothing: absence is the normal state, not a problem.
        { name: 'image_generation' as const, enabled: false, executor: null, candidates: [candidate('src_relay001', 'gpt-6-astra', evidence({}))] },
      ],
      reverify: { engine_upgraded: 0, aged: 0 },
    };

    expect(modelCapabilities(status, 'src_relay001', 'gpt-6-astra').map(({ tool, enabled, chip }) => [tool, enabled, chip.kind]))
      .toEqual([['web_search', true, 'native'], ['x_search', true, 'unsupported']]);
    expect(modelCapabilities(status, 'src_relay001', 'gpt-6-sol')).toEqual([]);
  });
});
