import type { TFunction } from 'i18next';

import type { MemoryProcessingSourceStatus, MemoryStatus } from '../../../context/ApiContext';
import { memoryErrorMessage } from '../../../lib/memoryRead';

export type RuntimeFactGroup = keyof typeof RUNTIME_FACT_LABEL_KEYS;
export type SourceState = MemoryStatus['source']['status'] | MemoryProcessingSourceStatus['status'];
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
  },
} as const;


const HEALTH_STATUS_LABEL_KEYS = {
  ok: 'memory.processingRecord.runtime.healthStatus.ok',
} as const;

const MEMORY_SOURCE_ERROR_REASONS = new Set([
  'memory_disabled',
  'memory_runtime_busy',
  'memory_runtime_missing',
  'memory_runtime_unsupported',
  'memory_runtime_install_failed',
  'memory_sidecar_unavailable',
  'memory_provider_timeout',
  'memory_provider_response_invalid',
  'memory_capability_unavailable',
  'memory_processing_failed',
  'memory_wake_failed',
  'memory_local_data_unusable',
  'memory_legacy_recovery_required',
  'memory_permission_denied',
  'memory_disk_unavailable',
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
} as const;

const PROCESSING_REASON_LABEL_KEYS = {
  busy: 'memory.processingRecord.reason.busy',
  memory_failure_history_unavailable: 'memory.processingRecord.reason.memory_failure_history_unavailable',
  native_memcells_unavailable: 'memory.processingRecord.reason.native_memcells_unavailable',
  native_runs_unavailable: 'memory.processingRecord.reason.native_runs_unavailable',
  native_semantic_unavailable: 'memory.processingRecord.reason.native_semantic_unavailable',
  payload_projection_limit: 'memory.processingRecord.reason.payload_projection_limit',
  payload_unavailable: 'memory.processingRecord.reason.payload_unavailable',
  payload_malformed: 'memory.processingRecord.reason.payload_malformed',
  authorized_user_payload_unavailable: 'memory.processingRecord.reason.authorized_user_payload_unavailable',
  unauthorized_or_bounded_items_omitted: 'memory.processingRecord.reason.unauthorized_or_bounded_items_omitted',
  native_runs_missing_or_retained: 'memory.processingRecord.reason.native_runs_missing_or_retained',
  native_run_retention_bounded: 'memory.processingRecord.reason.native_run_retention_bounded',
  semantic_results_not_user_scoped: 'memory.processingRecord.reason.semantic_results_not_user_scoped',
  semantic_results_missing_or_retained: 'memory.processingRecord.reason.semantic_results_missing_or_retained',
  semantic_projection_bounded: 'memory.processingRecord.reason.semantic_projection_bounded',
  current_state_not_user_scoped: 'memory.processingRecord.reason.current_state_not_user_scoped',
  current_state_unavailable: 'memory.processingRecord.reason.current_state_unavailable',
  index_state_unavailable: 'memory.processingRecord.reason.index_state_unavailable',
  index_state_missing_or_retained: 'memory.processingRecord.reason.index_state_missing_or_retained',
  index_state_incomplete: 'memory.processingRecord.reason.index_state_incomplete',
  unknown: 'memory.processingRecord.reason.unknown',
} as const;

const knownLabel = (t: TFunction, keys: Record<string, string>, value: string): string => {
  const key = Object.prototype.hasOwnProperty.call(keys, value) ? keys[value] : undefined;
  return key ? t(key) : value;
};

export const isMemoryProcessingRecordReason = (
  value: string | null | undefined,
): value is keyof typeof PROCESSING_REASON_LABEL_KEYS => (
  typeof value === 'string'
  && Object.prototype.hasOwnProperty.call(PROCESSING_REASON_LABEL_KEYS, value)
);

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
    : knownLabel(t, PROCESSING_REASON_LABEL_KEYS, value)
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
  return formatMemoryStatusFact(value);
};
