import { describe, expect, it } from 'vitest';

import { getTunnelQualityDisplayState, getTunnelRequestPathDisplayState } from './tunnelQuality';

describe('getTunnelQualityDisplayState', () => {
  it('lets degraded health override a good latency grade', () => {
    expect(getTunnelQualityDisplayState({ state: 'degraded', grade: 'good' }, true)).toBe('degraded');
  });

  it('preserves healthy grades and treats stale samples as unknown', () => {
    expect(getTunnelQualityDisplayState({ state: 'healthy', grade: 'fair' }, true)).toBe('fair');
    expect(getTunnelQualityDisplayState({ state: 'degraded', grade: 'good' }, false)).toBe('unknown');
  });
});

describe('getTunnelRequestPathDisplayState', () => {
  const requestPath = {
    source: 'synthetic_local' as const,
    status: 'healthy' as const,
    confidence: 'high' as const,
    window_seconds: 180,
    sample_count: 36,
    success_count: 36,
    latency_ms: { p50: 120, p95: 180, p99: 220, max: 240 },
    failure_rate: 0,
    slow_request_rate: { over_500_ms: 0, over_1000_ms: 0, over_2000_ms: 0 },
    baseline_p95_ms: null,
  };

  it('renders failed request windows as unavailable instead of latency', () => {
    expect(getTunnelRequestPathDisplayState({
      ...requestPath,
      status: 'unavailable',
      success_count: 0,
    })).toBe('unavailable');
  });

  it('renders usable high-confidence request latency', () => {
    expect(getTunnelRequestPathDisplayState(requestPath)).toBe('latency');
  });
});
