import { describe, expect, it } from 'vitest';

import { chatSessionViewState } from './chatSessionViewState';

describe('chatSessionViewState', () => {
  it.each([true, false])(
    'keeps an empty error-free session in loading state when loading=%s',
    (loading) => {
      expect(chatSessionViewState({
        routeSessionId: 'session-new',
        loadedSessionId: null,
        loading,
        error: null,
      })).toBe('loading');
    },
  );

  it('keeps a row from the previous route out of the new chat', () => {
    expect(chatSessionViewState({
      routeSessionId: 'session-new',
      loadedSessionId: 'session-old',
      loading: false,
      error: null,
    })).toBe('loading');
  });

  it('shows content only for the current route and failure only for an explicit error', () => {
    expect(chatSessionViewState({
      routeSessionId: 'session-new',
      loadedSessionId: 'session-new',
      loading: true,
      error: null,
    })).toBe('ready');
    expect(chatSessionViewState({
      routeSessionId: 'session-new',
      loadedSessionId: null,
      loading: false,
      error: 'Session unavailable',
    })).toBe('failed');
  });
});
