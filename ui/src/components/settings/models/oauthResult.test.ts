// The OAuth terminal envelope (api.md → "OAuth completion").
//
// The regression these pin: the transport used to unwrap the response to `r.flow
// ?? r` and return the flow alone, so the `{source, adopted_by}` half the server
// puts beside it was discarded — and since the server materializes the Source
// inside the very call that first reports success (consuming the flow binding
// doing it), the dialog's follow-up POST /sources was then refused as
// `flow_not_found` on a connect that had in fact succeeded. Nothing in the mock
// caught it, because the mock had modelled completion and creation as separate.
import { describe, expect, it } from 'vitest';

import { oauthResult, type OAuthResultResponse } from './modelsApi';
import type { AdoptedBy, OAuthFlow, Source } from './types';

const flow = (over: Partial<OAuthFlow> = {}): OAuthFlow => ({
  flow_id: 'oaf_1',
  source_id: 'src_1',
  vendor: 'anthropic',
  channel: 'native_cli',
  state: 'success',
  presentation: { auth_url: null, device_code: null, expects: 'none', instructions_key: null },
  error_key: null,
  expires_at: null,
  ...over,
});

const source = { id: 'src_1', vendor: 'anthropic', kind: 'subscription' } as unknown as Source;
const adopted: AdoptedBy[] = [{ backend: 'claude', policy: 'follow', position: 1 }];

describe('oauthResult', () => {
  it('keeps the creation the terminal response reports', () => {
    const r = { flow: flow(), source, adopted_by: adopted } as OAuthResultResponse;
    expect(oauthResult(r)).toEqual({ flow: r.flow, created: { source, adopted_by: adopted }, repaired: null });
  });

  it('reads an absent adopted_by beside a source as nothing adopted it', () => {
    // Distinct from a response that reports no creation at all: here the source
    // exists, so 「没有 Agent 采用」 is a true statement and must be rendered.
    const { created } = oauthResult({ flow: flow(), source } as OAuthResultResponse);
    expect(created).toEqual({ source, adopted_by: [] });
  });

  it('reports no creation while the flow is still running', () => {
    expect(oauthResult({ flow: flow({ state: 'awaiting_action' }) } as OAuthResultResponse).created).toBeNull();
  });

  it('reports no creation for a terminal reauth flow', () => {
    // A reauth also terminates with a `source`, but its counterpart keys are
    // `recovered` / `interrupted_pairs` — no order changed, so there is no
    // adoption to report. Reading its payload as an adoption would state
    // something about this connect that the server never said.
    const r = { flow: flow({ intent: 'reauth' }), source } as OAuthResultResponse;
    expect(oauthResult(r).created).toBeNull();
  });

  it('keeps the repair tail a terminal reauth reports', () => {
    // The other half of the same discrimination: dropping this is the L5 version
    // of the bug above — the user re-authorizes, the server reports which Agents
    // are still stranded, and the dialog would close saying nothing about them.
    const gaps = [{ backend: 'claude' as const, model_id: 'claude-opus-4-6', agents: ['pm'] }];
    const r = {
      flow: flow({ intent: 'reauth' }),
      source,
      recovered: true,
      interrupted_pairs: gaps,
    } as OAuthResultResponse;
    expect(oauthResult(r).repaired).toEqual({ source, recovered: true, interrupted_pairs: gaps });
  });

  it('reads an absent recovered as "the server did not say so"', () => {
    // Never as a client guess that it did: `recovered` drives 「已恢复」 copy, and
    // claiming a recovery the server never reported is the one lie this tail can
    // tell. An absent `interrupted_pairs` normalizes to [], which is the same
    // statement the server makes by sending an empty array.
    const r = { flow: flow({ intent: 'reauth' }), source } as OAuthResultResponse;
    expect(oauthResult(r).repaired).toEqual({ source, recovered: false, interrupted_pairs: [] });
  });

  it('fills a gap missing its agents rather than dropping the gap', () => {
    // A gap with no nameable Agent is still a stranded model, and the confirm
    // dialog was opened to report it. `agents: []` renders as 「无」; a dropped
    // entry renders as 「nothing was stranded」, which is the opposite.
    const r = {
      flow: flow({ intent: 'reauth' }),
      source,
      interrupted_pairs: [{ backend: 'codex', model_id: 'gpt-5.6' }],
    } as unknown as OAuthResultResponse;
    expect(oauthResult(r).repaired?.interrupted_pairs).toEqual([
      { backend: 'codex', model_id: 'gpt-5.6', agents: [] },
    ]);
  });

  it('reports no repair while a reauth flow is still running', () => {
    const r = { flow: flow({ intent: 'reauth', state: 'awaiting_action' }) } as OAuthResultResponse;
    expect(oauthResult(r).repaired).toBeNull();
  });

  it('treats a flow without `intent` as a create flow', () => {
    // The field postdates the first shipped payloads and the schema keeps it
    // optional, so absent must not mean "unknown, report nothing".
    expect(oauthResult({ flow: flow(), source } as OAuthResultResponse).created).not.toBeNull();
  });

  it('tolerates a flat terminal payload', () => {
    // Same tolerance every other write in this transport keeps: the flow may
    // arrive un-nested, and the create half still has to survive.
    const r = { ...flow(), source, adopted_by: adopted } as OAuthResultResponse;
    const result = oauthResult(r);
    expect(result.flow.flow_id).toBe('oaf_1');
    expect(result.created?.adopted_by).toEqual(adopted);
  });
});
