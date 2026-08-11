import { readdirSync, readFileSync } from 'node:fs';
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

  it('requires every product file that projects RegionRead data to name its read-state branch', () => {
    const violations = productFiles(__dirname).flatMap((path) => {
      if (path.endsWith('/regionRead.ts')) return [];
      const source = readFileSync(path, 'utf8');
      const importsRegionRead = /from ['"]\.\/(?:regionRead|modelRows)['"]/.test(source);
      const projectsData = /\bregionData\s*\(|\b\w+\.data\b/.test(source);
      const namesReadState = /\.kind\s*(?:===|!==)\s*['"](?:ready|error|unread)['"]/.test(source);
      return importsRegionRead && projectsData && !namesReadState ? [path] : [];
    });

    expect(violations).toEqual([]);
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
