import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiContextType } from '../context/ApiContext';
import { loadBackendModelsWithRefresh } from './backendModels';

describe('loadBackendModelsWithRefresh', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('delivers the cached snapshot and silently refetches after a catalog refresh', async () => {
    vi.useFakeTimers();
    const claudeModels = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        models: ['claude-old'],
        catalog_refresh_pending: true,
      })
      .mockResolvedValueOnce({
        ok: true,
        models: ['claude-old', 'claude-new'],
        catalog_refresh_pending: false,
      });
    const api = { claudeModels } as unknown as ApiContextType;
    const snapshots: string[][] = [];

    const cancel = loadBackendModelsWithRefresh(api, 'claude', (result) => {
      snapshots.push(result.models);
    });

    await vi.advanceTimersByTimeAsync(0);
    expect(snapshots).toEqual([['claude-old']]);

    await vi.advanceTimersByTimeAsync(3_500);
    expect(snapshots).toEqual([['claude-old'], ['claude-old', 'claude-new']]);
    expect(claudeModels).toHaveBeenCalledTimes(2);

    cancel();
  });
});
