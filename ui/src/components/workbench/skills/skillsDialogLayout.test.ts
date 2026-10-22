import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const directory = dirname(fileURLToPath(import.meta.url));
const sources = {
  browse: readFileSync(join(directory, 'BrowseRegistryDialog.tsx'), 'utf8'),
  add: readFileSync(join(directory, 'AddSkillDialog.tsx'), 'utf8'),
};

describe('skill dialog viewport safety', () => {
  it.each(Object.entries(sources))('%s keeps custom modal content within a scrollable viewport', (_name, source) => {
    expect(source).toContain('p-4 backdrop-blur-[6px] sm:p-10');
    expect(source).toContain('min-w-0 max-h-[calc(100dvh-2rem)]');
    expect(source).toContain('overflow-y-auto rounded-2xl');
    expect(source).toContain('min-w-0 flex-1 flex-col');
    expect(source).toContain('[overflow-wrap:anywhere]');
  });
});
