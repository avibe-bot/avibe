import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';

import {
  beginRegionRead,
  foldRegionRead,
  failRegionRead,
  loadingRegion,
  readRegion,
  readyRegion,
  settleRegionRead,
  type RegionRead,
} from './regionRead';
import { FIRST_PAINT_REGION_WHITELIST, readFirstPaintRegions, type FirstPaintRegionReaders } from './firstPaintRegions';

const productFiles = (directory: string): string[] => readdirSync(directory, { withFileTypes: true })
  .flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return productFiles(path);
    return /\.(?:ts|tsx)$/.test(entry.name) && !/\.test\.(?:ts|tsx)$/.test(entry.name) ? [path] : [];
  });

describe('RegionRead', () => {
  it('gives a newly injected region the same tagged lifecycle without domain fallbacks', async () => {
    type NewRegion = { rows: string[] };
    const read = vi.fn<() => Promise<NewRegion>>()
      .mockResolvedValueOnce({ rows: ['first'] })
      .mockRejectedValueOnce(new TypeError('unread'));

    const first = settleRegionRead(loadingRegion<NewRegion>(), await readRegion(read));
    expect(first).toEqual(readyRegion({ rows: ['first'] }));

    const pending = beginRegionRead(first);
    expect(pending).toMatchObject({ kind: 'degraded', cause: 'refreshing', retryable: false });

    const failed = settleRegionRead(pending, await readRegion(read));
    expect(failed).toMatchObject({ kind: 'degraded', cause: 'read_failed', retryable: true });
    expect(foldRegionRead(failed, {
      loading: () => null,
      ready: (data) => data,
      unread: () => null,
      degraded: (staleData) => staleData,
    })).toEqual({ rows: ['first'] });
  });

  it('marks a first failure unread instead of inventing a domain value', () => {
    expect(failRegionRead(loadingRegion<number>())).toEqual({ kind: 'unread', retryable: true });
  });

  it('makes unclassified data access impossible on the public union', () => {
    type ReadyRead = Extract<RegionRead<unknown>, { kind: 'ready' }>;
    type DegradedRead = Extract<RegionRead<unknown>, { kind: 'degraded' }>;
    type HasReadyPublicData = 'data' extends keyof ReadyRead ? true : false;
    type HasDegradedPublicData = 'data' extends keyof DegradedRead ? true : false;
    const hasPublicData: [HasReadyPublicData, HasDegradedPublicData] = [false, false];
    expect(hasPublicData).toEqual([false, false]);
  });

  it('keeps every landing region tagged instead of pairing nullable data with flags', () => {
    const page = readFileSync(join(__dirname, 'SettingsModelsPage.tsx'), 'utf8');
    const policy = readFileSync(join(__dirname, 'firstPaintRegions.ts'), 'utf8');

    expect(policy).toMatch(/export type SurfaceLanding = \{\s*\[K in keyof FirstPaintRegionValues\]: RegionRead<FirstPaintRegionValues\[K\]>;\s*\}/);
    expect(page).not.toMatch(/(?:sources|supply|runtime|events|chains)(?:State|Unread|Failed)\b/);
  });

  it('runs the first-paint barrier from its reasoned core-region whitelist only', async () => {
    const keys = Object.keys(FIRST_PAINT_REGION_WHITELIST);
    const called: string[] = [];
    const readers = Object.fromEntries(keys.map((key) => [key, async () => {
      called.push(key);
      return key;
    }])) as unknown as FirstPaintRegionReaders;

    const landing = await readFirstPaintRegions(readers);

    expect(called).toEqual(keys);
    expect(Object.keys(landing)).toEqual(keys);
    const page = readFileSync(join(__dirname, 'SettingsModelsPage.tsx'), 'utf8');
    const barrier = page.slice(page.indexOf('const readSurfaceLanding'), page.indexOf('\n\nconst surfaceLandingFailed'));
    expect(barrier).not.toMatch(/listEvents|events/);
  });

  it('allows RegionRead projection only through an exhaustive fold', () => {
    const violations = productFiles(__dirname).flatMap((path) => {
      if (path.endsWith('/regionRead.ts')) return [];
      const source = readFileSync(path, 'utf8');
      const importsRegionRead = /from ['"]\.\/(?:regionRead|modelRows)['"]/.test(source);
      const bypassesProjection = /\bfreshRegionData\b|\bregionData\s*\(|\b\w+\.data\b/.test(source);
      return importsRegionRead && bypassesProjection ? [path] : [];
    });

    expect(violations).toEqual([]);
  });

  it('keeps adoption decisions on the tagged runtime read', () => {
    const dialog = readFileSync(join(__dirname, 'EnableGatewayDialog.tsx'), 'utf8');

    expect(dialog).toMatch(/runtime: RegionRead<RuntimeDependency>/);
    expect(dialog).toMatch(/foldRegionRead<RuntimeDependency,[\s\S]*?degraded: \(\) => \(\{ kind: 'unavailable' \}\)/);
    expect(dialog).not.toMatch(/runtime: RuntimeDependency \| null/);
  });

  it('routes every per-model chain read through the per-backend latest authority', () => {
    const page = readFileSync(join(__dirname, 'SettingsModelsPage.tsx'), 'utf8');
    const definitionStart = page.indexOf('const readAgentChains');
    const definitionEnd = page.indexOf('\n\nconst settleAgentChainIndex');
    const withoutDefinition = `${page.slice(0, definitionStart)}${page.slice(definitionEnd)}`;
    const unownedCalls = [...withoutDefinition.matchAll(/\breadAgentChains\(/g)].flatMap((match) => {
      const before = withoutDefinition.slice(Math.max(0, (match.index ?? 0) - 240), match.index);
      return before.includes('chainReadAuthority.run') ? [] : [match.index];
    });
    const landing = page.slice(page.indexOf('const readSurfaceLanding'), page.indexOf('\n\nconst surfaceLandingFailed'));

    expect(definitionStart).toBeGreaterThanOrEqual(0);
    expect(definitionEnd).toBeGreaterThan(definitionStart);
    expect(withoutDefinition).not.toMatch(/modelsApi\.getAgentChain/);
    expect(unownedCalls).toEqual([]);
    expect(landing).not.toMatch(/readAgentChains|getAgentChain/);
    expect(page).not.toMatch(/\breadChains\b/);
    expect(page).toMatch(/chainReadAuthority\.invalidateExcept\(activeBackends\)/);
  });
});
