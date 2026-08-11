import type { MemoryCascadeHealth } from '../context/ApiContext';

export const memoryCascadeHealth = (
  overrides: Partial<MemoryCascadeHealth> = {},
): MemoryCascadeHealth => ({
  healthy: true,
  reasons: [],
  pending: 0,
  failed_permanent: 0,
  failed_retryable: 0,
  drain_consecutive_failures: 0,
  unrecoverable_total: 0,
  optimize_failure_streak: 0,
  prune_stale_seconds: 0,
  ...overrides,
});
