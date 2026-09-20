import { describe, expect, it } from 'vitest';

import { optionalTrimmedTextWithin, unicodeCodePointLength } from './validation';

describe('contract text validation', () => {
  it('counts Unicode code points rather than UTF-16 code units', () => {
    expect(unicodeCodePointLength('😀')).toBe(1);
    expect(optionalTrimmedTextWithin('😀'.repeat(64), 64)).toBe(true);
    expect(optionalTrimmedTextWithin('😀'.repeat(65), 64)).toBe(false);
  });
});
