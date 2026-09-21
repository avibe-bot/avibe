import { describe, expect, it } from 'vitest';

import {
  EFFORT_BY_BACKEND,
  isEffortSupported,
  REASONING_EFFORTS,
  resolveEffortOptions,
  sortEffortsByVocabulary,
} from './effortOptions';

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

  it.each(['claude', 'codex', 'opencode'])('offers Off only from the selected %s model declaration', (backend) => {
    const reasoningOptions = {
      'with-off': [
        { value: '__default__', label: 'Default' },
        { value: 'high', label: 'High' },
        { value: 'none', label: 'Off' },
        { value: 'custom-effort', label: 'Custom' },
      ],
      'without-off': [{ value: 'high', label: 'High' }],
      'empty': [],
      'default-only': [{ value: '__default__', label: 'Default' }],
    };

    // Preserve declared order and custom values, rather than changing the first
    // option (which existing callers may use as their suggested default).
    expect(resolveEffortOptions(backend, 'with-off', reasoningOptions)).toEqual(['high', 'none', 'custom-effort']);
    expect(isEffortSupported(backend, 'with-off', 'none', reasoningOptions)).toBe(true);
    for (const model of ['without-off', 'empty', 'default-only', 'unknown']) {
      expect(isEffortSupported(backend, model, 'none', reasoningOptions)).toBe(false);
      expect(isEffortSupported(backend, model, null, reasoningOptions)).toBe(true);
    }
    expect(resolveEffortOptions(backend, 'without-off', reasoningOptions)).toEqual(['high']);
    expect(resolveEffortOptions(backend, 'empty', reasoningOptions)).toEqual([]);
    expect(resolveEffortOptions(backend, 'unknown', undefined)).not.toContain('none');
  });

  it('treats an explicitly empty entry as "no efforts", not as a missing answer', () => {
    const reasoningOptions = {
      '': [{ value: 'low', label: 'Low' }],
      'no-reasoning-model': [],
    };

    expect(resolveEffortOptions('claude', 'no-reasoning-model', reasoningOptions)).toEqual([]);
    expect(resolveEffortOptions('codex', 'no-reasoning-model', reasoningOptions)).toEqual([]);
    // An empty set supports nothing but "unset".
    expect(isEffortSupported('claude', 'no-reasoning-model', 'medium', reasoningOptions)).toBe(false);
    expect(isEffortSupported('claude', 'no-reasoning-model', null, reasoningOptions)).toBe(true);
    // A key nobody wrote still falls back, including to the catalog default set.
    expect(resolveEffortOptions('claude', 'unlisted-model', reasoningOptions)).toEqual(['low']);
  });

  it('honours OpenCode per-model answers and keeps its fallback for models it never names', () => {
    // OpenCode has no "" default set, and its Hub catalog now states an entry
    // per model like the others: reading only claude/codex would throw those
    // answers away and offer the broad superset for a model that has none.
    const reasoningOptions = {
      'openrouter/anthropic/claude-x': [],
      'custom/hub-only': [{ value: 'high', label: 'High' }],
    };

    expect(resolveEffortOptions('opencode', 'openrouter/anthropic/claude-x', reasoningOptions)).toEqual([]);
    expect(isEffortSupported('opencode', 'openrouter/anthropic/claude-x', 'high', reasoningOptions)).toBe(false);
    expect(resolveEffortOptions('opencode', 'custom/hub-only', reasoningOptions)).toEqual(['high']);
    // A typed or Direct-mode model the catalog does not name keeps the superset.
    expect(resolveEffortOptions('opencode', 'typed/unknown', reasoningOptions)).toEqual([
      'minimal',
      'low',
      'medium',
      'high',
      'xhigh',
      'max',
    ]);
    expect(resolveEffortOptions('opencode', null, reasoningOptions)).toHaveLength(6);
  });

  it('drops the backend-chooses sentinel from an entry instead of offering it', () => {
    const reasoningOptions = {
      'sentinel-plus': [{ value: '__default__', label: 'Default' }, { value: 'ultra', label: 'Ultra' }],
      // The sentinel alone leaves nothing to select, and "the backend picks" is
      // not a reason to offer the generic ladder instead.
      'sentinel-only': [{ value: '__default__', label: 'Default' }],
    };

    expect(resolveEffortOptions('claude', 'sentinel-plus', reasoningOptions)).toEqual(['ultra']);
    expect(resolveEffortOptions('claude', 'sentinel-only', reasoningOptions)).toEqual([]);
  });

  it('does not mistake inherited Object properties for catalog entries', () => {
    expect(resolveEffortOptions('codex', 'constructor', {})).toEqual([
      'minimal',
      'low',
      'medium',
      'high',
      'xhigh',
    ]);
  });

  it('orders selected efforts by the unified vocabulary, unknowns last', () => {
    expect(sortEffortsByVocabulary(['ultra', 'low', 'custom-b', 'none', 'max', 'custom-a'])).toEqual([
      'none',
      'low',
      'max',
      'ultra',
      'custom-a',
      'custom-b',
    ]);
  });

  it('keeps the OpenCode family fallback inside the vocabulary without ultra', () => {
    // Same set the OpenCode provider form offers and the save path accepts
    // (`vibe/opencode_config.py:_VALID_REASONING_VARIANTS` minus `none`).
    expect(EFFORT_BY_BACKEND.opencode).toEqual(
      [...REASONING_EFFORTS].filter((effort) => effort !== 'ultra' && effort !== 'none'),
    );
    for (const efforts of Object.values(EFFORT_BY_BACKEND)) expect(efforts).not.toContain('none');
  });
});
