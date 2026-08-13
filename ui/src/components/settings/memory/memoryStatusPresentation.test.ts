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

  it('localizes legacy abort source reasons through memory errors', () => {
    expect(memoryStatusSourceReasonLabel(t, 'memory_clear_legacy_abort_unsupported'))
      .toContain('errors.memory_clear_legacy_abort_unsupported');
  });
});
