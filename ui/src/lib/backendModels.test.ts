import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, type ApiContextType } from '../context/ApiContext';
import { fetchBackendModels, loadBackendModelsWithRefresh } from './backendModels';

describe('fetchBackendModels for OpenCode', () => {
  it('reads the provider catalog through the picker helper, never the Settings one', async () => {
    const readOpencodeProvidersForModelPicker = vi.fn().mockResolvedValue({
      ok: true,
      providers: [
        { id: 'openrouter', configured: true, models: ['anthropic/claude-x'] },
        { id: 'openai', configured: false, models: ['gpt-5'] },
      ],
    });
    const getOpencodeProviders = vi.fn();
    const api = {
      readOpencodeProvidersForModelPicker,
      getOpencodeProviders,
    } as unknown as ApiContextType;

    const result = await fetchBackendModels(api, 'opencode');

    // Settings owns the toast-on-403 read; the picker must not borrow it, or a
    // member's page load announces a refusal it is expected to absorb.
    expect(getOpencodeProviders).not.toHaveBeenCalled();
    expect(result.models).toEqual(['openrouter/anthropic/claude-x']);
  });

  it('degrades to an empty catalog when the rank may not read the Settings endpoint', async () => {
    const api = {
      readOpencodeProvidersForModelPicker: vi
        .fn()
        .mockRejectedValue(new ApiError('forbidden', 403, 'instance_access_forbidden')),
    } as unknown as ApiContextType;

    await expect(fetchBackendModels(api, 'opencode')).resolves.toEqual({ models: [] });
  });

  it('still propagates a failure that is not the expected refusal', async () => {
    const api = {
      readOpencodeProvidersForModelPicker: vi
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
