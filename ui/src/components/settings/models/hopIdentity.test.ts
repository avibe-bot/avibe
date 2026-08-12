import { readFileSync, readdirSync } from 'node:fs';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { equalHopIdentity, hopIdentity } from './hopIdentity';

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const implementationFiles = (): string[] => readdirSync(sourceDirectory, { withFileTypes: true })
  .filter((entry) => entry.isFile() && /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name))
  .map((entry) => join(sourceDirectory, entry.name))
  .filter((file) => basename(file) !== 'hopIdentity.ts')
  .filter((file) => /(?:chain\.current|isTakeoverChain|currentChainLink|SupplyRelation)/.test(readFileSync(file, 'utf8')));

describe('hop identity', () => {
  it('compares the complete source/model tuple', () => {
    const hop = hopIdentity({ source_id: 'source-a', model_id: 'model-a' });
    expect(equalHopIdentity(hop, { source_id: 'source-a', model_id: 'model-a' })).toBe(true);
    expect(equalHopIdentity(hop, { source_id: 'source-a', model_id: 'model-b' })).toBe(false);
    expect(equalHopIdentity(hop, { source_id: 'source-b', model_id: 'model-a' })).toBe(false);
  });

  it('keeps hop-field equality inside the identity primitive', () => {
    const directComparison = /(?:\.(?:source_id|model_id)\s*(?:===|!==)|(?:===|!==)[^\n;]*\.(?:source_id|model_id))/;
    const offenders = implementationFiles().filter((file) => directComparison.test(readFileSync(file, 'utf8')));
    expect(offenders).toEqual([]);
  });
});
