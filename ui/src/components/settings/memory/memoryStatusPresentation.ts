import type { TFunction } from 'i18next';

import type { MemoryLogSourceStatus, MemoryStatus } from '../../../context/ApiContext';
import { memoryErrorMessage } from '../../../lib/memoryRead';
import { memoryLogEnumLabel } from './memoryLog';

export type RuntimeFactGroup = keyof typeof RUNTIME_FACT_LABEL_KEYS;
export type SourceState = MemoryStatus['source']['status'] | MemoryLogSourceStatus['status'];
export type BadgeVariant = 'success' | 'warning' | 'destructive' | 'info' | 'secondary';

type AnomalyLabelGroup = keyof typeof ANOMALY_LABEL_KEYS;

const SOURCE_BADGE_VARIANT: Record<SourceState, BadgeVariant> = {
  available: 'success',
  partial: 'warning',
  stale: 'warning',
  unknown: 'secondary',
  unavailable: 'destructive',
};

const ANOMALY_LABEL_KEYS = {
  kind: {
    boot_recovery: 'memory.status.failureLog.kind.boot_recovery',
    delivery_abandoned: 'memory.status.failureLog.kind.delivery_abandoned',
    distillation_rejected: 'memory.status.failureLog.kind.distillation_rejected',
    recorder_degraded: 'memory.status.failureLog.kind.recorder_degraded',
    result_unknown: 'memory.status.failureLog.kind.result_unknown',
  },
  state: {
    dead: 'memory.processingRecord.anomalyState.dead',
    degraded: 'memory.processingRecord.anomalyState.degraded',
    manual_required: 'memory.processingRecord.anomalyState.manualRequired',
    rejected: 'memory.processingRecord.anomalyState.rejected',
  },
  operation: {
    add: 'memory.processingRecord.anomalyOperation.add',
    flush: 'memory.processingRecord.anomalyOperation.flush',
    record: 'memory.processingRecord.anomalyOperation.record',
  },
} as const;


const HEALTH_STATUS_LABEL_KEYS = {
  ok: 'memory.processingRecord.runtime.healthStatus.ok',
} as const;

const MEMORY_SOURCE_ERROR_REASONS = new Set([
  'memory_disabled',
  'memory_runtime_missing',
  'memory_runtime_unsupported',
  'memory_runtime_install_failed',
  'memory_sidecar_unavailable',
  'memory_provider_timeout',
  'memory_provider_response_invalid',
  'memory_processing_failed',
  'memory_clear_failed',
  'memory_clear_legacy_state_requires_rerun',
  'memory_restart_failed',
]);

const RUNTIME_FACT_LABEL_KEYS = {
  capability: {
    llm: 'memory.processingRecord.runtime.fact.capability.llm',
    embed: 'memory.processingRecord.runtime.fact.capability.embed',
    rerank: 'memory.processingRecord.runtime.fact.capability.rerank',
    multimodal_llm: 'memory.processingRecord.runtime.fact.capability.multimodalLlm',
    parser: 'memory.processingRecord.runtime.fact.capability.parser',
    agentic_search: 'memory.processingRecord.runtime.fact.capability.agenticSearch',
    knowledge: 'memory.processingRecord.runtime.fact.capability.knowledge',
  },
  cascade: {
    healthy: 'memory.processingRecord.runtime.fact.cascade.healthy',
    reasons: 'memory.processingRecord.runtime.fact.cascade.reasons',
    pending: 'memory.processingRecord.runtime.fact.cascade.pending',
    failed_permanent: 'memory.processingRecord.runtime.fact.cascade.failedPermanent',
    failed_retryable: 'memory.processingRecord.runtime.fact.cascade.failedRetryable',
    drain_consecutive_failures: 'memory.processingRecord.runtime.fact.cascade.drainConsecutiveFailures',
    unrecoverable_total: 'memory.processingRecord.runtime.fact.cascade.unrecoverableTotal',
    optimize_failure_streak: 'memory.processingRecord.runtime.fact.cascade.optimizeFailureStreak',
    prune_stale_seconds: 'memory.processingRecord.runtime.fact.cascade.pruneStaleSeconds',
  },
  recorder: {
    state: 'memory.processingRecord.runtime.fact.recorder.state',
    reason: 'memory.processingRecord.runtime.fact.recorder.reason',
  },
} as const;

const CASCADE_REASON_LABEL_KEYS = {
  drain_failures: 'memory.processingRecord.runtime.fact.cascadeReason.drainFailures',
  optimize_stuck: 'memory.processingRecord.runtime.fact.cascadeReason.optimizeStuck',
  prune_stale: 'memory.processingRecord.runtime.fact.cascadeReason.pruneStale',
  health_probe_failed: 'memory.processingRecord.runtime.fact.cascadeReason.healthProbeFailed',
  unknown: 'memory.processingRecord.runtime.fact.cascadeReason.unknown',
} as const;

const RECORDER_STATE_LABEL_KEYS = {
  active: 'memory.processingRecord.runtime.fact.recorderState.active',
  degraded: 'memory.processingRecord.runtime.fact.recorderState.degraded',
  disabled: 'memory.processingRecord.runtime.fact.recorderState.disabled',
} as const;

const RECORDER_REASON_LABEL_KEYS = {
  writer_failures: 'memory.processingRecord.runtime.fact.recorderReason.writerFailures',
  serialization_failed: 'memory.processingRecord.runtime.fact.recorderReason.serializationFailed',
  call_log_corrupt: 'memory.processingRecord.runtime.fact.recorderReason.callLogCorrupt',
} as const;

const knownLabel = (t: TFunction, keys: Record<string, string>, value: string): string => {
  const key = Object.prototype.hasOwnProperty.call(keys, value) ? keys[value] : undefined;
  return key ? t(key) : value;
};

export function memoryStatusSourceDisplayState(
  source: { status: SourceState; observed_at: string | null },
): SourceState {
  return source.observed_at || source.status === 'unavailable' ? source.status : 'unknown';
}

export const memoryStatusSourceBadgeVariant = (state: SourceState): BadgeVariant => (
  SOURCE_BADGE_VARIANT[state]
);

export const memoryStatusSourceStateLabel = (t: TFunction, state: SourceState): string => (
  t(`memory.processingRecord.sourceState.${state}`)
);

export const memoryStatusAnomalyLabel = (
  t: TFunction,
  group: AnomalyLabelGroup,
  value: string,
): string => knownLabel(t, ANOMALY_LABEL_KEYS[group] as Record<string, string>, value);

export const memoryStatusHealthLabel = (t: TFunction, value: string): string => (
  knownLabel(t, HEALTH_STATUS_LABEL_KEYS, value)
);

export const memoryStatusSourceReasonLabel = (t: TFunction, value: string): string => (
  MEMORY_SOURCE_ERROR_REASONS.has(value)
    ? memoryErrorMessage(t, value)
    : memoryLogEnumLabel(t, 'reason', value)
);

export const formatMemoryStatusTimestamp = (value: string | null | undefined): string => {
  if (!value) return '-';
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? value : timestamp.toLocaleString();
};

export const formatMemoryStatusFact = (value: unknown): string => {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return '-';
  }
};

export const memoryStatusRuntimeFactLabel = (
  t: TFunction,
  group: RuntimeFactGroup,
  value: string,
): string => knownLabel(t, RUNTIME_FACT_LABEL_KEYS[group] as Record<string, string>, value);

const formatSecondsDuration = (t: TFunction, seconds: number): string => {
  const total = Math.round(seconds);
  if (total >= 3600) {
    return t('memory.processingRecord.runtime.fact.duration.hoursMinutes', {
      hours: Math.floor(total / 3600),
      minutes: Math.floor((total % 3600) / 60),
    });
  }
  if (total >= 60) {
    return t('memory.processingRecord.runtime.fact.duration.minutesSeconds', {
      minutes: Math.floor(total / 60),
      seconds: total % 60,
    });
  }
  return t('memory.processingRecord.runtime.fact.duration.seconds', { seconds: total });
};

export const formatMemoryStatusRuntimeFact = (
  t: TFunction,
  group: RuntimeFactGroup,
  name: string,
  value: unknown,
): string => {
  const knownField = Object.prototype.hasOwnProperty.call(RUNTIME_FACT_LABEL_KEYS[group], name);
  if (!knownField) return formatMemoryStatusFact(value);
  if (typeof value === 'boolean') {
    return t(`memory.processingRecord.runtime.fact.boolean.${value ? 'true' : 'false'}`);
  }
  if (group === 'cascade' && name === 'prune_stale_seconds' && typeof value === 'number') {
    return formatSecondsDuration(t, value);
  }
  if (group === 'cascade' && name === 'reasons' && Array.isArray(value)) {
    if (value.length === 0) return formatMemoryStatusFact(value);
    return value
      .map((reason) => typeof reason === 'string'
        ? knownLabel(t, CASCADE_REASON_LABEL_KEYS, reason)
        : formatMemoryStatusFact(reason))
      .join(', ');
  }
  if (group === 'recorder' && name === 'state' && typeof value === 'string') {
    return knownLabel(t, RECORDER_STATE_LABEL_KEYS, value);
  }
  if (group === 'recorder' && name === 'reason' && typeof value === 'string') {
    return knownLabel(t, RECORDER_REASON_LABEL_KEYS, value);
  }
  return formatMemoryStatusFact(value);
};
