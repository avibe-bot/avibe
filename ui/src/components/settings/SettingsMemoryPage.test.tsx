/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { SettingsMemoryPage } from './SettingsMemoryPage';
import type { MemoryStatus } from '../../context/ApiContext';

const api = vi.hoisted(() => ({
  abortMemoryClear: vi.fn(),
  clearMemory: vi.fn(),
  getMemoryFailures: vi.fn(),
  getMemoryMaintenance: vi.fn(),
  getMemorySettings: vi.fn(),
  getMemoryStatus: vi.fn(),
  listDependencies: vi.fn(),
  restartMemoryRuntime: vi.fn(),
  resumeMemoryClear: vi.fn(),
}));
const logMounts = vi.hoisted(() => ({ count: 0 }));
const showToast = vi.hoisted(() => vi.fn());
const translate = vi.hoisted(() => (key: string, options?: { returnObjects?: boolean }) =>
  options?.returnObjects ? [] : key,
);

vi.mock('../../context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../../context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('../../context/ToastContext', () => ({
  useToast: () => ({ showToast }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: translate }),
}));

vi.mock('./SettingsPageShell', () => ({
  SettingsPageShell: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock('../ui/confirm-dialog', () => ({
  ConfirmDialog: ({ open, onConfirm }: { open: boolean; onConfirm: () => void | Promise<void> }) =>
    open ? <button type="button" onClick={() => void onConfirm()}>confirm-clear</button> : null,
}));

vi.mock('./memory/MemoryLogPanel', async () => {
  const React = await import('react');
  return {
    MemoryLogPanel: ({ refreshToken = 0 }: { refreshToken?: number }) => {
      const [mount] = React.useState(() => ++logMounts.count);
      return <div data-testid="processing-log">processing-log-{mount}-refresh-{refreshToken}</div>;
    },
  };
});

vi.mock('./memory/MemoryProfilePanel', () => ({ MemoryProfilePanel: () => null }));
vi.mock('./memory/MemorySearchPanel', () => ({ MemorySearchPanel: () => null }));
vi.mock('./memory/MemorySettingsPanel', () => ({
  MemorySettingsPanel: ({ onClearAll }: { onClearAll: () => void }) => (
    <button type="button" onClick={onClearAll}>open-clear</button>
  ),
}));

const endpoint = {
  base_url: 'https://provider.example.test/v1',
  model: 'model-1',
  api_key: null,
  has_api_key: false,
};

const readyStatus = (): MemoryStatus => ({
  status: 'ok',
  source: { status: 'available', observed_at: '2026-08-08T12:00:00Z', reason: null },
  health: {
    status: 'ok',
    version: '1.2.3',
    capabilities: {},
    disabled_features: [],
    cascade: {},
    recorder: {},
  },
});

const renderPage = () => render(
  <MemoryRouter>
    <SettingsMemoryPage />
  </MemoryRouter>,
);

beforeEach(() => {
  logMounts.count = 0;
  api.getMemorySettings.mockResolvedValue({
    status: 'ok',
    enabled: true,
    processing: { llm: endpoint, embedding: endpoint },
  });
  api.getMemoryStatus.mockResolvedValue(readyStatus());
  api.getMemoryFailures.mockResolvedValue({ status: 'ok', items: [], recovery: null });
  api.getMemoryMaintenance.mockResolvedValue({ status: 'ok', data_exists: false, clear_recovery: null });
  api.listDependencies.mockResolvedValue({ ok: true, deps: [] });
  api.restartMemoryRuntime.mockResolvedValue({ ok: true, state: 'ready' });
  api.clearMemory.mockResolvedValue({ status: 'completed', operation_id: 'clear-ok', epoch: 1 });
  api.resumeMemoryClear.mockResolvedValue({ status: 'completed', operation_id: 'clear-42' });
  api.abortMemoryClear.mockResolvedValue({ status: 'aborted', operation_id: 'clear-42' });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe('SettingsMemoryPage Processing Record', () => {
  it('observes provider health before loading recorder anomalies', async () => {
    let resolveStatus: ((status: MemoryStatus) => void) | undefined;
    api.getMemoryStatus.mockReturnValue(
      new Promise<MemoryStatus>((resolve) => {
        resolveStatus = resolve;
      }),
    );

    renderPage();

    await waitFor(() => expect(api.getMemoryStatus).toHaveBeenCalledTimes(1));
    expect(api.getMemoryFailures).not.toHaveBeenCalled();

    resolveStatus?.(readyStatus());
    await waitFor(() => expect(api.getMemoryFailures).toHaveBeenCalledTimes(1));
  });

  it('merges status and log into one tab and performs no interval polling', async () => {
    const setInterval = vi.spyOn(window, 'setInterval');
    renderPage();

    expect(await screen.findByRole('radio', { name: 'memory.tabs.processingRecord' })).toBeTruthy();
    expect(await screen.findByTestId('processing-log')).toBeTruthy();
    expect(screen.queryByRole('radio', { name: 'memory.tabs.status' })).toBeNull();
    expect(screen.queryByRole('radio', { name: 'memory.tabs.log' })).toBeNull();
    await waitFor(() => expect(api.getMemoryStatus).toHaveBeenCalledTimes(1));
    expect(api.getMemoryFailures).toHaveBeenCalledTimes(1);
    expect(api.getMemoryMaintenance).toHaveBeenCalledTimes(1);
    expect(setInterval.mock.calls.some(([, delay]) => delay === 4000)).toBe(false);
  });

  it('refreshes health, anomalies, maintenance, and the timeline independently', async () => {
    const user = userEvent.setup();
    renderPage();
    const refresh = await screen.findByRole('button', { name: 'memory.processingRecord.refresh' });
    await waitFor(() => expect((refresh as HTMLButtonElement).disabled).toBe(false));

    await user.click(refresh);

    await waitFor(() => expect(api.getMemoryStatus).toHaveBeenCalledTimes(2));
    expect(api.getMemoryFailures).toHaveBeenCalledTimes(2);
    expect(api.getMemoryMaintenance).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('processing-log').textContent).toContain('refresh-1');
  });

  it('keeps retained Processing Record evidence available while Memory is disabled', async () => {
    api.getMemorySettings.mockResolvedValue({
      status: 'ok',
      enabled: false,
      processing: { llm: endpoint, embedding: endpoint },
    });
    renderPage();

    expect(await screen.findByRole('radio', { name: 'memory.tabs.processingRecord' })).toBeTruthy();
    expect(await screen.findByTestId('processing-log')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'memory.status.restartEngine' })).toBeNull();
  });

  it('keeps manual_required evidence read-only', async () => {
    api.getMemoryFailures.mockResolvedValue({
      status: 'ok',
      recovery: null,
      items: [{
        kind: 'attachment_release',
        state: 'manual_required',
        operation: 'flush',
        occurred_at: '2026-08-08T12:01:00Z',
        error_code: 'attachment_release_unknown',
        attempts: 3,
        generation: 2,
        request_id: 'request-2',
      }],
    });
    renderPage();

    expect(await screen.findByText('memory.processingRecord.manualRequiredReadOnly')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'memory.processingRecord.clearRecovery.resume' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'memory.processingRecord.clearRecovery.abort' })).toBeNull();
  });

  it('keeps clear recovery available from maintenance when the anomaly source fails', async () => {
    api.getMemoryFailures.mockResolvedValue({ status: 'failed', error: 'memory_store_unavailable' });
    api.getMemoryMaintenance.mockResolvedValue({
      status: 'ok',
      data_exists: true,
      clear_recovery: {
        operation_id: 'clear-maintenance',
        state: 'recovery_required',
        can_resume: true,
        can_abort: true,
      },
    });
    renderPage();

    expect(await screen.findByText('clear-maintenance')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'memory.processingRecord.clearRecovery.resume' })).toBeTruthy();
    expect(screen.getByText('errors.memory_store_unavailable')).toBeTruthy();
  });

  it.each([
    ['resume', 'resumeMemoryClear', 'completed'],
    ['abort', 'abortMemoryClear', 'aborted'],
  ] as const)('runs the clear recovery %s command with its operation ID', async (action, method) => {
    api.getMemoryFailures.mockResolvedValue({
      status: 'ok',
      items: [],
      recovery: {
        operation_id: 'clear-42',
        state: 'recovery_required',
        can_resume: true,
        can_abort: true,
      },
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('button', {
      name: `memory.processingRecord.clearRecovery.${action}`,
    }));

    await waitFor(() => expect(api[method]).toHaveBeenCalledWith('clear-42'));
    expect(showToast).toHaveBeenCalledWith(
      `memory.processingRecord.clearRecovery.${action}Success`,
      'success',
    );
  });

  it.each([
    ['resume', 'resumeMemoryClear', 'non-success', {
      status: 'ok',
      data_exists: true,
      clear_recovery: {
        operation_id: 'clear-refreshed',
        state: 'recovery_needed',
        can_resume: true,
        can_abort: false,
      },
    }],
    ['abort', 'abortMemoryClear', 'rejection', {
      status: 'ok',
      data_exists: false,
      clear_recovery: null,
    }],
  ] as const)(
    'reloads recovery state before clearing the %s action after a %s',
    async (action, method, outcome, refreshedMaintenance) => {
      api.getMemoryFailures.mockResolvedValue({
        status: 'ok',
        items: [],
        recovery: {
          operation_id: 'clear-stale',
          state: 'recovery_needed',
          can_resume: true,
          can_abort: true,
        },
      });
      const user = userEvent.setup();
      renderPage();
      const actionButton = await screen.findByRole('button', {
        name: `memory.processingRecord.clearRecovery.${action}`,
      });
      await waitFor(() => expect(api.getMemoryMaintenance).toHaveBeenCalledTimes(1));

      let finishFailures: ((value: { status: 'ok'; items: []; recovery: null }) => void) | undefined;
      let finishMaintenance: ((value: typeof refreshedMaintenance) => void) | undefined;
      api.getMemoryFailures.mockReturnValueOnce(new Promise((resolve) => { finishFailures = resolve; }));
      api.getMemoryMaintenance.mockReturnValueOnce(new Promise((resolve) => { finishMaintenance = resolve; }));
      if (outcome === 'non-success') {
        api[method].mockResolvedValueOnce({ status: 'failed', error: 'memory_clear_failed' });
      } else {
        api[method].mockRejectedValueOnce(new Error('transport failed'));
      }

      await user.click(actionButton);

      await waitFor(() => expect(api.getMemoryStatus).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(api.getMemoryMaintenance).toHaveBeenCalledTimes(2));
      expect(api.getMemoryFailures).toHaveBeenCalledTimes(2);
      expect((actionButton as HTMLButtonElement).disabled).toBe(true);
      finishFailures?.({ status: 'ok', items: [], recovery: null });
      finishMaintenance?.(refreshedMaintenance);

      if (outcome === 'non-success') {
        expect(await screen.findByText('clear-refreshed')).toBeTruthy();
        expect((screen.getByRole('button', {
          name: 'memory.processingRecord.clearRecovery.abort',
        }) as HTMLButtonElement).disabled).toBe(true);
      } else {
        await waitFor(() => expect(screen.queryByText('clear-stale')).toBeNull());
        expect(screen.queryByRole('button', {
          name: 'memory.processingRecord.clearRecovery.abort',
        })).toBeNull();
      }

      const reloadStatusOrder = api.getMemoryStatus.mock.invocationCallOrder[1];
      const reloadFailuresOrder = api.getMemoryFailures.mock.invocationCallOrder[1];
      expect(reloadStatusOrder).toBeLessThan(reloadFailuresOrder);
    },
  );

  it('refreshes and exposes recovery immediately after a failed clear', async () => {
    api.clearMemory.mockResolvedValue({
      status: 'failed',
      error: 'memory_clear_failed',
      recovery: {
        operation_id: 'clear-interrupted',
        state: 'recovery_needed',
        can_resume: true,
        can_abort: false,
      },
    });
    api.getMemoryFailures
      .mockResolvedValueOnce({ status: 'ok', items: [], recovery: null })
      .mockResolvedValueOnce({
        status: 'ok',
        items: [],
        recovery: {
          operation_id: 'clear-interrupted',
          state: 'recovery_needed',
          can_resume: true,
          can_abort: false,
        },
      });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('radio', { name: 'memory.tabs.settings' }));
    await user.click(await screen.findByRole('button', { name: 'open-clear' }));
    await user.click(await screen.findByRole('button', { name: 'confirm-clear' }));

    await waitFor(() => expect(api.getMemoryFailures).toHaveBeenCalledTimes(2));
    expect(api.getMemoryMaintenance).toHaveBeenCalledTimes(2);
    await user.click(screen.getByRole('radio', { name: 'memory.tabs.processingRecord' }));
    expect(await screen.findByText('clear-interrupted')).toBeTruthy();
  });
});

describe('SettingsMemoryPage restart action', () => {
  it('stays available when runtime health cannot be loaded', async () => {
    api.getMemoryStatus.mockResolvedValue({ status: 'failed', error: 'memory_status_failed' });
    renderPage();

    expect(await screen.findByRole('button', { name: 'memory.status.restartEngine' })).toBeTruthy();
  });

  it('is disabled and shows progress while the request is pending', async () => {
    let finishRestart: ((value: { ok: true; state: string }) => void) | undefined;
    api.restartMemoryRuntime.mockReturnValue(new Promise((resolve) => { finishRestart = resolve; }));
    const user = userEvent.setup();
    renderPage();
    const action = await screen.findByRole('button', { name: 'memory.status.restartEngine' });

    await user.click(action);
    expect((action as HTMLButtonElement).disabled).toBe(true);
    expect(action.querySelector('.animate-spin')).toBeTruthy();

    finishRestart?.({ ok: true, state: 'ready' });
    await waitFor(() => expect((action as HTMLButtonElement).disabled).toBe(false));
  });
});
