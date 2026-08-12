import type { TFunction } from 'i18next';
import { describe, expect, it } from 'vitest';

import {
  formatMemoryStatusFact,
  formatMemoryStatusRuntimeFact,
  formatMemoryStatusTimestamp,
  memoryStatusAnomalyLabel,
  memoryStatusClearRecoveryStateLabel,
  memoryStatusHealthLabel,
  memoryStatusRuntimeFactLabel,
  memoryStatusSourceBadgeVariant,
  memoryStatusSourceDisplayState,
  memoryStatusSourceReasonLabel,
  memoryStatusSourceStateLabel,
  type SourceState,
} from './memoryStatusPresentation';

const t = ((key: string) => key) as TFunction;

describe('memory status source presentation', () => {
  it.each(['available', 'partial', 'stale'] as const)(
    'downgrades unobserved %s evidence to unknown',
    (status) => {
      expect(memoryStatusSourceDisplayState({ status, observed_at: null })).toBe('unknown');
    },
  );

  it('preserves observed and explicitly unavailable source states', () => {
    expect(memoryStatusSourceDisplayState({ status: 'partial', observed_at: '2026-08-08T12:00:00Z' })).toBe('partial');
    expect(memoryStatusSourceDisplayState({ status: 'unavailable', observed_at: null })).toBe('unavailable');
    expect(memoryStatusSourceStateLabel(t, 'stale')).toBe('memory.processingRecord.sourceState.stale');
  });

  it.each<[SourceState, string]>([
    ['available', 'success'],
    ['partial', 'warning'],
    ['stale', 'warning'],
    ['unknown', 'secondary'],
    ['unavailable', 'destructive'],
  ])('uses the %s source badge variant %s', (status, variant) => {
    expect(memoryStatusSourceBadgeVariant(status)).toBe(variant);
  });

  it.each([
    ['memory_sidecar_unavailable', 'errors.memory_sidecar_unavailable'],
    ['missing', 'memory.log.reason.missing'],
    ['runs_busy', 'memory.log.reason.runsBusy'],
    ['future_source_reason', 'future_source_reason'],
  ])('maps source reason %s to %s', (reason, expected) => {
    expect(memoryStatusSourceReasonLabel(t, reason)).toBe(expected);
  });
});

describe('memory status enum labels', () => {
  it.each([
    ['kind', 'boot_recovery', 'memory.status.failureLog.kind.boot_recovery'],
    ['state', 'manual_required', 'memory.processingRecord.anomalyState.manualRequired'],
    ['operation', 'flush', 'memory.processingRecord.anomalyOperation.flush'],
    ['kind', 'future_kind', 'future_kind'],
    ['state', 'future_state', 'future_state'],
    ['operation', 'future_operation', 'future_operation'],
  ] as const)('maps anomaly %s value %s to %s', (group, value, expected) => {
    expect(memoryStatusAnomalyLabel(t, group, value)).toBe(expected);
  });

  it.each([
    ['preparing', 'memory.processingRecord.clearRecovery.state.preparing'],
    ['prepared', 'memory.processingRecord.clearRecovery.state.prepared'],
    ['deleting', 'memory.processingRecord.clearRecovery.state.deleting'],
    ['recovery_needed', 'memory.processingRecord.clearRecovery.state.recoveryNeeded'],
    ['future_state', 'future_state'],
  ])('maps clear recovery state %s to %s', (state, expected) => {
    expect(memoryStatusClearRecoveryStateLabel(t, state)).toBe(expected);
  });

  it.each([
    ['ok', 'memory.processingRecord.runtime.healthStatus.ok'],
    ['future_health_state', 'future_health_state'],
  ])('maps provider health %s to %s', (health, expected) => {
    expect(memoryStatusHealthLabel(t, health)).toBe(expected);
  });
});

describe('memory status runtime facts', () => {
  it.each([
    ['capability', 'embed', 'memory.processingRecord.runtime.fact.capability.embed'],
    ['cascade', 'prune_stale_seconds', 'memory.processingRecord.runtime.fact.cascade.pruneStaleSeconds'],
    ['recorder', 'state', 'memory.processingRecord.runtime.fact.recorder.state'],
    ['capability', 'future_capability', 'future_capability'],
    ['cascade', 'future_counter', 'future_counter'],
    ['recorder', 'future_field', 'future_field'],
  ] as const)('maps %s fact label %s to %s', (group, name, expected) => {
    expect(memoryStatusRuntimeFactLabel(t, group, name)).toBe(expected);
  });

  it('localizes known fact values and preserves future diagnostics', () => {
    expect(formatMemoryStatusRuntimeFact(t, 'capability', 'embed', false)).toBe(
      'memory.processingRecord.runtime.fact.boolean.false',
    );
    expect(formatMemoryStatusRuntimeFact(t, 'cascade', 'reasons', ['drain_failures', 'future_reason', 7])).toBe(
      'memory.processingRecord.runtime.fact.cascadeReason.drainFailures, future_reason, 7',
    );
    expect(formatMemoryStatusRuntimeFact(t, 'recorder', 'state', 'degraded')).toBe(
      'memory.processingRecord.runtime.fact.recorderState.degraded',
    );
    expect(formatMemoryStatusRuntimeFact(t, 'recorder', 'reason', 'call_log_corrupt')).toBe(
      'memory.processingRecord.runtime.fact.recorderReason.callLogCorrupt',
    );
    expect(formatMemoryStatusRuntimeFact(t, 'recorder', 'state', 'future_state')).toBe('future_state');
    expect(formatMemoryStatusRuntimeFact(t, 'cascade', 'future_counter', 12)).toBe('12');
  });
});

describe('memory status formatting', () => {
  it('formats null, invalid, and valid timestamps without inventing data', () => {
    const valid = '2026-08-08T12:00:00Z';
    expect(formatMemoryStatusTimestamp(null)).toBe('-');
    expect(formatMemoryStatusTimestamp(undefined)).toBe('-');
    expect(formatMemoryStatusTimestamp('not-a-timestamp')).toBe('not-a-timestamp');
    expect(formatMemoryStatusTimestamp(valid)).toBe(new Date(valid).toLocaleString());
  });

  it.each([
    [null, '-'],
    [undefined, '-'],
    [true, 'true'],
    [false, 'false'],
    ['diagnostic', 'diagnostic'],
    [42, '42'],
    [{ answer: 42 }, '{"answer":42}'],
    [[1, 'two'], '[1,"two"]'],
  ])('serializes fact value %j as %s', (value, expected) => {
    expect(formatMemoryStatusFact(value)).toBe(expected);
  });

  it('falls back when a fact cannot be serialized', () => {
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    expect(formatMemoryStatusFact(circular)).toBe('-');
  });
});
