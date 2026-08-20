import { describe, expect, it } from 'vitest';

import zh from './zh.json';

describe('Chinese product terminology', () => {
  it('uses the localized app bar name throughout user-visible copy', () => {
    expect(JSON.stringify(zh)).not.toMatch(/\bDock\b/);
  });
});
