/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { MemoryFailureLogEntry, MemoryStatus } from '../../../context/ApiContext';
import { MemoryStatusPanel } from './MemoryStatusPanel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
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
    expect(screen.getByText('unhealthy')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.sourceState.stale')).toBeTruthy();
    expect(screen.getByText('memory.processingRecord.sourceState.unavailable')).toBeTruthy();
    expect(screen.queryByText('memory.status.state.degraded')).toBeNull();
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
        recovery={{ operation_id: 'clear-42', state: 'recovery_required', can_abort: true }}
        onResumeClear={onResumeClear}
        onAbortClear={onAbortClear}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'memory.processingRecord.clearRecovery.resume' }));
    await user.click(screen.getByRole('button', { name: 'memory.processingRecord.clearRecovery.abort' }));

    expect(onResumeClear).toHaveBeenCalledWith('clear-42');
    expect(onAbortClear).toHaveBeenCalledWith('clear-42');
  });

  it('keeps abort unavailable until the journal verifies a complete snapshot', async () => {
    const onAbortClear = vi.fn();
    const user = userEvent.setup();
    render(
      <MemoryStatusPanel
        {...baseProps}
        recovery={{ operation_id: 'clear-incomplete', state: 'recovery_needed', can_abort: false }}
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
});
