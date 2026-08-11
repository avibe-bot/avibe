import { describe, expect, it } from 'vitest';

import { parseMemoryFactoryResetResult } from './memoryFactoryReset';

const roots = [
  { path: 'memory' as const, existed: true, deleted: true },
  { path: 'state/memory' as const, existed: true, deleted: true },
] as const;

describe('parseMemoryFactoryResetResult', () => {
  it('accepts the exact completed contract', () => {
    expect(parseMemoryFactoryResetResult({
      ok: true, result: 'completed', data_deleted: true, data_remaining: false, roots: [...roots],
    }).result).toBe('completed');
  });

  it.each([
    { status: 'completed' },
    { roots_deleted: { memory: true, state_memory: true } },
    { ok: true, result: 'completed', data_deleted: true, data_remaining: false, roots: [...roots], extra: true },
  ])('rejects aliases or extra fields: %o', (value) => {
    expect(() => parseMemoryFactoryResetResult(value)).toThrow('Invalid Memory factory reset response');
  });

  it('rejects malformed or duplicated root outcomes', () => {
    expect(() => parseMemoryFactoryResetResult({
      ok: true, result: 'completed', data_deleted: true, data_remaining: false,
      roots: [roots[0], { ...roots[0] }],
    })).toThrow('Invalid Memory factory reset response');
  });
});
