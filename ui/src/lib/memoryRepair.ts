import type { MemoryCascadeHealth, MemoryRuntimeRepairResult } from '../context/ApiContext';

const REPAIR_ERRORS = new Set([
  'memory_disabled',
  'memory_operation_in_progress',
  'memory_runtime_unsupported',
  'memory_store_unavailable',
  'memory_sidecar_unavailable',
  'memory_repair_failed',
]);

const HEALTH_KEYS = [
  'healthy',
  'reasons',
  'pending',
  'failed_permanent',
  'failed_retryable',
  'drain_consecutive_failures',
  'unrecoverable_total',
  'optimize_failure_streak',
  'prune_stale_seconds',
] as const;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

const hasExactKeys = (value: Record<string, unknown>, keys: readonly string[]): boolean => {
  const actual = Object.keys(value).sort();
  return actual.length === keys.length && actual.every((key, index) => key === [...keys].sort()[index]);
};

const isHealth = (value: unknown): value is MemoryCascadeHealth => {
  if (!isRecord(value) || !hasExactKeys(value, HEALTH_KEYS)) return false;
  if (typeof value.healthy !== 'boolean') return false;
  if (
    !Array.isArray(value.reasons) ||
    value.reasons.length > 8 ||
    value.reasons.some((reason) => typeof reason !== 'string' || new TextEncoder().encode(reason).length > 64)
  ) return false;
  for (const key of HEALTH_KEYS) {
    if (key === 'healthy' || key === 'reasons' || key === 'prune_stale_seconds') continue;
    const number = value[key];
    if (typeof number !== 'number' || !Number.isInteger(number) || number < 0 || number > 2 ** 53) return false;
  }
  const stale = value.prune_stale_seconds;
  return typeof stale === 'number' && Number.isFinite(stale) && stale >= 0 && stale <= 10 ** 12;
};

const invalidRepairResponse = (): MemoryRuntimeRepairResult => ({
  ok: false,
  error: 'memory_repair_failed',
  result: 'failed',
});

/** Parse the frozen final Repair envelope; every malformed or extended body fails closed. */
export const parseMemoryRuntimeRepair = (value: unknown): MemoryRuntimeRepairResult => {
  if (!isRecord(value)) return invalidRepairResponse();
  if (value.ok === true) {
    if (!hasExactKeys(value, ['ok', 'result', 'health']) || !isHealth(value.health)) {
      return invalidRepairResponse();
    }
    if (value.result !== 'completed' && value.result !== 'completed_with_warnings') {
      return invalidRepairResponse();
    }
    if ((value.result === 'completed') !== value.health.healthy) return invalidRepairResponse();
    return {
      ok: true,
      result: value.result,
      health: value.health,
    };
  }
  if (!hasExactKeys(value, ['ok', 'error', 'result']) || value.ok !== false) {
    return invalidRepairResponse();
  }
  if (typeof value.error !== 'string' || !REPAIR_ERRORS.has(value.error)) return invalidRepairResponse();
  if (value.error === 'memory_repair_failed') {
    if (value.result !== 'interrupted' && value.result !== 'timed_out' && value.result !== 'failed') {
      return invalidRepairResponse();
    }
  } else if (value.result !== 'failed') {
    return invalidRepairResponse();
  }
  return {
    ok: false,
    error: value.error,
    result: value.result,
  };
};

type PostJson = (
  path: string,
  payload: unknown,
  options: { handleError: boolean },
) => Promise<unknown>;

/** Execute the one frozen public Repair request and parse its final response. */
export const requestMemoryRuntimeRepair = async (postJson: PostJson): Promise<MemoryRuntimeRepairResult> =>
  parseMemoryRuntimeRepair(
    await postJson('/api/memory/runtime/repair', { confirm: true }, { handleError: false }),
  );
