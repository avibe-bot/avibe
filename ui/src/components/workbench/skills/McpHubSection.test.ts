import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const source = readFileSync(fileURLToPath(new URL('./McpHubSection.tsx', import.meta.url)), 'utf8');

describe('McpHubSection OAuth start link', () => {
  it('renders login as a new-tab start-link anchor', () => {
    expect(source).toMatch(/target="_blank"/);
    expect(source).toMatch(/rel="noopener noreferrer"/);
    expect(source).toMatch(/\/api\/mcp\/\$\{encodeURIComponent\(name\)\}\/oauth\/start/);
  });

  it('does not preallocate a blank tab or call window.open', () => {
    expect(source).not.toMatch(/window\.open/);
    expect(source).not.toMatch(/about:blank/);
  });

  it('keeps the add dialog open with a go-authorize start link after needs_login', () => {
    expect(source).toMatch(/mcpHub\.goAuthorize/);
    expect(source).toMatch(/mcpHub\.needsLoginAfterAdd/);
  });
});
