/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { MemoryClearRecovery, MemoryFailureLogEntry, MemoryStatus } from '../../../context/ApiContext';
import { MemoryStatusPanel } from './MemoryStatusPanel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => (
      key === 'memory.processingRecord.sourceReason'
        ? `${key}:${String(options?.reason)}`
        : key
    ),
  }),
}));

const STATUS: MemoryStatus = {
  status: 'ok',
  source: {
    status: 'available',
    observed_at: '2026-08-08T12:00:00Z',
    reason: null,
  },
  health: {
    status: 'ok',
    version: '1.2.3',
    capabilities: { keyword_search: true, agentic_search: false },
    disabled_features: ['agentic_search'],
    cascade: { status: 'unhealthy' },
    recorder: { status: 'active' },
  },
};

const MANUAL_FAILURE: MemoryFailureLogEntry = {
  kind: 'result_unknown',
  state: 'manual_required',
  operation: 'flush',
  occurred_at: '2026-08-08T12:01:00Z',
  error_code: 'attachment_release_unknown',
  attempts: 3,
  generation: 7,
  request_id: 'request-7',
};

const baseProps: React.ComponentProps<typeof MemoryStatusPanel> = {
  status: STATUS,
  failures: [],
  recovery: null,
  logSections: {
    everos: { status: 'available', observed_at: '2026-08-08T12:00:00Z' },
    capture: { status: 'stale', observed_at: '2026-08-08T11:00:00Z', reason: 'locked' },
    calls: { status: 'unavailable', observed_at: null, reason: 'malformed' },
  },
  statusLoading: false,
  failuresLoading: false,
  statusError: null,
  failuresError: null,
  refreshPending: false,
  recoveryAction: null,
  onRefresh: vi.fn(),
  onResumeClear: vi.fn(),
  onAbortClear: vi.fn(),
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('MemoryStatusPanel', () => {
  it('renders runtime facts and every source without synthesizing a global state', () => {
    render(<MemoryStatusPanel {...baseProps} />);

    expect(screen.getByText('1.2.3')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.runtime.healthStatus.ok')).toBeTruthy();
    expect(screen.getByText('unhealthy')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.sourceState.stale')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.sourceState.unavailable')).toBeTruthy();
    expect(screen.queryByText('memory.status.state.degraded')).toBeNull();
  });

  it('preserves a future runtime health status as diagnostic fallback text', () => {
    render(
      <MemoryStatusPanel
        {...baseProps}
        status={{
          ...STATUS,
          health: { ...STATUS.health!, status: 'future_health_state' },
        }}
      />,
    );

    expect(screen.getByText('future_health_state')).toBeTruthy();
  });

  it('localizes closed runtime fact labels and enum values while preserving diagnostics', () => {
    render(
      <MemoryStatusPanel
        {...baseProps}
        status={{
          ...STATUS,
          health: {
            ...STATUS.health!,
            capabilities: { embed: false },
            disabled_features: ['embed'],
            cascade: {
              healthy: false,
              reasons: ['drain_failures'],
              prune_stale_seconds: 45,
            },
            recorder: { state: 'degraded', reason: 'call_log_corrupt' },
          },
        }}
      />,
    );

    expect(screen.getAllByText('memory.processingRecord.runtime.fact.capability.embed')).toHaveLength(2);
    expect(screen.getAllByText('memory.processingRecord.runtime.fact.boolean.false')).toHaveLength(2);
    expect(screen.getByText('memory.processingRecord.runtime.fact.cascade.pruneStaleSeconds')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.runtime.fact.cascadeReason.drainFailures')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.runtime.fact.recorder.state')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.runtime.fact.recorderState.degraded')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.runtime.fact.recorderReason.callLogCorrupt')).toBeTruthy();
    expect(screen.getByText('45')).toBeTruthy();
  });

  it('leaves future runtime fact labels and values as raw fallback text', () => {
    render(
      <MemoryStatusPanel
        {...baseProps}
        status={{
          ...STATUS,
          health: {
            ...STATUS.health!,
            capabilities: { future_capability: true },
            disabled_features: ['future_feature'],
            cascade: { reasons: ['future_reason'], future_counter: 12 },
            recorder: { state: 'future_state', future_field: 'future_value' },
          },
        }}
      />,
    );

    expect(screen.getByText('future_capability')).toBeTruthy();
    expect(screen.getByText('true')).toBeTruthy();
    expect(screen.getByText('future_feature')).toBeTruthy();
    expect(screen.getByText('future_reason')).toBeTruthy();
    expect(screen.getByText('future_counter')).toBeTruthy();
    expect(screen.getByText('12')).toBeTruthy();
    expect(screen.getByText('future_state')).toBeTruthy();
    expect(screen.getByText('future_field')).toBeTruthy();
    expect(screen.getByText('future_value')).toBeTruthy();
  });

  it('localizes known runtime and log source reasons', () => {
    render(
      <MemoryStatusPanel
        {...baseProps}
        status={{
          ...STATUS,
          source: { status: 'unavailable', observed_at: null, reason: 'memory_sidecar_unavailable' },
        }}
        logSections={{
          ...baseProps.logSections!,
          everos: { status: 'unavailable', observed_at: null, reason: 'missing' },
          capture: { status: 'stale', observed_at: null, reason: 'runs_busy' },
        }}
      />,
    );

    expect(screen.getByText('memory.processingRecord.sourceReason:errors.memory_sidecar_unavailable')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.sourceReason:memory.log.reason.missing')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.sourceReason:memory.log.reason.runsBusy')).toBeTruthy();
  });

  it('leaves a future source reason as inert fallback text', () => {
    render(
      <MemoryStatusPanel
        {...baseProps}
        status={{
          ...STATUS,
          source: { status: 'unavailable', observed_at: null, reason: 'future_source_reason' },
        }}
      />,
    );

    expect(screen.getByText('memory.processingRecord.sourceReason:future_source_reason')).toBeTruthy();
  });

  it('keeps source-independent anomalies visible when health cannot be read', () => {
    render(
      <MemoryStatusPanel
        {...baseProps}
        status={null}
        statusError="health failed"
        failures={[MANUAL_FAILURE]}
      />,
    );

    expect(screen.getByText('health failed')).toBeTruthy();
    expect(screen.getByText('memory.status.failureLog.kind.result_unknown')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.manualRequiredReadOnly')).toBeTruthy();
  });

  it('renders manual_required as read-only without recovery commands', () => {
    render(<MemoryStatusPanel {...baseProps} failures={[MANUAL_FAILURE]} />);

    expect(screen.getByText('memory.processingRecord.anomalyState.manualRequired')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.anomalyOperation.flush')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'memory.processingRecord.clearRecovery.resume' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'memory.processingRecord.clearRecovery.abort' })).toBeNull();
  });

  it('leaves future anomaly enum values as inert fallback text', () => {
    render(
      <MemoryStatusPanel
        {...baseProps}
        failures={[{
          ...MANUAL_FAILURE,
          kind: 'future_kind',
          state: 'future_state',
          operation: 'future_operation',
        }]}
      />,
    );

    expect(screen.getByText('future_kind')).toBeTruthy();
    expect(screen.getByText('future_state')).toBeTruthy();
    expect(screen.getByText('future_operation')).toBeTruthy();
  });

  it('offers distinct resume and abort commands for clear recovery', async () => {
    const onResumeClear = vi.fn();
    const onAbortClear = vi.fn();
    const user = userEvent.setup();
    render(
      <MemoryStatusPanel
        {...baseProps}
        recovery={{ operation_id: 'clear-42', state: 'recovery_needed', can_resume: true, can_abort: true }}
        onResumeClear={onResumeClear}
        onAbortClear={onAbortClear}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'memory.processingRecord.clearRecovery.resume' }));
    await user.click(screen.getByRole('button', { name: 'memory.processingRecord.clearRecovery.abort' }));

    expect(onResumeClear).toHaveBeenCalledWith('clear-42');
    expect(onAbortClear).toHaveBeenCalledWith('clear-42');
  });

  it.each([
    ['preparing', 'memory.processingRecord.clearRecovery.state.preparing'],
    ['prepared', 'memory.processingRecord.clearRecovery.state.prepared'],
    ['deleting', 'memory.processingRecord.clearRecovery.state.deleting'],
    ['recovery_needed', 'memory.processingRecord.clearRecovery.state.recoveryNeeded'],
  ])('localizes the known Clear recovery state %s', (state, label) => {
    render(
      <MemoryStatusPanel
        {...baseProps}
        recovery={{ operation_id: `clear-${state}`, state, can_resume: true, can_abort: false }}
      />,
    );

    expect(screen.getByText(label)).toBeTruthy();
    expect(screen.queryByText(state)).toBeNull();
  });

  it('leaves a future Clear recovery state as inert fallback text', () => {
    render(
      <MemoryStatusPanel
        {...baseProps}
        recovery={{ operation_id: 'clear-future', state: 'future_state', can_resume: false, can_abort: false }}
      />,
    );

    expect(screen.getByText('future_state')).toBeTruthy();
  });

  it('keeps abort unavailable until the journal verifies a complete snapshot', async () => {
    const onAbortClear = vi.fn();
    const user = userEvent.setup();
    render(
      <MemoryStatusPanel
        {...baseProps}
        recovery={{ operation_id: 'clear-incomplete', state: 'recovery_needed', can_resume: true, can_abort: false }}
        onAbortClear={onAbortClear}
      />,
    );

    const resume = screen.getByRole('button', { name: 'memory.processingRecord.clearRecovery.resume' });
    const abort = screen.getByRole('button', { name: 'memory.processingRecord.clearRecovery.abort' });
    expect((resume as HTMLButtonElement).disabled).toBe(false);
    expect((abort as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('memory.processingRecord.clearRecovery.abortUnavailable')).toBeTruthy();

    await user.click(abort);
    expect(onAbortClear).not.toHaveBeenCalled();
  });

  it('keeps resume unavailable after the journal commits to abort', async () => {
    const onResumeClear = vi.fn();
    const user = userEvent.setup();
    const recovery = {
      operation_id: 'clear-aborting',
      state: 'recovery_needed',
      can_resume: false,
      can_abort: true,
    } satisfies MemoryClearRecovery;
    render(
      <MemoryStatusPanel
        {...baseProps}
        recovery={recovery}
        onResumeClear={onResumeClear}
      />,
    );

    const resume = screen.getByRole('button', { name: 'memory.processingRecord.clearRecovery.resume' });
    expect((resume as HTMLButtonElement).disabled).toBe(true);

    await user.click(resume);
    expect(onResumeClear).not.toHaveBeenCalled();
  });
});
