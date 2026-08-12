export type MemoryFactoryResetRootOutcome = {
  path: 'memory' | 'state/memory';
  existed: boolean;
  deleted: boolean;
  error?: string;
};

export type MemoryFactoryResetResult =
  | { ok: true; result: 'completed'; data_deleted: boolean; data_remaining: boolean; roots: [MemoryFactoryResetRootOutcome, MemoryFactoryResetRootOutcome] }
  | { ok: false; result: 'partial' | 'deleted_activation_failed' | 'failed'; error: string; data_deleted: boolean; data_remaining: boolean; roots: [MemoryFactoryResetRootOutcome, MemoryFactoryResetRootOutcome]; reason?: string }
  | { ok: false; result: 'failed'; error: 'memory_operation_in_progress'; roots?: never };

const ROOTS = new Set(['memory', 'state/memory']);
const SUCCESS_KEYS = new Set(['ok', 'result', 'data_deleted', 'data_remaining', 'roots']);
const FAILURE_KEYS = new Set([...SUCCESS_KEYS, 'error', 'reason']);
const OPERATION_IN_PROGRESS_KEYS = new Set(['ok', 'error', 'result']);
const ROOT_KEYS = new Set(['path', 'existed', 'deleted', 'error']);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const hasOnlyKeys = (value: Record<string, unknown>, keys: Set<string>): boolean =>
  Object.keys(value).every((key) => keys.has(key));
const isRoot = (value: unknown): value is MemoryFactoryResetRootOutcome => {
  if (!isRecord(value) || !hasOnlyKeys(value, ROOT_KEYS)) return false;
  return typeof value.path === 'string' && ROOTS.has(value.path)
    && typeof value.existed === 'boolean' && typeof value.deleted === 'boolean'
    && (value.error === undefined || typeof value.error === 'string');
};

/** Parse only the closed factory-reset response contract; aliases fail closed. */
export const parseMemoryFactoryResetResult = (value: unknown): MemoryFactoryResetResult => {
  if (!isRecord(value) || typeof value.ok !== 'boolean') {
    throw new Error('Invalid Memory factory reset response');
  }
  if (!value.ok && value.error === 'memory_operation_in_progress') {
    if (!hasOnlyKeys(value, OPERATION_IN_PROGRESS_KEYS) || value.result !== 'failed') {
      throw new Error('Invalid Memory factory reset response');
    }
    return value as MemoryFactoryResetResult;
  }
  if (!hasOnlyKeys(value, value.ok ? SUCCESS_KEYS : FAILURE_KEYS)) {
    throw new Error('Invalid Memory factory reset response');
  }
  if (!Array.isArray(value.roots) || value.roots.length !== 2 || !value.roots.every(isRoot)
    || new Set(value.roots.map((root) => root.path)).size !== 2
    || typeof value.data_deleted !== 'boolean' || typeof value.data_remaining !== 'boolean') {
    throw new Error('Invalid Memory factory reset response');
  }
  if (value.ok && value.result !== 'completed') throw new Error('Invalid Memory factory reset response');
  if (!value.ok && (value.result !== 'partial' && value.result !== 'deleted_activation_failed' && value.result !== 'failed')) {
    throw new Error('Invalid Memory factory reset response');
  }
  if (!value.ok && (typeof value.error !== 'string' || (value.reason !== undefined && typeof value.reason !== 'string'))) {
    throw new Error('Invalid Memory factory reset response');
  }
  return value as MemoryFactoryResetResult;
};
