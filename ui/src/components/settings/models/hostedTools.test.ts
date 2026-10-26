import { describe, expect, it } from 'vitest';

import { executorOrderAfterMove, modelCapabilities, pairEvidence, refusalSummary } from './hostedTools';
import type { HostedRefusal, HostedSuccess, HostedToolRunStatus, HostedToolsStatus } from './types';

const RELAY = 'src_relay001';
const SUB = 'src_chatgpt1';
const CLAUDE = 'src_claude01';

const ok = (sourceId: string, modelId: string, at: string): HostedSuccess => ({ source_id: sourceId, model_id: modelId, mode: 'native', at });
const refused = (sourceId: string, modelId: string, at: string, aspect: HostedRefusal['aspect'] = 'call'): HostedRefusal => ({
  source_id: sourceId, model_id: modelId, aspect, at, reason: '400 unsupported_tool_type',
});
const run = (over: Partial<HostedToolRunStatus>): HostedToolRunStatus => ({ latest_ok: null, verified: [], refused: [], ...over });

describe('hosted-tool status projection', () => {
  it('MH-HOSTED-UI-002: groups refusals by aspect and Source, with each group\'s models and newest time', () => {
    const summary = refusalSummary(run({
      refused: [
        refused(RELAY, 'gpt-6-astra', '2026-10-14T09:00:00Z'),
        refused(RELAY, 'gpt-6-luna', '2026-10-14T11:00:00Z'),
        refused(SUB, 'gpt-6.1-sol', '2026-10-14T10:00:00Z', 'replay'),
      ],
    }));

    expect(summary.call).toEqual([{ sourceId: RELAY, aspect: 'call', models: ['gpt-6-astra', 'gpt-6-luna'], at: '2026-10-14T11:00:00Z' }]);
    expect(summary.replay).toEqual([{ sourceId: SUB, aspect: 'replay', models: ['gpt-6.1-sol'], at: '2026-10-14T10:00:00Z' }]);
    expect(summary.sources).toBe(2);
  });

  it('MH-HOSTED-UI-002: leaves a Source that refused the call out of the replay line, so it counts once', () => {
    const summary = refusalSummary(run({
      refused: [refused(RELAY, 'gpt-6-astra', '2026-10-14T09:00:00Z'), refused(RELAY, 'gpt-6-luna', '2026-10-14T09:00:00Z', 'replay')],
    }));

    expect(summary.call.map((group) => group.sourceId)).toEqual([RELAY]);
    expect(summary.replay).toEqual([]);
    expect(summary.sources).toBe(1);
  });

  // "已改用" claims the tool has run elsewhere since every refusal: it needs a
  // later success, on a Source that is not among the refusing ones.
  const switched: [string, Partial<HostedToolRunStatus>, string | null][] = [
    ['a later success on another Source', { latest_ok: ok(CLAUDE, 'claude-haiku-5-5', '2026-10-15T08:00:00Z') }, CLAUDE],
    ['no success since', { latest_ok: ok(CLAUDE, 'claude-haiku-5-5', '2026-10-13T08:00:00Z') }, null],
    ['the success is on the refusing Source', { latest_ok: ok(RELAY, 'gpt-6-sol', '2026-10-15T08:00:00Z') }, null],
    ['no success at all', {}, null],
  ];
  it.each(switched)('MH-HOSTED-UI-002: says where the tool runs now only after %s', (_name, over, expected) => {
    expect(refusalSummary(run({ refused: [refused(RELAY, 'gpt-6-astra', '2026-10-14T09:00:00Z')], ...over })).switchedTo).toBe(expected);
  });

  it('MH-HOSTED-UI-006: gives each pair one record, a refusal over a success and a call refusal over a replay one', () => {
    const lastOk = ok(RELAY, 'gpt-6-astra', '2026-10-13T08:00:00Z');
    const pairs = pairEvidence(run({
      verified: [lastOk, ok(CLAUDE, 'claude-haiku-5-5', '2026-10-15T08:00:00Z')],
      refused: [
        refused(RELAY, 'gpt-6-astra', '2026-10-14T09:00:00Z', 'replay'),
        refused(RELAY, 'gpt-6-astra', '2026-10-14T10:00:00Z'),
      ],
    }));

    expect(pairs).toEqual([
      { kind: 'ok', sourceId: CLAUDE, modelId: 'claude-haiku-5-5', success: ok(CLAUDE, 'claude-haiku-5-5', '2026-10-15T08:00:00Z') },
      { kind: 'refused', sourceId: RELAY, modelId: 'gpt-6-astra', refusal: refused(RELAY, 'gpt-6-astra', '2026-10-14T10:00:00Z'), lastOk },
    ]);
  });

  it('MH-HOSTED-UI-006: gives a Source model one capability per feature, a call refusal hiding the success and the replay', () => {
    const replayed = run({ verified: [ok(RELAY, 'gpt-6-astra', '2026-10-15T08:00:00Z')], refused: [refused(RELAY, 'gpt-6-astra', '2026-10-14T09:00:00Z', 'replay')] });
    const refusedToo = { ...replayed, refused: [...replayed.refused, refused(RELAY, 'gpt-6-astra', '2026-10-14T10:00:00Z')] };
    const kinds = (runStatus: HostedToolRunStatus) => modelCapabilities(
      { tools: [{ name: 'web_search', prefer_native: true, services: {}, service_order: [], enabled: true, status: runStatus, executor_order: null, effective_order: [], executor_candidates: [] }] },
      RELAY, 'gpt-6-astra',
    ).map(({ evidence }) => (evidence.kind === 'ok' ? 'ok' : evidence.refusal.aspect));
    expect(kinds(replayed)).toEqual(['ok', 'replay']);
    expect(kinds(refusedToo)).toEqual(['call']);
  });

  it('MH-HOSTED-UI-006: gives a Source model one capability per tool with a record, in catalog order', () => {
    const status: HostedToolsStatus = {
      tools: [
        { name: 'web_search', prefer_native: true, services: {}, service_order: [], enabled: true, status: run({ verified: [ok(RELAY, 'gpt-6-astra', '2026-10-15T08:00:00Z')] }), executor_order: null, effective_order: [], executor_candidates: [] },
        { name: 'x_search', enabled: true, status: run({ refused: [refused(RELAY, 'gpt-6-astra', '2026-10-14T09:00:00Z')] }), executor_order: null, effective_order: [], executor_candidates: [] },
        // No record draws nothing: unused is the normal state, not a problem.
        { name: 'image_generate', enabled: true, status: run({}), default_model: null, effective_default: null, models: [], image_offered_by: [] },
      ],
    };

    expect(modelCapabilities(status, RELAY, 'gpt-6-astra').map(({ tool, evidence }) => [tool, evidence.kind]))
      .toEqual([['web_search', 'ok'], ['x_search', 'refused']]);
    expect(modelCapabilities(status, RELAY, 'gpt-6-sol')).toEqual([]);
  });

  it('MH-HOSTED-UI-021: moves only the shown executors within the saved order, keeping hidden ones in place', () => {
    // H is saved but not shown now (refused, or gone for a while); C is shown but not saved.
    expect(executorOrderAfterMove(['A', 'H', 'B'], ['A', 'B', 'C'], 2, -1)).toEqual(['A', 'H', 'C', 'B']);
    expect(executorOrderAfterMove(['H', 'A', 'B'], ['A', 'B'], 1, -1)).toEqual(['H', 'B', 'A']);
    // Automatic: the shown order is the whole order.
    expect(executorOrderAfterMove(null, ['A', 'B'], 0, 1)).toEqual(['B', 'A']);
  });
});
