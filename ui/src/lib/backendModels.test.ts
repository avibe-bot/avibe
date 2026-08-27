import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, type ApiContextType } from '../context/ApiContext';
import { fetchBackendModels, loadBackendModelsWithRefresh } from './backendModels';

describe('fetchBackendModels for OpenCode', () => {
  it('reads the public options catalog without borrowing the native Settings surface', async () => {
    const opencodeOptions = vi.fn().mockResolvedValue({
      ok: true,
      data: {
        models: {
          providers: [
            { id: 'openrouter', models: { 'anthropic/claude-x': {} } },
            { id: 'custom', models: [{ id: 'hub-only' }] },
          ],
        },
        reasoning_options: {
          'custom/hub-only': [{ value: 'high', label: 'High' }],
        },
      },
    });
    const getOpencodeProviders = vi.fn();
    const api = {
      opencodeOptions,
      getOpencodeProviders,
    } as unknown as ApiContextType;

    const result = await fetchBackendModels(api, 'opencode');

    expect(getOpencodeProviders).not.toHaveBeenCalled();
    expect(opencodeOptions).toHaveBeenCalledWith('~');
    expect(result.models).toEqual([
      'openrouter/anthropic/claude-x',
      'custom/hub-only',
    ]);
    expect(result.reasoningOptions).toEqual({
      'custom/hub-only': [{ value: 'high', label: 'High' }],
    });
  });

  it('degrades to an empty catalog when the rank may not read the live options endpoint', async () => {
    const api = {
      opencodeOptions: vi
        .fn()
        .mockRejectedValue(new ApiError('forbidden', 403, 'instance_access_forbidden')),
    } as unknown as ApiContextType;

    await expect(fetchBackendModels(api, 'opencode')).resolves.toEqual({ models: [] });
  });

  it('still propagates a failure that is not the expected refusal', async () => {
    const api = {
      opencodeOptions: vi
        .fn()
        .mockRejectedValue(new ApiError('boom', 500, null)),
    } as unknown as ApiContextType;

    await expect(fetchBackendModels(api, 'opencode')).rejects.toBeInstanceOf(ApiError);
  });
});

describe('loadBackendModelsWithRefresh', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('delivers the immediate snapshot and silently refetches after refresh', async () => {
    vi.useFakeTimers();
    const codexModels = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        models: ['gpt-old'],
        catalog_refresh_pending: true,
      })
      .mockResolvedValueOnce({
        ok: true,
        models: ['gpt-old', 'gpt-new'],
        catalog_refresh_pending: false,
      });
    const api = { codexModels } as unknown as ApiContextType;
    const snapshots: string[][] = [];

    const cancel = loadBackendModelsWithRefresh(api, 'codex', (result) => {
      snapshots.push(result.models);
    });

    await vi.advanceTimersByTimeAsync(0);
    expect(snapshots).toEqual([['gpt-old']]);

    await vi.advanceTimersByTimeAsync(3_500);
    expect(snapshots).toEqual([['gpt-old'], ['gpt-old', 'gpt-new']]);
    expect(codexModels).toHaveBeenCalledTimes(2);

    cancel();
  });
});
