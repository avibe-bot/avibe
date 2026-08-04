/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { SettingsMemoryPage } from './SettingsMemoryPage';

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
vi.mock('./memory/MemoryStatusPanel', () => ({ MemoryStatusPanel: () => null }));

const endpoint = {
  base_url: 'https://provider.example.test/v1',
  model: 'model-1',
  api_key: null,
  has_api_key: false,
};

beforeEach(() => {
  logMounts.count = 0;
  api.getMemorySettings.mockResolvedValue({
    status: 'ok',
    enabled: true,
    processing: { llm: endpoint, embedding: endpoint },
    diagnostics: { log_provider_calls: true, mutable: true },
  });
  api.getMemoryStatus.mockResolvedValue({ status: 'failed', error: 'memory_status_failed' });
  api.getMemoryFailures.mockResolvedValue({ items: [], retention_days: 90 });
  api.listDependencies.mockResolvedValue({ ok: true, deps: [] });
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

describe('SettingsMemoryPage disabled recorder health', () => {
  beforeEach(() => {
    api.getMemorySettings.mockResolvedValue({
      status: 'ok',
      enabled: false,
      processing: { llm: endpoint, embedding: endpoint },
      diagnostics: { log_provider_calls: false, mutable: true },
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

  it('surfaces a retained call-log writer failure with the Restart recovery action', async () => {
    api.getMemoryStatus.mockResolvedValue({
      status: 'ok',
      state: 'disabled',
      recorder: { state: 'degraded', reason: 'writer_failures' },
    });
    api.restartMemoryRuntime.mockResolvedValue({ ok: true });
    const user = userEvent.setup();

    render(<SettingsMemoryPage />);
    await user.click(await screen.findByRole('button', { name: 'memory.log.restartAction' }));

    await waitFor(() => expect(api.restartMemoryRuntime).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('radio', { name: 'memory.tabs.log' })).toBeNull();
  });

  it('does not offer recorder recovery when enabled Memory is missing its runtime', async () => {
    api.getMemorySettings.mockResolvedValue({
      status: 'ok',
      enabled: true,
      processing: { llm: endpoint, embedding: endpoint },
      diagnostics: { log_provider_calls: true, mutable: true },
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
  });
});
