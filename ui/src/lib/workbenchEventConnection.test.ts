import { describe, expect, it } from 'vitest';

import { eventSourceErrorConnectionState } from './workbenchEventConnection';

describe('eventSourceErrorConnectionState', () => {
  it('keeps transient EventSource failures in the automatic retry state', () => {
    expect(eventSourceErrorConnectionState(0)).toBe('reconnecting');
  });

  it('reserves disconnected for a stream that stopped retrying', () => {
    expect(eventSourceErrorConnectionState(2)).toBe('disconnected');
    expect(eventSourceErrorConnectionState(1)).toBe('disconnected');
  });
});
