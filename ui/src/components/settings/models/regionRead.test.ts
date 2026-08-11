import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';

import {
  beginRegionRead,
  failRegionRead,
  loadingRegion,
  readRegion,
  readyRegion,
  regionData,
  settleRegionRead,
} from './regionRead';

describe('RegionRead', () => {
  it('gives a newly injected region the same tagged lifecycle without domain fallbacks', async () => {
    type NewRegion = { rows: string[] };
    const read = vi.fn<() => Promise<NewRegion>>()
      .mockResolvedValueOnce({ rows: ['first'] })
      .mockRejectedValueOnce(new TypeError('unread'));

    const first = settleRegionRead(loadingRegion<NewRegion>(), await readRegion(read));
    expect(first).toEqual(readyRegion({ rows: ['first'] }));

    const pending = beginRegionRead(first);
    expect(pending).toEqual({ kind: 'loading', data: { rows: ['first'] } });

    const failed = settleRegionRead(pending, await readRegion(read));
    expect(failed).toEqual({ kind: 'error', data: { rows: ['first'] }, retryable: true });
    expect(regionData(failed)).toEqual({ rows: ['first'] });
  });

  it('marks a first failure unread instead of inventing a domain value', () => {
    expect(failRegionRead(loadingRegion<number>())).toEqual({ kind: 'unread', retryable: true });
    expect(regionData(failRegionRead(loadingRegion<number>()))).toBeUndefined();
  });

  it('keeps every landing region tagged instead of pairing nullable data with flags', () => {
    const page = readFileSync(join(__dirname, 'SettingsModelsPage.tsx'), 'utf8');
    const body = page.match(/type SurfaceLanding = \{(?<body>[\s\S]*?)\n\};/)?.groups?.body;
    const memberTypes = [...(body ?? '').matchAll(/^\s+\w+:\s+([^;]+);$/gm)]
      .map((match) => match[1]);

    expect(memberTypes.length).toBeGreaterThan(0);
    expect(memberTypes.every((type) => type.startsWith('RegionRead<'))).toBe(true);
    expect(page).not.toMatch(/(?:sources|supply|runtime|events|chains)(?:State|Unread|Failed)\b/);
  });
});
