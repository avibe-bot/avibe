/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { SettingsMemoryPage } from './SettingsMemoryPage';
import type { MemoryProcessingRecordSummary } from '../../context/ApiContext';

const api = vi.hoisted(() => ({
  abortMemoryClear: vi.fn(),
  clearMemory: vi.fn(),
  factoryResetMemory: vi.fn(),
  getMemoryMaintenance: vi.fn(),
  getMemoryProcessingRecord: vi.fn(),
  getMemorySettings: vi.fn(),
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
  ConfirmDialog: ({ open, onConfirm, title }: { open: boolean; onConfirm: () => void | Promise<void>; title?: string }) =>
    open ? <button type="button" onClick={() => void onConfirm()}>{title?.includes('factoryReset') ? 'confirm-factory' : 'confirm-clear'}</button> : null,
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
  MemorySettingsPanel: ({
    maintenance,
    onClearAll,
    onFactoryReset,
    factoryResetBusy,
    factoryResetArtifactValid,
    factoryResetPending,
    onRebuildBusyChange,
  }: {
    maintenance: { can_clear: boolean } | null;
    onClearAll: () => void;
    onFactoryReset?: () => void;
    factoryResetBusy?: boolean;
    factoryResetArtifactValid?: boolean;
    factoryResetPending?: boolean;
    onRebuildBusyChange: (busy: boolean) => void;
  }) => (
    <div>
      <span>{maintenance?.can_clear ? 'maintenance-ready' : 'maintenance-unknown'}</span>
      <button type="button" onClick={onClearAll}>open-clear</button>
      <button type="button" onClick={onFactoryReset} disabled={factoryResetBusy || !factoryResetArtifactValid}>
        {factoryResetPending ? 'retry-factory' : 'open-factory'}
      </button>
      <button type="button" onClick={() => onRebuildBusyChange(true)}>
        begin-rebuild
      </button>
      <button type="button" onClick={() => onRebuildBusyChange(false)}>
        end-rebuild
      </button>
    </div>
  ),
}));

const endpoint = {
  base_url: 'https://provider.example.test/v1',
  model: 'model-1',
  api_key: null,
  has_api_key: false,
};

const source = { status: 'available' as const, observed_at: '2026-08-08T12:00:00Z', reason: null };

const readyProcessingRecord = (): MemoryProcessingRecordSummary => ({
  status: 'ok',
  runtime: {
    source,
    health: {
      status: 'ok',
      version: '1.2.3',
      capabilities: {},
      disabled_features: [],
      cascade: {},
      recorder: {},
    },
  },
  sources: { everos: source, capture: source, calls: source },
  anomalies: { source, items: [] },
  maintenance: {
    source,
    data_exists: false,
    can_clear: true,
    clear_recovery: null,
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
  api.getMemoryProcessingRecord.mockResolvedValue(readyProcessingRecord());
  api.getMemoryMaintenance.mockResolvedValue({
    status: 'ok',
    data_exists: false,
    can_clear: true,
    clear_recovery: null,
  });
  api.listDependencies.mockResolvedValue({ ok: true, deps: [] });
  api.restartMemoryRuntime.mockResolvedValue({ ok: true, state: 'ready' });
  api.clearMemory.mockResolvedValue({ status: 'completed', operation_id: 'clear-ok', epoch: 1 });
  api.factoryResetMemory.mockResolvedValue({
    ok: true,
    result: 'completed',
    data_deleted: true,
    roots: [
      { path: 'memory', existed: true, deleted: true },
      { path: 'state/memory', existed: true, deleted: true },
    ],
  });
  api.resumeMemoryClear.mockResolvedValue({ status: 'completed', operation_id: 'clear-42' });
  api.abortMemoryClear.mockResolvedValue({ status: 'aborted', operation_id: 'clear-42' });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe('SettingsMemoryPage Processing Record', () => {
  it('loads maintenance controls even while the composite summary is stalled', async () => {
    api.getMemoryProcessingRecord.mockReturnValue(new Promise(() => undefined));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('radio', { name: 'memory.tabs.settings' }));

    expect(await screen.findByText('maintenance-ready')).toBeTruthy();
    expect(api.getMemoryMaintenance).toHaveBeenCalledTimes(1);
  });

  it('loads one summary and keeps timeline pagination separate', async () => {
    const setInterval = vi.spyOn(window, 'setInterval');
    renderPage();

    expect(await screen.findByRole('radio', { name: 'memory.tabs.processingRecord' })).toBeTruthy();
    expect(await screen.findByTestId('processing-log')).toBeTruthy();
    await waitFor(() => expect(api.getMemoryProcessingRecord).toHaveBeenCalledTimes(1));
    expect(setInterval.mock.calls.some(([, delay]) => delay === 4000)).toBe(false);
  });

  it('refreshes the summary once and refreshes the timeline independently', async () => {
    const user = userEvent.setup();
    renderPage();
    const refresh = await screen.findByRole('button', { name: 'memory.processingRecord.refresh' });
    await waitFor(() => expect((refresh as HTMLButtonElement).disabled).toBe(false));

    await user.click(refresh);

    await waitFor(() => expect(api.getMemoryProcessingRecord).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('processing-log').textContent).toContain('refresh-1');
  });

  it('keeps retained Processing Record evidence available while Memory is disabled', async () => {
    api.getMemorySettings.mockResolvedValue({
      status: 'ok',
      enabled: false,
      processing: { llm: endpoint, embedding: endpoint },
    });
    renderPage();

    expect(await screen.findByTestId('processing-log')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'memory.status.restartEngine' })).toBeNull();
  });

  it('renders source-local anomaly failure without hiding maintenance recovery', async () => {
    const summary = readyProcessingRecord();
    summary.anomalies.source = {
      status: 'unavailable',
      observed_at: null,
      reason: 'memory_store_unavailable',
    };
    summary.maintenance.clear_recovery = {
      operation_id: 'clear-maintenance',
      state: 'recovery_needed',
      can_resume: true,
      can_abort: true,
    };
    api.getMemoryProcessingRecord.mockResolvedValue(summary);
    renderPage();

    expect(await screen.findByText('clear-maintenance')).toBeTruthy();
    expect(screen.getByText('errors.memory_store_unavailable')).toBeTruthy();
  });

  it('keeps manual_required evidence read-only', async () => {
    const summary = readyProcessingRecord();
    summary.anomalies.items = [{
      id: 'ma_4444444444444444444444444444444444444444444444444444444444444444',
      kind: 'attachment_release',
      state: 'manual_required',
      operation: 'flush',
      occurred_at: '2026-08-08T12:01:00Z',
      error_code: 'attachment_release_unknown',
      attempts: 3,
      generation: 2,
      request_id: 'request-2',
    }];
    api.getMemoryProcessingRecord.mockResolvedValue(summary);
    renderPage();

    expect(await screen.findByText('memory.processingRecord.manualRequiredReadOnly')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'memory.processingRecord.clearRecovery.resume' })).toBeNull();
  });

  it.each([
    ['resume', 'resumeMemoryClear', 'completed'],
    ['abort', 'abortMemoryClear', 'aborted'],
  ] as const)('runs %s and reloads the summary', async (action, method) => {
    const summary = readyProcessingRecord();
    summary.maintenance.clear_recovery = {
      operation_id: 'clear-42',
      state: 'recovery_needed',
      can_resume: true,
      can_abort: true,
    };
    api.getMemoryProcessingRecord.mockResolvedValue(summary);
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('button', {
      name: `memory.processingRecord.clearRecovery.${action}`,
    }));

    await waitFor(() => expect(api[method]).toHaveBeenCalledWith('clear-42'));
    await waitFor(() => expect(api.getMemoryProcessingRecord).toHaveBeenCalledTimes(2));
  });

  it('awaits a summary reload after a rejected recovery command', async () => {
    const initial = readyProcessingRecord();
    initial.maintenance.clear_recovery = {
      operation_id: 'clear-stale',
      state: 'recovery_needed',
      can_resume: true,
      can_abort: true,
    };
    const refreshed = readyProcessingRecord();
    refreshed.maintenance.clear_recovery = {
      operation_id: 'clear-refreshed',
      state: 'recovery_needed',
      can_resume: true,
      can_abort: false,
    };
    api.getMemoryProcessingRecord
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(refreshed);
    api.resumeMemoryClear.mockResolvedValueOnce({ status: 'failed', error: 'memory_clear_failed' });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('button', {
      name: 'memory.processingRecord.clearRecovery.resume',
    }));

    expect(await screen.findByText('clear-refreshed')).toBeTruthy();
    expect(api.getMemoryProcessingRecord).toHaveBeenCalledTimes(2);
  });

  it('reloads the summary after a failed Clear', async () => {
    api.clearMemory.mockResolvedValue({ status: 'failed', error: 'memory_clear_failed' });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('radio', { name: 'memory.tabs.settings' }));
    await user.click(await screen.findByRole('button', { name: 'open-clear' }));
    await user.click(await screen.findByRole('button', { name: 'confirm-clear' }));

    await waitFor(() => expect(api.getMemoryProcessingRecord).toHaveBeenCalledTimes(2));
  });
});

describe('SettingsMemoryPage restart action', () => {
  it('stays available when the summary cannot be loaded', async () => {
    api.getMemoryProcessingRecord.mockResolvedValue({
      status: 'failed',
      error: 'memory_status_failed',
    });
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
    finishRestart?.({ ok: true, state: 'ready' });
    await waitFor(() => expect((action as HTMLButtonElement).disabled).toBe(false));
  });

  it('is disabled while an embedding rebuild is required', async () => {
    api.getMemorySettings.mockResolvedValue({
      status: 'ok',
      enabled: true,
      rebuild_required: true,
      processing: { llm: endpoint, embedding: endpoint },
    });
    renderPage();

    const action = await screen.findByRole('button', { name: 'memory.status.restartEngine' });
    expect((action as HTMLButtonElement).disabled).toBe(true);
  });

  it('is disabled while the settings panel is running a rebuild', async () => {
    const user = userEvent.setup();
    renderPage();
    const action = await screen.findByRole('button', { name: 'memory.status.restartEngine' });

    await user.click(await screen.findByRole('radio', { name: 'memory.tabs.settings' }));
    await user.click(await screen.findByRole('button', { name: 'begin-rebuild' }));
    expect((action as HTMLButtonElement).disabled).toBe(true);

    await user.click(screen.getByRole('button', { name: 'end-rebuild' }));
    expect((action as HTMLButtonElement).disabled).toBe(false);
  });

  it('awaits factory reset and posts the exact confirmation contract', async () => {
    api.listDependencies.mockResolvedValue({
      ok: true,
      deps: [{ id: 'memory-runtime', kind: 'runtime', required: false, installed: true, status: 'ready', version: '1.0.0' }],
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('radio', { name: 'memory.tabs.settings' }));
    await user.click(await screen.findByRole('button', { name: 'open-factory' }));
    await user.click(screen.getByRole('button', { name: 'confirm-factory' }));

    await waitFor(() => expect(api.factoryResetMemory).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.getMemorySettings).toHaveBeenCalledTimes(2));
  });

  it('shows Retry while a durable factory reset intent remains pending', async () => {
    api.listDependencies.mockResolvedValue({
      ok: true,
      deps: [{ id: 'memory-runtime', kind: 'runtime', required: false, installed: true, status: 'ready', version: '1.0.0' }],
    });
    api.getMemorySettings.mockResolvedValue({
      status: 'ok',
      enabled: true,
      factory_reset_required: true,
      processing: { llm: endpoint, embedding: endpoint },
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('radio', { name: 'memory.tabs.settings' }));
    const retry = await screen.findByRole('button', { name: 'retry-factory' });
    expect((retry as HTMLButtonElement).disabled).toBe(false);
    await user.click(retry);
    expect(await screen.findByRole('button', { name: 'confirm-factory' })).toBeTruthy();
  });

  it('does not derive Retry from a raw recovery_intent field', async () => {
    api.listDependencies.mockResolvedValue({
      ok: true,
      deps: [{ id: 'memory-runtime', kind: 'runtime', required: false, installed: true, status: 'ready', version: '1.0.0' }],
    });
    api.getMemorySettings.mockResolvedValue({
      status: 'ok',
      enabled: true,
      processing: { llm: endpoint, embedding: endpoint },
      recovery_intent: 'factory_reset',
    } as never);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('radio', { name: 'memory.tabs.settings' }));

    expect(await screen.findByRole('button', { name: 'open-factory' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'retry-factory' })).toBeNull();
  });
});
