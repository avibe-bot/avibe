import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  assertModelHubModuleBoundary,
  findMockOnlyReachability,
  liveEntry,
  mockOnlyRoot,
  sourceRoot,
} from './validate-model-hub-live-boundary.mjs';

describe('Model Hub live module boundary', () => {
  it('keeps every mock-only module unreachable from the production entry', async () => {
    await expect(assertModelHubModuleBoundary({
      entry: liveEntry,
      root: sourceRoot,
      forbiddenRoot: mockOnlyRoot,
    })).resolves.toBeUndefined();
  });

  it('discovers future mock-only modules through the directory boundary', async () => {
    const root = await mkdtemp(join(tmpdir(), 'model-hub-module-boundary-'));
    const forbiddenRoot = join(root, 'mock-only');
    await mkdir(forbiddenRoot);
    await writeFile(join(root, 'live.ts'), "import './bridge';\n");
    await writeFile(join(root, 'bridge.ts'), "void import('./mock-only/future');\n");
    await writeFile(join(forbiddenRoot, 'future.ts'), 'export const value = 1;\n');

    try {
      const leaked = await findMockOnlyReachability({
        entry: join(root, 'live.ts'),
        root,
        forbiddenRoot,
      });
      expect(leaked.map(({ path }) => path)).toEqual([
        join(forbiddenRoot, 'future.ts'),
      ]);
      await expect(assertModelHubModuleBoundary({
        entry: join(root, 'live.ts'),
        root,
        forbiddenRoot,
      })).rejects.toThrow(
        'live.ts -> bridge.ts -> mock-only/future.ts',
      );
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
});
