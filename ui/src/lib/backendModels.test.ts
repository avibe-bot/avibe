import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiContextType } from '../context/ApiContext';
import { fetchBackendModels, loadBackendModelsWithRefresh } from './backendModels';

describe('fetchBackendModels', () => {
  it('reads OpenCode models from the picker catalog, not the Settings provider endpoint', async () => {
    const getOpencodeModelCatalog = vi.fn().mockResolvedValue({
      ok: true,
      providers: [{ id: 'openrouter', name: 'OpenRouter', models: ['anthropic/claude-x'] }],
    });
    const getOpencodeProviders = vi.fn();
    const api = { getOpencodeModelCatalog, getOpencodeProviders } as unknown as ApiContextType;

    const result = await fetchBackendModels(api, 'opencode');

    // The Settings endpoint carries provider credentials and is Owner-only, so
    // touching it here would 403 for every rank below the instance owner.
    expect(getOpencodeProviders).not.toHaveBeenCalled();
    expect(result.models).toEqual(['openrouter/anthropic/claude-x']);
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
