import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE_ROOT = join(process.cwd(), 'src');
const SOURCE_MODULE = /\.[cm]?[jt]sx?$/;
const TEST_MODULE = /\.(?:test|spec)\.[cm]?[jt]sx?$/;
const MODULE_SPECIFIER = /(?:\bfrom\s*|\b(?:import|require)\s*\(\s*|\bimport\s*)['"]([^'"]+)['"]/g;

const sourceModules = (directory: string): string[] =>
  readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sourceModules(path);
    return SOURCE_MODULE.test(entry.name) ? [path] : [];
  });

const importsMockData = (path: string): boolean =>
  [...readFileSync(path, 'utf8').matchAll(MODULE_SPECIFIER)]
    .some((match) => /(?:^|\/)mockData(?:\.[^/]*)?$/.test(match[1]));

describe('Model Hub fixture import boundary', () => {
  it('keeps fabricated source fixtures out of product modules', () => {
    const productImporters = sourceModules(SOURCE_ROOT)
      .filter(importsMockData)
      .filter((path) => !TEST_MODULE.test(path))
      .map((path) => relative(SOURCE_ROOT, path));

    expect(productImporters).toEqual([]);
  });
});
