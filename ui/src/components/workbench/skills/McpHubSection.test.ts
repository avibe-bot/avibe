import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const source = readFileSync(fileURLToPath(new URL('./McpHubSection.tsx', import.meta.url)), 'utf8');

describe('McpHubSection OAuth start link', () => {
  it('does not preallocate a blank tab or call window.open', () => {
    expect(source).not.toMatch(/window\.open/);
    expect(source).not.toMatch(/about:blank/);
  });

  it('keeps the add result copy and go-authorize start links', () => {
    expect(source).toMatch(/mcpHub\.goAuthorize/);
    expect(source).toMatch(/mcpHub\.needsLoginAfterAdd/);
    expect(source).toMatch(/mcpHub\.addResultHint/);
    expect(source).toMatch(/common\.done/);
  });
});
