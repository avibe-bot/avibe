import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';

import { createAgentCollectionReadAuthority } from './collectionReadAuthority';
import type { AgentSupply } from './types';

const agent = (mode: AgentSupply['mode']): AgentSupply => ({
  backend: 'claude',
  cli_present: true,
  mode,
  menu_kind: 'fixed',
  sources: mode === 'hub' ? { order: [], eligibility: [] } : null,
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

const productFiles = (directory: string): string[] => readdirSync(directory, { withFileTypes: true })
  .flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return productFiles(path);
    return /\.(?:ts|tsx)$/.test(entry.name) && !/\.test\.(?:ts|tsx)$/.test(entry.name) ? [path] : [];
  });

describe('collection read authority', () => {
  it('rejects an older Agent collection response after a newer generation lands', async () => {
    const older = deferred<AgentSupply[]>();
    const newer = deferred<AgentSupply[]>();
    const api = {
      listAgents: vi.fn()
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(newer.promise),
    };
    const authority = createAgentCollectionReadAuthority(api);

    const first = authority.read();
    const second = authority.read();
    newer.resolve([agent('hub')]);
    await expect(second).resolves.toEqual({ kind: 'current', value: [agent('hub')] });

    older.resolve([agent('direct')]);
    await expect(first).resolves.toEqual({ kind: 'stale' });
  });

  it('keeps collection endpoints private to the generation authority', () => {
    const allowed = new Set(['collectionReadAuthority.ts', 'modelsApi.ts']);
    const violations = productFiles(__dirname).flatMap((path) => {
      if (allowed.has(path.split('/').at(-1) ?? '')) return [];
      return /\.list(?:Agents|Sources)\s*\(/.test(readFileSync(path, 'utf8')) ? [path] : [];
    });

    expect(violations).toEqual([]);
  });
});
