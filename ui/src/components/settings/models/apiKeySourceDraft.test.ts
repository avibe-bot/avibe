// The rules two different frames now share.
//
// The same fields appear in Settings' Add-API-key dialog and in setup's add-source
// dialog. Every case here is a way the two could quietly stop agreeing about what a
// key is: a vendor switch that keeps the previous vendor's endpoint, a hand-edited
// endpoint lost to a re-selection, a protocol sent that is not the one the catalog
// publishes, or a failed create classified as decided when it was not.
import { describe, expect, it, vi } from 'vitest';

import {
  EMPTY_API_KEY_DRAFT,
  apiKeySourceCreate,
  apiKeyWriteSettled,
  draftComplete,
  draftNameValid,
  draftProtocol,
  selectVendor,
  sourceClientNonce,
} from './apiKeySourceDraft';
import { apiKeyVendorPreset } from './apiKeyVendors';
import { ApiCallError } from './modelsApi';
import { SOURCE_DISPLAY_NAME_MAX_LENGTH } from './types';

describe('selectVendor', () => {
  it('opens with no vendor assumed, so nothing is filled in on the person\'s behalf', () => {
    expect(EMPTY_API_KEY_DRAFT).toMatchObject({ baseUrl: '', apiKey: '', displayName: '' });
  });

  it('fills the endpoint a catalog vendor publishes', () => {
    const draft = selectVendor(EMPTY_API_KEY_DRAFT, 'openai');

    expect(draft.vendor).toBe('openai');
    expect(draft.baseUrl).not.toBe('');
  });

  it('starts a different vendor from its own defaults, not the last one\'s leftovers', () => {
    const openai = selectVendor(EMPTY_API_KEY_DRAFT, 'openai');
    const anthropic = selectVendor({ ...openai, baseUrl: 'https://edited.example' }, 'anthropic');

    expect(anthropic.baseUrl).not.toBe('https://edited.example');
    expect(anthropic.baseUrl).not.toBe(openai.baseUrl);
  });

  it('is identity on re-selection, which is what preserves a hand-edited endpoint', () => {
    // The vendor combobox re-fires on every render of its own list. Rebuilding the
    // draft there would silently discard a proxy URL someone typed on purpose.
    const edited = { ...selectVendor(EMPTY_API_KEY_DRAFT, 'openai'), baseUrl: 'https://proxy.example/v1' };

    expect(selectVendor(edited, 'openai')).toBe(edited);
  });

  it('never touches the key, because it belongs to the person and not to the catalog', () => {
    const typed = { ...EMPTY_API_KEY_DRAFT, apiKey: 'sk-typed-by-hand' };

    expect(selectVendor(typed, 'openai').apiKey).toBe('sk-typed-by-hand');
    expect(selectVendor(selectVendor(typed, 'openai'), 'anthropic').apiKey).toBe('sk-typed-by-hand');
  });
});

describe('draftProtocol', () => {
  it('sends what a catalog vendor publishes, whatever the control was left on', () => {
    const draft = { ...selectVendor(EMPTY_API_KEY_DRAFT, 'anthropic'), protocol: 'openai_chat' as const };

    expect(draftProtocol(draft)).toBe(apiKeyVendorPreset('anthropic')?.protocol);
    expect(draftProtocol(draft)).not.toBe('openai_chat');
  });

  it('sends the chosen protocol for a vendor the catalog does not know', () => {
    expect(draftProtocol({ ...EMPTY_API_KEY_DRAFT, protocol: 'anthropic' }))
      .toBe('anthropic');
  });
});

describe('draftComplete', () => {
  const filled = { ...EMPTY_API_KEY_DRAFT, baseUrl: 'https://api.example/v1', apiKey: 'sk-1' };

  it('needs an endpoint and a key', () => {
    expect(draftComplete(filled)).toBe(true);
    expect(draftComplete({ ...filled, apiKey: '' })).toBe(false);
    expect(draftComplete({ ...filled, baseUrl: '' })).toBe(false);
  });

  it('does not accept whitespace as either one', () => {
    expect(draftComplete({ ...filled, apiKey: '   ' })).toBe(false);
    expect(draftComplete({ ...filled, baseUrl: '  \t ' })).toBe(false);
  });

  it('leaves the name optional but not unbounded', () => {
    expect(draftNameValid({ ...filled, displayName: '' })).toBe(true);
    expect(draftComplete({ ...filled, displayName: 'x'.repeat(SOURCE_DISPLAY_NAME_MAX_LENGTH) })).toBe(true);
    expect(draftComplete({ ...filled, displayName: 'x'.repeat(SOURCE_DISPLAY_NAME_MAX_LENGTH + 1) })).toBe(false);
  });
});

describe('apiKeySourceCreate', () => {
  it('trims on the way out, and omits a name that was never given', () => {
    const request = apiKeySourceCreate({
      vendor: 'openai',
      displayName: '  ',
      baseUrl: '  https://api.example/v1  ',
      apiKey: '  sk-1  ',
      protocol: 'openai_chat',
    }, 'scn_test');

    expect(request).toEqual({
      kind: 'api_key',
      vendor: 'openai',
      base_url: 'https://api.example/v1',
      key: 'sk-1',
      // The catalog's pinned protocol, not the field's leftover value.
      protocol: apiKeyVendorPreset('openai')?.protocol,
      client_nonce: 'scn_test',
      save_unverified: true,
    });
  });

  it('carries the name once one is given', () => {
    const request = apiKeySourceCreate({
      ...EMPTY_API_KEY_DRAFT, baseUrl: 'https://api.example/v1', apiKey: 'sk-1', displayName: '  Work key  ',
    }, 'scn_test');

    expect(request.display_name).toBe('Work key');
  });

  it('always saves unverified, in every frame the form is hosted in', () => {
    // Deliberate product policy, not an oversight: the credential is saved on the
    // person's word and verified afterwards, so a provider that is briefly down
    // does not cost them the key they just pasted.
    expect(apiKeySourceCreate({ ...EMPTY_API_KEY_DRAFT, baseUrl: 'u', apiKey: 'k' }, 'n').save_unverified).toBe(true);
  });
});

describe('sourceClientNonce', () => {
  it('is unique per call, which is what a readback can recognise one write by', () => {
    const nonces = new Set(Array.from({ length: 32 }, sourceClientNonce));

    expect(nonces.size).toBe(32);
    for (const nonce of nonces) expect(nonce).toMatch(/^scn_[0-9a-f]{32}$/);
  });

  it('still produces one where randomUUID is unavailable', () => {
    // Not hypothetical: `crypto.randomUUID` is absent on a non-secure origin, which
    // is exactly where a local install gets opened over plain HTTP.
    vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue(undefined as never);
    try {
      expect(sourceClientNonce()).toMatch(/^scn_[0-9a-f]{32}$/);
    } finally {
      vi.restoreAllMocks();
    }
  });
});

describe('apiKeyWriteSettled', () => {
  const failure = (status: number, code = 'source_invalid') =>
    new ApiCallError(code, 'failed', true, [], [], [], status);

  it('accepts a server-named 4xx as a decision', () => {
    expect(apiKeyWriteSettled(failure(400))).toBe(true);
    expect(apiKeyWriteSettled(failure(422))).toBe(true);
  });

  it.each([409, 500, 502, 504])('leaves %i unsettled, because the write may have landed', (status) => {
    // Repeating one of these is how a second identical source appears. An unsettled
    // write is reconciled against its nonce instead.
    expect(apiKeyWriteSettled(failure(status))).toBe(false);
  });

  it('leaves a transport failure unsettled', () => {
    expect(apiKeyWriteSettled(new TypeError('Failed to fetch'))).toBe(false);
    expect(apiKeyWriteSettled(undefined)).toBe(false);
  });
});
