/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { MemoryStatus } from '../../../context/ApiContext';
import { MemoryStatusPanel } from './MemoryStatusPanel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('../../ui/confirm-dialog', () => ({
  ConfirmDialog: ({ open, onConfirm }: { open: boolean; onConfirm: () => void }) => (
    open ? <button type="button" onClick={onConfirm}>confirm-loss</button> : null
  ),
}));

const status = (state: MemoryStatus['state'], reason: string | null = null): MemoryStatus => ({
  status: 'ok',
  state,
  reason,
  source: { status: 'available', observed_at: '2026-08-24T00:00:00Z', reason: null },
  health: {
    status: 'ok',
    version: '1.2.3',
    capabilities: {},
    disabled_features: [],
  },
});

const props = (runtime: MemoryStatus): React.ComponentProps<typeof MemoryStatusPanel> => ({
  status: runtime,
  failures: [],
  logSections: null,
  statusLoading: false,
  failuresLoading: false,
  statusError: null,
  failuresError: null,
  refreshPending: false,
  onRefresh: vi.fn(),
});

afterEach(cleanup);

describe('MemoryStatusPanel', () => {
  it.each(['disabled', 'starting', 'running', 'degraded', 'needs_repair'] as const)(
    'renders the coherent %s runtime state',
    (state) => {
      render(<MemoryStatusPanel {...props(status(state))} />);
      expect(screen.getByText(`memory.runtimeState.${state}`)).toBeTruthy();
    },
  );

  it.each([256, undefined])('shows affected counts and submission state with healthy runtime (%s)', (count) => {
    render(<MemoryStatusPanel {...props(status('running'))}
      failuresNotice="memory.processingRecord.reason.memory_failure_history_unavailable"
      failures={[{
        id: 'drop', kind: 'delivery_abandoned', state: 'not_submitted', operation: 'add',
        occurred_at: '2026-09-17T00:00:00Z', error_code: null, attempts: 0,
        generation: 0, request_id: null, affected_count: count,
      }, {
        id: 'unknown', kind: 'result_unknown', state: 'unknown', operation: 'flush',
        occurred_at: '2026-09-17T00:00:00Z', error_code: null, attempts: 3,
        generation: 0, request_id: null,
      }]} />);
    const dropped = within(screen.getByTestId('memory-anomaly-delivery_abandoned'));
    expect(dropped.getByText('memory.processingRecord.anomalyState.notSubmitted')).toBeTruthy();
    expect(dropped.getByText('memory.processingRecord.field.affectedCount').parentElement?.textContent)
      .toContain(String(count ?? 1));
    const unknown = within(screen.getByTestId('memory-anomaly-result_unknown'));
    expect(unknown.getByText('memory.processingRecord.anomalyState.unknown')).toBeTruthy();
    expect(unknown.getByText('memory.processingRecord.field.attempts').parentElement?.textContent).toContain('3');
    expect(screen.getByText('memory.processingRecord.reason.memory_failure_history_unavailable')).toBeTruthy();
  });

  it('offers destructive Repair only when the caller marks it supported', () => {
    const onRepair = vi.fn();
    render(
      <MemoryStatusPanel
        {...props(status('needs_repair', 'memory_local_data_unusable'))}
        repairSupported
        onRepair={onRepair}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'memory.repair.button' }));
    fireEvent.click(screen.getByRole('button', { name: 'confirm-loss' }));
    expect(onRepair).toHaveBeenCalledOnce();
  });

  it('does not expose Restart, Rebuild, or live index Repair controls', () => {
    render(<MemoryStatusPanel {...props(status('degraded', 'memory_provider_timeout'))} />);

    expect(screen.queryByText(/restart|rebuild|repair index/i)).toBeNull();
    expect(screen.queryByRole('button', { name: 'memory.repair.button' })).toBeNull();
  });

  it('renders an intentionally unavailable source as a neutral notice', () => {
    render(
      <MemoryStatusPanel
        {...props(status('running'))}
        failuresNotice="memory.processingRecord.reason.memory_failure_history_unavailable"
      />,
    );

    const notice = screen.getByRole('status');
    expect(notice.textContent).toBe('memory.processingRecord.reason.memory_failure_history_unavailable');
    expect(notice.className).toContain('text-muted');
  });
});
