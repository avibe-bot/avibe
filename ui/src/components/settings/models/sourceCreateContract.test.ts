import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { SOURCE_DISPLAY_NAME_MAX_LENGTH } from './types';

const CONTRACT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../..',
  'docs/plans/model-hub-contracts/source-create.schema.json',
);

describe('Source create contract projection', () => {
  it('takes the display-name limit from the authoritative schema', () => {
    const schema = JSON.parse(readFileSync(CONTRACT, 'utf8')) as {
      properties: { display_name: { maxLength: number } };
    };
    expect(SOURCE_DISPLAY_NAME_MAX_LENGTH).toBe(schema.properties.display_name.maxLength);
  });
});
