import { describe, expect, it } from 'vitest';

import { isEffortSupported, resolveEffortOptions } from './effortOptions';

describe('effort options', () => {
  it('uses the backend fallback for an unknown model', () => {
    const reasoningOptions = {
      'gpt-5.6-sol': [
        { value: '__default__', label: 'Default' },
        { value: 'ultra', label: 'Ultra' },
      ],
    };

    expect(resolveEffortOptions('codex', 'custom-model', reasoningOptions)).toEqual([
      'minimal',
      'low',
      'medium',
      'high',
      'xhigh',
    ]);
    expect(isEffortSupported('codex', 'custom-model', 'ultra', reasoningOptions)).toBe(false);
  });

  it('accepts catalog-only efforts for Claude and Codex models', () => {
    const reasoningOptions = {
      'future-model': [
        { value: '__default__', label: 'Default' },
        { value: 'ultra', label: 'Ultra' },
      ],
    };

    expect(isEffortSupported('claude', 'future-model', 'ultra', reasoningOptions)).toBe(true);
    expect(isEffortSupported('codex', 'future-model', 'ultra', reasoningOptions)).toBe(true);
  });
});
