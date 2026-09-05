/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { ToastProvider } from '../../context/ToastProvider';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import { SettingsMemoryPage } from './SettingsMemoryPage';

const api = vi.hoisted(() => ({
  deleteMemoryData: vi.fn(),
  getMemoryMaintenance: vi.fn(),
  getMemoryProcessingRecord: vi.fn(),
  getMemorySettings: vi.fn(),
  getMemoryStatus: vi.fn(),
  listDependencies: vi.fn(),
  repairMemory: vi.fn(),
  wakeMemory: vi.fn(),
}));
const translate = vi.hoisted(() => (key: string) => key);

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: translate }),
}));

vi.mock('../../context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../../context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('../ui/confirm-dialog', () => ({
  ConfirmDialog: ({ open, onConfirm, children }: { open: boolean; onConfirm: () => void; children?: ReactNode }) => (
    open ? (
      <div role="dialog">
        {children}
        <button type="button" onClick={onConfirm}>confirm-delete</button>
      </div>
    ) : null
  ),
}));

vi.mock('./memory/MemoryProcessingRecordPanel', () => ({
  MemoryProcessingRecordPanel: () => null,
}));
vi.mock('./memory/MemoryProfilePanel', () => ({ MemoryProfilePanel: () => null }));
vi.mock('./memory/MemorySearchPanel', () => ({ MemorySearchPanel: () => null }));
vi.mock('./memory/MemorySettingsPanel', () => ({
  MemorySettingsPanel: ({ onDeleteData }: { onDeleteData: () => void }) => (
    <button type="button" onClick={onDeleteData}>open-delete</button>
  ),
}));

vi.mock('./memory/MemoryStatusPanel', () => ({
  MemoryStatusPanel: ({
    repairSupported,
    onRepair,
    failuresError,
    failuresNotice,
  }: {
    repairSupported?: boolean;
    onRepair?: () => void;
    failuresError?: string | null;
    failuresNotice?: string | null;
  }) => (
    <div>
      <span>{repairSupported ? 'repair-supported' : 'repair-hidden'}</span>
      {repairSupported ? <button type="button" onClick={onRepair}>run-repair</button> : null}
      <span data-testid="memory-failures-message">{failuresError ?? ''}</span>
      <span data-testid="memory-failures-notice">{failuresNotice ?? ''}</span>
    </div>
  ),
}));

const settings = {
  status: 'ok' as const,
  enabled: true,
  mode: 'custom' as const,
  processing: {
    llm: { base_url: null, model: null, api_key: null, has_api_key: false },
    embedding: { base_url: null, model: null, api_key: null, has_api_key: false },
  },
};

const status = (state: 'starting' | 'running' | 'degraded' | 'needs_repair') => ({
  status: 'ok' as const,
  state,
  reason: state === 'needs_repair' ? 'memory_local_data_unusable' : null,
  source: { status: 'unavailable' as const, observed_at: null, reason: null },
  health: null,
});

const renderPage = () => render(
  <MemoryRouter>
    <InstanceAuthorizationContext.Provider value={{
      remote: false,
      instanceKind: null,
      instanceRole: 'owner',
      capabilities: OWNER_INSTANCE_CAPABILITIES,
    }}>
      <ToastProvider>
        <SettingsMemoryPage />
      </ToastProvider>
    </InstanceAuthorizationContext.Provider>
  </MemoryRouter>,
);

beforeEach(() => {
  api.getMemorySettings.mockResolvedValue(settings);
  api.getMemoryStatus.mockResolvedValue(status('needs_repair'));
  api.getMemoryProcessingRecord.mockResolvedValue({
    status: 'ok',
    runtime: { source: status('running').source, health: null },
    sources: {
      memcells: { status: 'unknown', observed_at: null },
      runs: { status: 'unknown', observed_at: null },
      semantic: { status: 'unknown', observed_at: null },
    },
    anomalies: { source: { status: 'available', observed_at: null }, items: [] },
    maintenance: {
      source: { status: 'available', observed_at: null },
      data_exists: true,
      can_delete_data: true,
    },
  });
  api.getMemoryMaintenance.mockResolvedValue({
    status: 'ok',
    data_exists: true,
    can_delete_data: true,
  });
  api.listDependencies.mockResolvedValue({
    deps: [{ id: 'memory-runtime', installed: true, status: 'ready' }],
  });
  api.wakeMemory.mockResolvedValue({ ok: true, operation: 'wake', state: 'running' });
  api.repairMemory.mockResolvedValue({
    ok: true,
    operation: 'repair',
    result: 'completed',
    data_deleted: true,
    data_remaining: false,
    roots: [],
  });
  api.deleteMemoryData.mockResolvedValue({
    ok: true,
    operation: 'delete_data',
    result: 'completed',
    data_deleted: true,
    data_remaining: false,
    roots: [],
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('SettingsMemoryPage', () => {
  it('inspects only the Memory dependency owner', async () => {
    renderPage();
    await waitFor(() => expect(api.listDependencies).toHaveBeenCalledWith({
      ids: ['memory-package', 'memory-runtime'],
    }));
  });

  it('directs disabled Memory to the explicit package bootstrap when missing', async () => {
    api.getMemorySettings.mockResolvedValue({ ...settings, enabled: false });
    api.listDependencies.mockResolvedValue({
      deps: [
        { id: 'memory-package', installed: false, status: 'missing', action_class: 'repairable' },
        { id: 'memory-runtime', installed: null, status: 'not_required', action_class: 'none' },
      ],
    });

    renderPage();

    await screen.findByText('repair-supported');
    expect(screen.getByText('memory.setup.runtimeRequired')).toBeTruthy();
    expect(screen.getByRole('link', { name: /memory.settings.goToDependencies/ })).toBeTruthy();
  });

  it('offers Retry startup for degraded Memory', async () => {
    api.getMemoryStatus.mockResolvedValue(status('degraded'));
    renderPage();
    await userEvent.click(await screen.findByRole('button', { name: 'memory.runtimeAction.retryButton' }));
    await waitFor(() => expect(api.wakeMemory).toHaveBeenCalledOnce());
    expect(api.repairMemory).not.toHaveBeenCalled();
    expect(api.deleteMemoryData).not.toHaveBeenCalled();
  });

  it('keeps manual restart in More actions while Memory is running', async () => {
    api.getMemoryStatus.mockResolvedValue(status('running'));
    renderPage();

    expect(screen.queryByRole('button', { name: 'memory.runtimeAction.retryButton' })).toBeNull();
    await userEvent.click(await screen.findByRole('button', { name: 'memory.runtimeAction.moreActions' }));
    expect(await screen.findByText('memory.runtimeAction.restartDescription')).toBeTruthy();
    await userEvent.click(screen.getByRole('menuitem', { name: /memory.runtimeAction.restartButton/ }));

    await waitFor(() => expect(api.wakeMemory).toHaveBeenCalledOnce());
    expect(api.repairMemory).not.toHaveBeenCalled();
    expect(api.deleteMemoryData).not.toHaveBeenCalled();
  });

  it.each(['starting', 'needs_repair'] as const)('hides runtime restart actions while Memory is %s', async (state) => {
    api.getMemoryStatus.mockResolvedValue(status(state));
    renderPage();

    await screen.findByText(state === 'needs_repair' ? 'repair-supported' : 'repair-hidden');
    expect(screen.queryByRole('button', { name: 'memory.runtimeAction.retryButton' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'memory.runtimeAction.moreActions' })).toBeNull();
  });

  it('offers Repair only for needs_repair and passes literal accepted loss', async () => {
    renderPage();
    expect(await screen.findByText('repair-supported')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'run-repair' }));
    await waitFor(() => expect(api.repairMemory).toHaveBeenCalledWith(true));
  });

  it('does not offer Repair for provider degradation', async () => {
    api.getMemoryStatus.mockResolvedValue(status('degraded'));
    renderPage();
    expect(await screen.findByText('repair-hidden')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'run-repair' })).toBeNull();
  });

  it('treats intentionally unretained failure history as an informational notice', async () => {
    api.getMemoryProcessingRecord.mockResolvedValue({
      status: 'ok',
      runtime: { source: status('running').source, health: null },
      sources: {
        memcells: { status: 'available', observed_at: '2026-08-24T00:00:00Z' },
        runs: { status: 'available', observed_at: '2026-08-24T00:00:00Z' },
        semantic: { status: 'available', observed_at: '2026-08-24T00:00:00Z' },
      },
      anomalies: {
        source: {
          status: 'unavailable',
          observed_at: null,
          reason: 'memory_failure_history_unavailable',
        },
        items: [],
      },
      maintenance: {
        source: { status: 'available', observed_at: '2026-08-24T00:00:00Z' },
        data_exists: true,
        can_delete_data: true,
      },
    });

    renderPage();

    await waitFor(() => expect(screen.getByTestId('memory-failures-message').textContent).toBe(''));
    expect(screen.getByTestId('memory-failures-notice').textContent).toBe(
      'memory.processingRecord.reason.memory_failure_history_unavailable',
    );
  });

  it('keeps bounded per-root outcomes visible after deletion fails', async () => {
    api.deleteMemoryData.mockResolvedValueOnce({
      ok: false,
      operation: 'delete_data',
      result: 'partial',
      error: 'memory_delete_data_failed',
      data_deleted: false,
      data_remaining: true,
      roots: [
        {
          path: 'memory',
          existed: true,
          deleted: false,
          error: 'ConfinedFilesystemError',
        },
        {
          path: 'state/memory/clear-intent.json',
          existed: false,
          deleted: false,
        },
      ],
    });
    renderPage();

    await userEvent.click(await screen.findByRole('radio', { name: 'memory.tabs.settings' }));
    await userEvent.click(screen.getByRole('button', { name: 'open-delete' }));
    await userEvent.click(screen.getByRole('button', { name: 'confirm-delete' }));

    await waitFor(() => expect(api.deleteMemoryData).toHaveBeenCalledWith(true));
    expect(await screen.findByText('memory.deleteData.rootResultsTitle')).toBeTruthy();
    expect(screen.getByText('memory')).toBeTruthy();
    expect(screen.getByText('ConfinedFilesystemError')).toBeTruthy();
    expect(screen.getByText('state/memory/clear-intent.json')).toBeTruthy();
    expect(screen.getByText('memory.deleteData.rootStatus.absent')).toBeTruthy();
  });
});
