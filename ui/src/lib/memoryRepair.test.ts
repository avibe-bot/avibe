import { describe, expect, it } from 'vitest';

import { parseMemoryRuntimeRepair, requestMemoryRuntimeRepair } from './memoryRepair';

const health = {
  healthy: true,
  reasons: [],
  pending: 0,
  failed_permanent: 0,
  failed_retryable: 0,
  drain_consecutive_failures: 0,
  unrecoverable_total: 0,
  optimize_failure_streak: 0,
  prune_stale_seconds: 0,
};

describe('parseMemoryRuntimeRepair', () => {
  it('accepts the exact healthy completion envelope', () => {
    expect(parseMemoryRuntimeRepair({ ok: true, result: 'completed', health })).toEqual({
      ok: true,
      result: 'completed',
      health,
    });
  });

  it('accepts the exact warning completion envelope', () => {
    expect(parseMemoryRuntimeRepair({
      ok: true,
      result: 'completed_with_warnings',
      health: { ...health, healthy: false, reasons: ['drain_failures'] },
    })).toMatchObject({ ok: true, result: 'completed_with_warnings' });
  });

  it.each([
    { ok: true, result: 'completed', health, extra: true },
    { ok: true, result: 'completed', health: { ...health, unknown: 1 } },
    { ok: true, result: 'completed_with_warnings', health },
    { ok: false, error: 'memory_repair_failed', result: 'failed', extra: true },
    { status: 'failed', error: 'memory_repair_failed' },
  ])('fails closed for malformed or extended responses: %j', (value) => {
    expect(parseMemoryRuntimeRepair(value)).toEqual({
      ok: false,
      error: 'memory_repair_failed',
      result: 'failed',
    });
  });

  it('accepts only the declared failure result combinations', () => {
    expect(parseMemoryRuntimeRepair({
      ok: false,
      error: 'memory_operation_in_progress',
      result: 'failed',
    })).toEqual({ ok: false, error: 'memory_operation_in_progress', result: 'failed' });
    expect(parseMemoryRuntimeRepair({
      ok: false,
      error: 'memory_repair_failed',
      result: 'timed_out',
    })).toEqual({ ok: false, error: 'memory_repair_failed', result: 'timed_out' });
  });
});

describe('requestMemoryRuntimeRepair', () => {
  it('posts the exact confirmed Repair contract and parses the response', async () => {
    const postJson = async (path: string, payload: unknown, options: { handleError: boolean }) => {
      expect(path).toBe('/api/memory/runtime/repair');
      expect(payload).toEqual({ confirm: true });
      expect(options).toEqual({ handleError: false });
      return { ok: true, result: 'completed', health };
    };

    await expect(requestMemoryRuntimeRepair(postJson)).resolves.toEqual({
      ok: true,
      result: 'completed',
      health,
    });
  });
});
