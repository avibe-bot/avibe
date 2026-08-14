import { describe, expect, it } from 'vitest';

import {
  buildEndpointPatch,
  memoryNavShouldBeVisible,
  memoryRuntimeRecoveryAvailable,
  memorySetupStage,
} from './memorySettings';


describe('memorySetupStage', () => {
  it('keeps setup discoverable without exposing the management navigation early', () => {
    expect(memorySetupStage(null, null)).toBe('loading');
    expect(memorySetupStage(false, false)).toBe('runtime-required');
    expect(memorySetupStage(true, false)).toBe('setup');
    expect(memorySetupStage(true, true)).toBe('manage');
  });

  it('keeps recovery settings reachable when the runtime is missing', () => {
    expect(memoryRuntimeRecoveryAvailable(false, true)).toBe(true);
    expect(memoryRuntimeRecoveryAvailable(false, false)).toBe(false);
    expect(memoryRuntimeRecoveryAvailable(true, true)).toBe(false);
  });

  it('keeps enabled Memory in navigation even when runtime health is checked elsewhere', () => {
    expect(memoryNavShouldBeVisible({
      status: 'ok',
      enabled: true,
      processing: {
        llm: { base_url: null, model: null, api_key: null, has_api_key: false },
        embedding: { base_url: null, model: null, api_key: null, has_api_key: false },
      },
    })).toBe(true);
    expect(memoryNavShouldBeVisible({ status: 'failed', error: 'memory_store_unavailable' })).toBe(false);
  });
});


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

  it('removes every field when clearing an optional endpoint', () => {
    expect(buildEndpointPatch(
      {
        baseUrl: 'https://rerank.example.test/v1/inference',
        model: 'rerank-model',
        apiKey: '',
        clearKey: true,
      },
      {
        base_url: 'https://rerank.example.test/v1/inference',
        model: 'rerank-model',
        api_key: null,
        has_api_key: true,
      },
      true,
      false,
      true,
    )).toEqual({ api_key: null, base_url: null, model: null });
  });
});
