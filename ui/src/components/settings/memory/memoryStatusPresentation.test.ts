import type { TFunction } from 'i18next';
import { describe, expect, it } from 'vitest';

import {
  memoryStatusHealthLabel,
  memoryStatusSourceReasonLabel,
  memoryStatusSourceDisplayState,
  memoryStatusSourceStateLabel,
} from './memoryStatusPresentation';

const t = ((key: string) => key) as TFunction;

describe('memory status presentation', () => {
  it('downgrades unobserved source evidence to unknown', () => {
    expect(memoryStatusSourceDisplayState({ status: 'available', observed_at: null })).toBe('unknown');
  });

  it('keeps observed source states and health labels', () => {
    expect(memoryStatusSourceDisplayState({ status: 'unavailable', observed_at: null })).toBe('unavailable');
    expect(memoryStatusSourceStateLabel(t, 'stale')).toContain('sourceState.stale');
    expect(memoryStatusHealthLabel(t, 'ok')).toContain('healthStatus.ok');
  });

  it('localizes legacy rerun source reasons through memory errors', () => {
    expect(memoryStatusSourceReasonLabel(t, 'memory_clear_legacy_state_requires_rerun'))
      .toContain('errors.memory_clear_legacy_state_requires_rerun');
  });

  it('localizes a cloud capability pause through the closed Memory error vocabulary', () => {
    expect(memoryStatusSourceReasonLabel(t, 'memory_capability_unavailable'))
      .toContain('errors.memory_capability_unavailable');
  });

  it('localizes native Processing Record source reasons through the closed vocabulary', () => {
    expect([
      'native_memcells_unavailable',
      'native_runs_unavailable',
      'native_semantic_unavailable',
      'memory_failure_history_unavailable',
    ].map((reason) => memoryStatusSourceReasonLabel(t, reason))).toEqual([
      'memory.processingRecord.reason.native_memcells_unavailable',
      'memory.processingRecord.reason.native_runs_unavailable',
      'memory.processingRecord.reason.native_semantic_unavailable',
      'memory.processingRecord.reason.memory_failure_history_unavailable',
    ]);
  });
});
