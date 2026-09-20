import { describe, expect, it } from 'vitest';

import { pendingInitialMessageHandoff } from './chatInitialMessage';

const candidate = (overrides: Partial<Parameters<typeof pendingInitialMessageHandoff>[0]> = {}) => ({
  handledSessionId: null,
  loadedSessionId: 'ses_1',
  loading: false,
  locationState: { initialMessage: 'hello' },
  routeSurfaceActive: true,
  sessionId: 'ses_1',
  ...overrides,
});

describe('initial chat message handoff', () => {
  it('waits until the retained route is foreground before consuming the message', () => {
    expect(pendingInitialMessageHandoff(candidate({ routeSurfaceActive: false }))).toBeNull();
    expect(pendingInitialMessageHandoff(candidate())).toEqual({
      message: 'hello',
      sessionId: 'ses_1',
    });
  });

  it('requires the matching loaded session and an unhandled message', () => {
    expect(pendingInitialMessageHandoff(candidate({ loading: true }))).toBeNull();
    expect(pendingInitialMessageHandoff(candidate({ loadedSessionId: 'ses_2' }))).toBeNull();
    expect(pendingInitialMessageHandoff(candidate({ handledSessionId: 'ses_1' }))).toBeNull();
    expect(pendingInitialMessageHandoff(candidate({ locationState: null }))).toBeNull();
  });
});
