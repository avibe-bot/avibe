import { describe, expect, it } from 'vitest';

import { oauthResult, type OAuthResultResponse } from './modelsApi';
import type { OAuthFlow, Source } from './types';

const flow = (intent: 'create' | 'reauth' = 'create'): OAuthFlow => ({
  flow_id: 'oaf_1', intent, source_id: 'src_a', vendor: 'anthropic', channel: 'hub',
  state: 'success', presentation: { expects: 'none' }, error_key: null, expires_at: null,
});
const source = { id: 'src_a', kind: 'api_key', vendor: 'anthropic' } as Source;

describe('oauthResult', () => {
  it('keeps exact placement on a create terminal', () => {
    const added_to = [{ backend: 'claude' as const, menu_model: 'claude-opus-4-6', source_id: 'src_a', model_id: 'claude-opus-4-6', position: 1 }];
    const adopted_by = [{ backend: 'claude' as const, menu_model: 'claude-opus-4-6' }];
    const result = oauthResult({ ...flow(), flow: flow(), source, added_to, adopted_by } as OAuthResultResponse);
    expect(result.created).toEqual({ source, added_to, adopted_by });
  });

  it('uses the repair tail for reauth', () => {
    const result = oauthResult({ ...flow('reauth'), flow: flow('reauth'), source, recovered: true, interrupted_pairs: [] } as OAuthResultResponse);
    expect(result.created).toBeNull();
    expect(result.repaired?.recovered).toBe(true);
  });
});
