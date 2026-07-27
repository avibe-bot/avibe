import { describe, expect, it } from 'vitest';

import { classifyMemoryResult, memoryErrorMessage } from './memoryRead';


describe('classifyMemoryResult', () => {
  it('accepts the tagged success envelope', () => {
    const result = { status: 'ok', items: [] };
    expect(classifyMemoryResult(result)).toEqual({ kind: 'ok', value: result });
  });

  it('reports a closed failure code without claiming it is forbidden', () => {
    expect(classifyMemoryResult({ status: 'failed', error: 'memory_sidecar_unavailable' })).toEqual({
      kind: 'failed',
      code: 'memory_sidecar_unavailable',
      forbidden: false,
    });
  });

  it('flags the non-loopback forbidden body while still carrying its code', () => {
    expect(classifyMemoryResult({ status: 'failed', error: 'memory_disabled' })).toEqual({
      kind: 'failed',
      code: 'memory_disabled',
      forbidden: true,
    });
  });

  it('rejects an untagged dependency-missing body that only carries an error', () => {
    expect(classifyMemoryResult({ error: 'memory_runtime_missing' })).toEqual({
      kind: 'failed',
      code: 'memory_runtime_missing',
      forbidden: false,
    });
  });

  it('lets the failure log declare its own untagged success shape', () => {
    const hasItems = (value: unknown): boolean => Array.isArray((value as { items?: unknown })?.items);
    const result = { items: [], retention_days: 90 };
    expect(classifyMemoryResult(result, hasItems)).toEqual({ kind: 'ok', value: result });
    expect(classifyMemoryResult({ status: 'failed', error: 'memory_disabled' }, hasItems)).toEqual({
      kind: 'failed',
      code: 'memory_disabled',
      forbidden: true,
    });
  });
});


describe('memoryErrorMessage', () => {
  const t = ((key: string, options?: { defaultValue?: string }) =>
    key === 'errors.memory_known' ? 'Known failure' : options?.defaultValue ?? key) as never;

  it('translates a known closed code', () => {
    expect(memoryErrorMessage(t, 'memory_known')).toBe('Known failure');
  });

  it('falls back to the raw code so an unmapped failure stays diagnosable', () => {
    expect(memoryErrorMessage(t, 'memory_unmapped')).toBe('memory_unmapped');
  });

  it('names an absent code rather than rendering an empty error', () => {
    expect(memoryErrorMessage(t, null)).toBe('common.unknown');
  });
});
