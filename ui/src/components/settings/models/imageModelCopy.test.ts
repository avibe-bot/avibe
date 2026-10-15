// Every image model the bundled overlay ships has its own description in each
// locale (`vibey-hosted-tools.md` §8.1): the overlay's line is model-facing
// English, kept only as the fallback for a model Avibe does not ship.
import { readFile } from 'node:fs/promises';
import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
import { imageModelCopyKey } from './hostedTools';
import zh from '../../../i18n/zh.json';

describe('image model copy', () => {
  it('MH-HOSTED-UI-022: every bundled image model has a description in each locale', async () => {
    const overlay = JSON.parse(await readFile('../vibe/data/backend_models.json', 'utf8')) as {
      providers: Record<string, { image_models?: { id: string }[] }>;
    };
    expect(overlay.providers).toBeTypeOf('object');
    const shipped = Object.values(overlay.providers).flatMap((vendor) => (vendor.image_models ?? []).map((model) => model.id));
    for (const [lng, copy] of [['en', en], ['zh', zh]] as const) {
      const descriptions = copy.settings.models.tools.image.description as Record<string, string>;
      expect(shipped.filter((id) => !descriptions[imageModelCopyKey(id)]?.trim()), lng).toEqual([]);
    }
  });
});
