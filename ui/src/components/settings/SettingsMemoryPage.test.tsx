/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { SettingsMemoryPage } from './SettingsMemoryPage';
import type { MemoryStatus } from '../../context/ApiContext';
import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { DENIED_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';

const api = vi.hoisted(() => ({
  clearMemory: vi.fn(),
  getMemoryFailures: vi.fn(),
  getMemorySettings: vi.fn(),
  getMemoryStatus: vi.fn(),
  listDependencies: vi.fn(),
  restartMemoryRuntime: vi.fn(),
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

vi.mock('./memory/MemoryLogPanel', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('./memory/MemoryLogPanel')>();
  const React = await import('react');
  return {
    ...original,
    MemoryLogPanel: ({ onClearAll }: { onClearAll: () => void }) => {
      const [mount] = React.useState(() => ++logMounts.count);
      return <button type="button" onClick={onClearAll}>open-clear-{mount}</button>;
    },
  };
});

vi.mock('./memory/MemoryProfilePanel', () => ({ MemoryProfilePanel: () => null }));
vi.mock('./memory/MemorySearchPanel', () => ({ MemorySearchPanel: () => null }));
vi.mock('./memory/MemorySettingsPanel', () => ({ MemorySettingsPanel: () => null }));

const endpoint = {
  base_url: 'https://provider.example.test/v1',
  model: 'model-1',
  api_key: null,
  has_api_key: false,
};

const readyStatus = (processingFaultKind: MemoryStatus['processing_fault_kind'] = null): MemoryStatus => ({
  status: 'ok',
  state: 'ready',
  buckets: { syncing: 0, succeeded: 0, unknown: 0, failed: 0, dead: 0, missed: 0 },
  pending: 0,
  processing: 0,
  awaiting_receipt: 0,
  succeeded: 0,
  receipt_unknown: 0,
  distill_failed: 0,
  dead: 0,
  missed: 0,
  queue_plaintext_bytes: 0,
  provider_disk_bytes: 0,
  last_success_at: null,
  last_flush_observation: null,
  last_flush_status: null,
  last_flush_error_code: null,
  last_flush_request_id: null,
  last_flush_at: null,
  processing_fault_kind: processingFaultKind,
  processing_fault_since: processingFaultKind ? '2026-08-04T00:00:00Z' : null,
  processing_alert_active: processingFaultKind !== null,
  error: null,
  data_exists: false,
});

beforeEach(() => {
  logMounts.count = 0;
  api.getMemorySettings.mockResolvedValue({
    status: 'ok',
    enabled: true,
    processing: { llm: endpoint, embedding: endpoint },
  });
  api.getMemoryStatus.mockResolvedValue({ status: 'failed', error: 'memory_status_failed' });
  api.getMemoryFailures.mockResolvedValue({ items: [], retention_days: 90 });
  api.listDependencies.mockResolvedValue({ ok: true, deps: [] });
  api.restartMemoryRuntime.mockResolvedValue({ ok: true, state: 'ready' });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('SettingsMemoryPage Clear handling', () => {
  const openAndConfirmClear = async () => {
    const user = userEvent.setup();

    render(<SettingsMemoryPage />);
    await user.click(await screen.findByRole('radio', { name: 'memory.tabs.log' }));
    await user.click(await screen.findByRole('button', { name: 'open-clear-1' }));
    await user.click(screen.getByRole('button', { name: 'confirm-clear' }));
  };

  it('purges mounted log payload state when Clear reports partial failure', async () => {
    api.clearMemory.mockResolvedValue({ status: 'failed', error: 'memory_clear_failed' });

    await openAndConfirmClear();

    await waitFor(() => expect(api.clearMemory).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole('button', { name: 'open-clear-2' })).toBeTruthy();
    expect(showToast).toHaveBeenCalledWith('errors.memory_clear_failed', 'error');
  });

  it('purges mounted log payload state when the Clear receipt is lost', async () => {
    api.clearMemory.mockRejectedValue(new Error('connection closed'));

    await openAndConfirmClear();

    await waitFor(() => expect(api.clearMemory).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole('button', { name: 'open-clear-2' })).toBeTruthy();
    expect(showToast).toHaveBeenCalledWith('memory.clear.failed', 'error');
  });
});

describe('SettingsMemoryPage restart action', () => {
  it('stays available when status cannot be loaded', async () => {
    render(<SettingsMemoryPage />);

    expect(await screen.findByRole('button', { name: 'memory.status.restartEngine' })).toBeTruthy();
  });

  it('is disabled and shows progress while the request is pending', async () => {
    let finishRestart: ((value: { ok: true; state: string }) => void) | undefined;
    api.restartMemoryRuntime.mockReturnValue(
      new Promise((resolve) => {
        finishRestart = resolve;
      }),
    );
    const user = userEvent.setup();

    render(<SettingsMemoryPage />);
    const action = await screen.findByRole('button', { name: 'memory.status.restartEngine' });
    await user.click(action);

    expect((action as HTMLButtonElement).disabled).toBe(true);
    expect(action.querySelector('.animate-spin')).toBeTruthy();

    finishRestart?.({ ok: true, state: 'ready' });
    await waitFor(() => expect((action as HTMLButtonElement).disabled).toBe(false));
  });

  it('renders exactly one restart action for an engine fault', async () => {
    api.getMemoryStatus.mockResolvedValue(readyStatus('engine'));

    render(<SettingsMemoryPage />);

    expect(await screen.findByText('memory.status.fault.engine')).toBeTruthy();
    expect(screen.getAllByRole('button', { name: 'memory.status.restartEngine' })).toHaveLength(1);
  });
});

describe('SettingsMemoryPage disabled recorder health', () => {
  beforeEach(() => {
    api.getMemorySettings.mockResolvedValue({
      status: 'ok',
      enabled: false,
      processing: { llm: endpoint, embedding: endpoint },
    });
  });

  it('surfaces a retained call-log corruption with the Clear recovery action', async () => {
    api.getMemoryStatus.mockResolvedValue({
      status: 'ok',
      state: 'disabled',
      recorder: { state: 'degraded', reason: 'call_log_corrupt' },
    });
    const user = userEvent.setup();

    render(<SettingsMemoryPage />);
    await user.click(await screen.findByRole('button', { name: 'memory.log.clearAction' }));

    expect(screen.getByRole('button', { name: 'confirm-clear' })).toBeTruthy();
    expect(screen.queryByRole('radio', { name: 'memory.tabs.log' })).toBeNull();
  });

  it('does not offer a restart that the disabled runtime must reject', async () => {
    api.getMemoryStatus.mockResolvedValue({
      status: 'ok',
      state: 'disabled',
      recorder: { state: 'degraded', reason: 'writer_failures' },
    });

    render(<SettingsMemoryPage />);

    await waitFor(() => expect(api.getMemoryStatus).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('memory.log.recorderDegraded')).toBeNull();
    expect(screen.queryByRole('button', { name: 'memory.log.restartAction' })).toBeNull();
    expect(api.restartMemoryRuntime).not.toHaveBeenCalled();
    expect(screen.queryByRole('radio', { name: 'memory.tabs.log' })).toBeNull();
  });

  it('does not offer recorder recovery when enabled Memory is missing its runtime', async () => {
    api.getMemorySettings.mockResolvedValue({
      status: 'ok',
      enabled: true,
      processing: { llm: endpoint, embedding: endpoint },
    });
    api.getMemoryStatus.mockResolvedValue({
      status: 'ok',
      state: 'error',
      recorder: { state: 'degraded', reason: 'writer_failures' },
    });
    api.listDependencies.mockResolvedValue({
      ok: true,
      deps: [{ id: 'memory-runtime', installed: false, status: 'missing' }],
    });

    render(
      <MemoryRouter>
        <SettingsMemoryPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText('memory.setup.runtimeRequired')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'memory.log.restartAction' })).toBeNull();
    expect(screen.getByRole('button', { name: 'memory.status.restartEngine' })).toBeTruthy();
  });
});

describe('SettingsMemoryPage remote administration gate', () => {
  // A remote Instance owner keeps `can_manage_instance`, so only the locality
  // half of the gate can keep the local-only administration routes off screen.
  const renderRemoteOwner = () =>
    render(
      <MemoryRouter>
        <InstanceAuthorizationContext.Provider
          value={{
            remote: true,
            instanceRole: 'owner',
            capabilities: { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true },
          }}
        >
          <SettingsMemoryPage />
        </InstanceAuthorizationContext.Provider>
      </MemoryRouter>,
    );

  it('hides the admin log, the settings tab and the engine restart from a remote owner', async () => {
    renderRemoteOwner();

    expect(await screen.findByRole('radio', { name: 'memory.tabs.status' })).toBeTruthy();
    expect(screen.getByRole('radio', { name: 'memory.tabs.search' })).toBeTruthy();
    expect(screen.queryByRole('radio', { name: 'memory.tabs.log' })).toBeNull();
    expect(screen.queryByRole('radio', { name: 'memory.tabs.settings' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'memory.status.restartEngine' })).toBeNull();
  });

  it('replaces the remote setup flow with the unavailable notice', async () => {
    api.getMemorySettings.mockResolvedValue({
      status: 'ok',
      enabled: false,
      processing: { llm: endpoint, embedding: endpoint },
    });

    renderRemoteOwner();

    expect(await screen.findByText('memory.remoteUnavailable.title')).toBeTruthy();
    expect(screen.queryByRole('radio', { name: 'memory.tabs.status' })).toBeNull();
  });
});
