import { describe, expect, it, vi } from 'vitest';

import {
  scheduleUpgradeReload,
  UPGRADE_RELOAD_DELAY_MS,
} from './upgradeReload';

describe('scheduleUpgradeReload', () => {
  it('waits 30 seconds after an upgrade requests a restart', async () => {
    vi.useFakeTimers();
    const reload = vi.fn();
    try {
      scheduleUpgradeReload(reload, (callback, delayMs) => setTimeout(callback, delayMs));

      expect(UPGRADE_RELOAD_DELAY_MS).toBe(30_000);
      await vi.advanceTimersByTimeAsync(29_999);
      expect(reload).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(1);
      expect(reload).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });
});
