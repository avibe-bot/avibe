import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiContextType } from '../context/ApiContext';
import { loadBackendModelsWithRefresh } from './backendModels';

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
