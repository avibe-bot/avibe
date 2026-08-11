import { describe, expect, it } from 'vitest';

import { parseMemoryRuntimeRepair, requestMemoryRuntimeRepair } from './memoryRepair';
import { memoryCascadeHealth } from '../test/memoryFixtures';

const health = memoryCascadeHealth();

const invalidRepairResponse = {
  ok: false,
  error: 'memory_repair_failed',
  result: 'failed',
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
    expect(parseMemoryRuntimeRepair(value)).toEqual(invalidRepairResponse);
  });

  it.each([
    ['memory_disabled', 'failed'],
    ['memory_operation_in_progress', 'failed'],
    ['memory_runtime_unsupported', 'failed'],
    ['memory_store_unavailable', 'failed'],
    ['memory_sidecar_unavailable', 'failed'],
    ['memory_repair_failed', 'failed'],
    ['memory_repair_failed', 'interrupted'],
    ['memory_repair_failed', 'timed_out'],
  ])('accepts the declared failure %s with result %s', (error, result) => {
    expect(parseMemoryRuntimeRepair({ ok: false, error, result })).toEqual({
      ok: false,
      error,
      result,
    });
  });

  it('accepts every bounded health field at its declared maximum', () => {
    const boundedHealth = memoryCascadeHealth({
      reasons: Array.from({ length: 8 }, () => 'r'.repeat(64)),
      pending: 2 ** 53,
      failed_permanent: 2 ** 53,
      failed_retryable: 2 ** 53,
      drain_consecutive_failures: 2 ** 53,
      unrecoverable_total: 2 ** 53,
      optimize_failure_streak: 2 ** 53,
      prune_stale_seconds: 10 ** 12,
    });

    expect(parseMemoryRuntimeRepair({
      ok: true,
      result: 'completed',
      health: boundedHealth,
    })).toEqual({ ok: true, result: 'completed', health: boundedHealth });
  });

  it.each([
    ['too many reasons', { reasons: Array.from({ length: 9 }, () => 'reason') }],
    ['an overlong UTF-8 reason', { reasons: ['\u00e9'.repeat(33)] }],
    ['a negative counter', { pending: -1 }],
    ['a fractional counter', { failed_permanent: 0.5 }],
    ['an oversized counter', { failed_retryable: 2 ** 53 + 2 }],
    ['a non-finite counter', { drain_consecutive_failures: Number.POSITIVE_INFINITY }],
    ['negative stale seconds', { prune_stale_seconds: -1 }],
    ['non-finite stale seconds', { prune_stale_seconds: Number.NaN }],
    ['oversized stale seconds', { prune_stale_seconds: 10 ** 12 + 1 }],
  ])('fails closed when health contains %s', (_description, overrides) => {
    expect(parseMemoryRuntimeRepair({
      ok: true,
      result: 'completed',
      health: memoryCascadeHealth(overrides),
    })).toEqual(invalidRepairResponse);
  });

  it.each([
    { ok: false, error: 'future_repair_error', result: 'failed' },
    { ok: false, error: 'memory_disabled', result: 'timed_out' },
    { ok: false, error: 'memory_repair_failed', result: 'completed' },
  ])('fails closed for undeclared failure combinations: %j', (value) => {
    expect(parseMemoryRuntimeRepair(value)).toEqual(invalidRepairResponse);
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
