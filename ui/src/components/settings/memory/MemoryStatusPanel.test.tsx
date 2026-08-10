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
        : key === 'errors.future_clear_error'
          ? String(options?.defaultValue)
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
  id: 'ma_1111111111111111111111111111111111111111111111111111111111111111',
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

  it('preserves the Processing Record structure and responsive class contract', () => {
    const { container } = render(
      <MemoryStatusPanel
        {...baseProps}
        failures={[MANUAL_FAILURE]}
        recovery={{ operation_id: 'clear-dom', state: 'prepared', can_resume: true, can_abort: true }}
      />,
    );

    expect(container.firstElementChild?.className).toBe('flex flex-col gap-5');
    expect(Array.from(container.querySelectorAll('section')).map((section) => section.getAttribute('aria-labelledby'))).toEqual([
      'memory-runtime-title',
      'memory-sources-title',
      'memory-anomalies-title',
    ]);
    expect(container.querySelector('#memory-sources-title')?.parentElement?.nextElementSibling?.className).toBe(
      'grid gap-2 sm:grid-cols-2 xl:grid-cols-4',
    );
    expect(screen.getByTestId('memory-anomaly-result_unknown').className).toBe(
      'flex min-w-0 flex-col gap-3 border-b border-border py-3 last:border-b-0 lg:flex-row lg:justify-between',
    );
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

  it('renders concurrent duplicate-shaped anomalies under distinct backend IDs', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const duplicateShape = {
      ...MANUAL_FAILURE,
      request_id: null,
      occurred_at: '2026-08-08T12:01:00.000Z',
    };

    render(
      <MemoryStatusPanel
        {...baseProps}
        failures={[
          duplicateShape,
          {
            ...duplicateShape,
            id: 'ma_2222222222222222222222222222222222222222222222222222222222222222',
          },
        ]}
      />,
    );

    expect(screen.getAllByTestId('memory-anomaly-result_unknown')).toHaveLength(2);
    expect(consoleError.mock.calls.flat().join(' ')).not.toContain(
      'Encountered two children with the same key',
    );
    consoleError.mockRestore();
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
    ['memory_clear_failed', 'errors.memory_clear_failed'],
    ['future_clear_error', 'future_clear_error'],
  ])('renders the Clear recovery error %s safely', (errorCode, expectedLabel) => {
    render(
      <MemoryStatusPanel
        {...baseProps}
        recovery={{
          operation_id: `clear-${errorCode}`,
          state: 'recovery_needed',
          can_resume: true,
          can_abort: false,
          error_code: errorCode,
        }}
      />,
    );

    expect(screen.getByText(expectedLabel)).toBeTruthy();
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
