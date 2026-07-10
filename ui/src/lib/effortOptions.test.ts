import { describe, expect, it } from 'vitest';

import { isEffortSupported, resolveEffortOptions } from './effortOptions';

describe('effort options', () => {
  it('uses backend fallbacks when the selected model has no catalog entry', () => {
    const reasoningOptions = {
      'gpt-5.6-sol': [
        { value: '__default__', label: 'Default' },
        { value: 'max', label: 'Max' },
      ],
    };

    expect(resolveEffortOptions('codex', 'gpt-fallback', reasoningOptions)).toEqual([
      'minimal',
      'low',
      'medium',
      'high',
      'xhigh',
    ]);
    expect(isEffortSupported('codex', 'gpt-fallback', 'max', reasoningOptions)).toBe(false);
  });

  it('accepts catalog-only efforts for models that explicitly provide them', () => {
    const reasoningOptions = {
      'claude-fable-6': [
        { value: '__default__', label: 'Default' },
        { value: 'ultra', label: 'Ultra' },
      ],
    };

    expect(isEffortSupported('claude', 'claude-fable-6', 'ultra', reasoningOptions)).toBe(true);
  });
});
