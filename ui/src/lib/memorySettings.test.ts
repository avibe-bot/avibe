import { describe, expect, it } from 'vitest';

import { buildEndpointPatch } from './memorySettings';


describe('buildEndpointPatch', () => {
  it('rotates an embedding API key while endpoint identity is locked', () => {
    expect(buildEndpointPatch(
      {
        baseUrl: 'https://changed.example.test/v1',
        model: 'changed-model',
        apiKey: 'rotated-key',
        clearKey: false,
      },
      {
        base_url: 'https://embed.example.test/v1',
        model: 'embed-model',
        api_key: null,
        has_api_key: true,
      },
      false,
      true,
    )).toEqual({ api_key: 'rotated-key' });
  });

  it('keeps endpoint identity editable when it is not locked', () => {
    expect(buildEndpointPatch(
      {
        baseUrl: 'https://changed.example.test/v1',
        model: 'changed-model',
        apiKey: '',
        clearKey: false,
      },
      {
        base_url: 'https://embed.example.test/v1',
        model: 'embed-model',
        api_key: null,
        has_api_key: false,
      },
      false,
      false,
    )).toEqual({
      base_url: 'https://changed.example.test/v1',
      model: 'changed-model',
    });
  });
});
