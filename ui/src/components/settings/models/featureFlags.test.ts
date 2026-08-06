import { describe, expect, it } from 'vitest';

import { modelHubEnabledFromConfig } from './featureFlags';

describe('modelHubEnabledFromConfig', () => {
  it('uses the backend capability as the only enabled value', () => {
    const config = { capabilities: { model_hub: { enabled: true } } };

    expect(modelHubEnabledFromConfig(config)).toBe(true);
    expect(modelHubEnabledFromConfig(config)).toBe(config.capabilities.model_hub.enabled);
  });

  it.each([
    undefined,
    {},
    { capabilities: {} },
    { capabilities: { model_hub: {} } },
    { capabilities: { model_hub: { enabled: false } } },
    { capabilities: { model_hub: { enabled: 'true' } } },
  ])('fails closed for missing or malformed backend capability: %o', (config) => {
    expect(modelHubEnabledFromConfig(config)).toBe(false);
  });
});
