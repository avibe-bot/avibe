import { describe, expect, it, vi } from 'vitest';

import { scanMigrationWhenEnabled } from './MigrationBanner';

describe('scanMigrationWhenEnabled', () => {
  it('does not issue a migration scan while Model Hub is disabled', async () => {
    const scan = vi.fn(async () => ({ items: [] }));

    await expect(scanMigrationWhenEnabled(false, scan)).resolves.toBeNull();
    expect(scan).not.toHaveBeenCalled();
  });

  it('issues one scan after the backend capability is enabled', async () => {
    const result = { items: [] };
    const scan = vi.fn(async () => result);

    await expect(scanMigrationWhenEnabled(true, scan)).resolves.toBe(result);
    expect(scan).toHaveBeenCalledTimes(1);
  });
});
