import { describe, expect, it } from 'vitest';
import { composeReady } from './composeReady';

// The two composer preconditions ChatPage decides for Ask Vibey. The others in
// the same list (mounted, voice capture, a send in flight for files, disabled,
// read-only, switching away mid-insert) are answered by the composer and the
// receiver at insertion time; see desktopComposeReceiver.test.ts and
// Composer.appendLine.test.tsx.
describe('composeReady', () => {
  it.each([
    ['snapshot painted, saved draft not hydrated yet', { showPageActive: false, hydratedSessionId: null }, false],
    ['another chat hydrated, this one not yet', { showPageActive: false, hydratedSessionId: 'ses_other' }, false],
    ['hydrated, but the Show Page covers the composer', { showPageActive: true, hydratedSessionId: 'ses_main' }, false],
    ['hydrated and the conversation is shown', { showPageActive: false, hydratedSessionId: 'ses_main' }, true],
  ] as const)('%s', (_name, state, expected) => {
    expect(composeReady({ sessionId: 'ses_main', ...state })).toBe(expected);
  });
});
