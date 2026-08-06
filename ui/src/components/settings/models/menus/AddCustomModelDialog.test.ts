import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { manualModelIdentifier } from './identifiers';

const here = dirname(fileURLToPath(import.meta.url));

describe('manual model identifier context', () => {
  const standardVendors = new Set(['anthropic', 'openai']);

  it('keeps raw upstream IDs outside OpenCode and prefixes OpenCode identifiers only', () => {
    expect(manualModelIdentifier('  claude-special  ', 'anthropic', standardVendors, false)).toBe('claude-special');
    expect(manualModelIdentifier('  claude-special  ', 'anthropic', standardVendors, true)).toBe('anthropic/claude-special');
  });

  it('shows the generated preview only for OpenCode callers', () => {
    const dialog = readFileSync(join(here, 'AddCustomModelDialog.tsx'), 'utf8');
    const drawer = readFileSync(join(here, 'OpenCodeMenuDrawer.tsx'), 'utf8');
    const page = readFileSync(join(here, '..', 'SettingsModelsPage.tsx'), 'utf8');

    expect(dialog).toMatch(/\{showOpenCodeIdentifier && \(\s*<div/);
    expect(drawer).toMatch(/<AddCustomModelDialog[\s\S]*?showOpenCodeIdentifier/);
    expect(page).toMatch(/showOpenCodeIdentifier=\{customModelRequest\?\.backend === 'opencode'\}/);
  });
});
