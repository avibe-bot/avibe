import { describe, expect, it } from 'vitest';

import { chatSessionViewState } from './chatSessionViewState';

describe('chatSessionViewState', () => {
  it('keeps an unhydrated Session in loading state', () => {
    expect(chatSessionViewState({
      routeSessionId: 'session-new',
      loadedSessionId: null,
      hydratedTranscriptSessionId: null,
      failedBootstrapSessionId: null,
    })).toBe('loading');
  });

  it('keeps a row from the previous route out of the new chat', () => {
    expect(chatSessionViewState({
      routeSessionId: 'session-new',
      loadedSessionId: 'session-old',
      hydratedTranscriptSessionId: 'session-old',
      failedBootstrapSessionId: 'session-old',
    })).toBe('loading');
  });

  it('keeps the route loading when Session-row recovery wins the bootstrap race', () => {
    expect(chatSessionViewState({
      routeSessionId: 'session-new',
      loadedSessionId: 'session-new',
      hydratedTranscriptSessionId: null,
      failedBootstrapSessionId: null,
    })).toBe('loading');
  });

  it('shows a successfully hydrated route even during a background refresh', () => {
    expect(chatSessionViewState({
      routeSessionId: 'session-new',
      loadedSessionId: 'session-new',
      hydratedTranscriptSessionId: 'session-new',
      failedBootstrapSessionId: 'session-new',
    })).toBe('ready');
  });

  it('waits for both the current Session row and its transcript in either completion order', () => {
    const routeSessionId = 'session-new';
    const states = [
      chatSessionViewState({
        routeSessionId,
        loadedSessionId: routeSessionId,
        hydratedTranscriptSessionId: null,
        failedBootstrapSessionId: null,
      }),
      chatSessionViewState({
        routeSessionId,
        loadedSessionId: null,
        hydratedTranscriptSessionId: routeSessionId,
        failedBootstrapSessionId: null,
      }),
      chatSessionViewState({
        routeSessionId,
        loadedSessionId: routeSessionId,
        hydratedTranscriptSessionId: routeSessionId,
        failedBootstrapSessionId: null,
      }),
    ];

    expect(states).toEqual(['loading', 'loading', 'ready']);
  });

  it('shows a route-owned bootstrap failure even if Session-row recovery succeeded', () => {
    expect(chatSessionViewState({
      routeSessionId: 'session-new',
      loadedSessionId: 'session-new',
      hydratedTranscriptSessionId: null,
      failedBootstrapSessionId: 'session-new',
    })).toBe('failed');
  });
});
