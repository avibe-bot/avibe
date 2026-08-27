/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MemorySettings } from '../../../context/ApiContext';
import { MemorySettingsPanel } from './MemorySettingsPanel';

const api = vi.hoisted(() => ({ saveMemorySettings: vi.fn() }));
const showToast = vi.hoisted(() => vi.fn());

vi.mock('../../../context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../../../context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('../../../context/ToastContext', () => ({
  useToast: () => ({ showToast }),
}));

vi.mock('../../ui/confirm-dialog', () => ({
  ConfirmDialog: ({ open, onConfirm }: { open: boolean; onConfirm: () => void }) => (
    open ? <button type="button" onClick={onConfirm}>confirm-loss</button> : null
  ),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { returnObjects?: boolean }) => (
      options?.returnObjects ? [] : key
    ),
  }),
}));

const endpoint = (baseUrl: string, model: string) => ({
  base_url: baseUrl,
  model,
  api_key: null,
  has_api_key: true,
});

const settings: MemorySettings = {
  status: 'ok',
  enabled: true,
  mode: 'custom',
  im_attachment_capture_available: false,
  processing: {
    llm: endpoint('https://llm.example.test/v1', 'chat-v1'),
    embedding: endpoint('https://embed.example.test/v1', 'embed-v1'),
  },
};

const renderPanel = (overrides: Partial<React.ComponentProps<typeof MemorySettingsPanel>> = {}) => {
  const props: React.ComponentProps<typeof MemorySettingsPanel> = {
    settings,
    maintenance: { status: 'ok', data_exists: true, can_delete_data: true },
    maintenanceError: null,
    onSaved: vi.fn(),
    onReloadSettings: vi.fn(),
    onReloadMaintenance: vi.fn(),
    onDeleteData: vi.fn(),
    deleting: false,
    ...overrides,
  };
  render(<MemorySettingsPanel {...props} />);
  return props;
};

beforeEach(() => {
  api.saveMemorySettings.mockResolvedValue(settings);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('MemorySettingsPanel', () => {
  it('keeps configuration enablement reachable while Memory is disabled', async () => {
    const user = userEvent.setup();
    renderPanel({ settings: { ...settings, enabled: false } });
    const enable = screen.getByRole('switch', { name: 'memory.settings.enableLabel' });

    expect((enable as HTMLButtonElement).disabled).toBe(false);
    await user.click(enable);
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));

    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledWith({ enabled: true }));
  });

  it('exposes one explicit Delete data action', async () => {
    const props = renderPanel();
    await userEvent.click(screen.getByRole('button', { name: 'memory.deleteData.button' }));
    expect(props.onDeleteData).toHaveBeenCalledOnce();
    expect(screen.queryByText(/Clear Memory|Factory Reset|Rebuild/i)).toBeNull();
  });

  it('disables Delete data when the runtime cannot prove deletion authority', () => {
    renderPanel({
      maintenance: { status: 'ok', data_exists: true, can_delete_data: false },
    });
    expect((screen.getByRole('button', { name: 'memory.deleteData.button' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('requires accepted loss for an embedding identity change', async () => {
    const user = userEvent.setup();
    renderPanel();
    const modelInput = screen.getByLabelText('memory.settings.embeddingTitle: memory.settings.model');
    await user.clear(modelInput);
    await user.type(modelInput, 'embed-v2');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));

    expect(api.saveMemorySettings).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'confirm-loss' }));
    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledOnce());
    expect(api.saveMemorySettings).toHaveBeenCalledWith({
      confirm_loss: true,
      processing: { embedding: { model: 'embed-v2' } },
    });
  });

  it('does not call a standalone rebuild client after a confirmed save', async () => {
    const user = userEvent.setup();
    renderPanel();
    const modelInput = screen.getByLabelText('memory.settings.embeddingTitle: memory.settings.model');
    await user.clear(modelInput);
    await user.type(modelInput, 'embed-v2');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));
    await user.click(screen.getByRole('button', { name: 'confirm-loss' }));

    await waitFor(() => expect(showToast).toHaveBeenCalled());
    expect(Object.keys(api)).toEqual(['saveMemorySettings']);
  });
});
