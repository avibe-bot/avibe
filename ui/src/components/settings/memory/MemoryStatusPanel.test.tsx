/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

import type { MemoryClearInProgress, MemoryStatus } from '../../../context/ApiContext';
import { MemoryStatusPanel } from './MemoryStatusPanel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

const status: MemoryStatus = {
  status: 'ok',
  source: { status: 'available', observed_at: '2026-08-13T00:00:00Z', reason: null },
  health: {
    status: 'ok',
    version: '1.2.3',
    capabilities: {},
    disabled_features: [],
    cascade: null,
  },
};

const baseProps: React.ComponentProps<typeof MemoryStatusPanel> = {
  status,
  failures: [],
  clearInProgress: null,
  logSections: null,
  providerChecks: [],
  providerChecksSource: null,
  statusLoading: false,
  failuresLoading: false,
  statusError: null,
  failuresError: null,
  refreshPending: false,
  onRefresh: vi.fn(),
};

afterEach(() => cleanup());

describe('MemoryStatusPanel', () => {
  it('renders runtime facts without recovery actions', () => {
    render(<MemoryStatusPanel {...baseProps} />);
    expect(screen.getByText('1.2.3')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /resume|abort/i })).toBeNull();
  });

  it('renders a failed clear projection as read-only state', () => {
    const clearInProgress: MemoryClearInProgress = {
      state: 'failed',
      operation_id: 'op-1',
      occurred_at: '2026-08-13T00:00:00Z',
      error_code: 'memory_clear_failed',
    };
    render(<MemoryStatusPanel {...baseProps} clearInProgress={clearInProgress} />);
    expect(screen.getByText('op-1')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /resume|abort/i })).toBeNull();
  });
});
