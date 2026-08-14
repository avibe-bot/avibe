import { describe, expect, it } from 'vitest';

import type { OpencodeProvider } from '@/context/ApiContext';

import { providerOauthSignedIn } from './OpencodeProviderConfig';

const baseProvider: OpencodeProvider = {
  id: 'openai',
  name: 'OpenAI',
  description: '',
  configured: false,
  oauth_available: true,
  local: false,
  models: [],
  default_model: null,
};

describe('providerOauthSignedIn', () => {
  // Regression: an API-key save marks the provider ``configured``, and the
  // in-card OAuth panel used to render "OAuth credentials stored" off that
  // flag alone. The panel must only report signed-in for a stored OAuth
  // entry — property: signed-in follows the active auth type, never the
  // configured badge.
  it('is false for a provider configured with an API key', () => {
    expect(
      providerOauthSignedIn({
        ...baseProvider,
        configured: true,
        has_auth: true,
        active_auth_type: 'api',
        api_key_masked: 'sk-9d68•••8819',
      }),
    ).toBe(false);
  });

  it('is true only while the active auth type is oauth', () => {
    expect(
      providerOauthSignedIn({
        ...baseProvider,
        configured: true,
        has_auth: true,
        active_auth_type: 'oauth',
      }),
    ).toBe(true);
  });

  it('is false when no auth type is reported (unconfigured, local, or custom)', () => {
    expect(providerOauthSignedIn({ ...baseProvider })).toBe(false);
    expect(
      providerOauthSignedIn({
        ...baseProvider,
        configured: true,
        local: true,
        oauth_available: false,
      }),
    ).toBe(false);
    expect(
      providerOauthSignedIn({
        ...baseProvider,
        configured: true,
        custom: true,
        oauth_available: false,
        active_auth_type: null,
      }),
    ).toBe(false);
  });
});
