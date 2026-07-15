import { describe, expect, it } from 'vitest';

import { getTunnelQualityDisplayState } from './tunnelQuality';

describe('getTunnelQualityDisplayState', () => {
  it('lets degraded health override a good latency grade', () => {
    expect(getTunnelQualityDisplayState({ state: 'degraded', grade: 'good' }, true)).toBe('degraded');
  });

  it('preserves healthy grades and treats stale samples as unknown', () => {
    expect(getTunnelQualityDisplayState({ state: 'healthy', grade: 'fair' }, true)).toBe('fair');
    expect(getTunnelQualityDisplayState({ state: 'degraded', grade: 'good' }, false)).toBe('unknown');
  });
});
