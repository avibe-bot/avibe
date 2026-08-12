import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { buildMockSources } from './mockData';

const SOURCE_SCHEMA = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../..',
  'docs/plans/model-hub-contracts/source.schema.json',
);

describe('Source model wire contract', () => {
  it('keeps typed fixtures aligned with every required model field and origin member', () => {
    const schema = JSON.parse(readFileSync(SOURCE_SCHEMA, 'utf8'));
    const modelSchema = schema.properties.models.items;
    const models = buildMockSources().flatMap((source) => source.models);
    const enumeratedFields = modelSchema.required.filter(
      (field: string) => Array.isArray(modelSchema.properties[field]?.enum),
    );
    const detail = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), 'SourceDetailPanel.tsx'), 'utf8');

    for (const model of models) {
      expect(modelSchema.required.every((field: string) => Object.hasOwn(model, field))).toBe(true);
    }
    for (const field of enumeratedFields) {
      expect(new Set(models.map((model) => model[field as keyof typeof model])))
        .toEqual(new Set(modelSchema.properties[field].enum));
      expect(detail).toContain(`model.${field}`);
    }
  });
});
