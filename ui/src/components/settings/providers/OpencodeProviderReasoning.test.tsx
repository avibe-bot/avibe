// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EFFORT_BY_BACKEND } from '@/lib/effortOptions';
import { OpencodeProviderConfig } from './OpencodeProviderConfig';

const mocks = vi.hoisted(() => ({
  getOpencodeProviders: vi.fn(),
  saveOpencodeProviderModel: vi.fn(),
  setPermissionAllowed: vi.fn(),
  showToast: vi.fn(),
}));
vi.mock('@/context/ApiContext', () => ({ useApi: () => mocks }));
vi.mock('@/context/ToastContext', () => ({ useToast: () => mocks }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key === 'chat.picker.effortOptions.none' ? 'Off' : key }),
}));
vi.mock('../shared/useBackendRuntime', () => ({
  useBackendRuntime: () => ({ loaded: true, enabled: true, cliStatus: 'ok' }),
}));
vi.mock('../shared/useOpencodePermission', () => ({ useOpencodePermission: () => mocks }));
vi.mock('../models/useModelHubCapability', () => ({ useModelHubCapability: () => false }));
vi.mock('../shared/BackendRuntimeCard', () => ({ BackendRuntimeCard: () => null }));
vi.mock('./BackendConnectionForm', () => ({ BackendConnectionForm: () => null }));
vi.mock('../OpencodeProviderTestPanel', () => ({ OpencodeProviderTestPanel: () => null }));

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('OpenCode per-model reasoning declarations', () => {
  it.each([false, true])('only saves Off after opting in and resets for the next model (optIn=%s)', async (optIn) => {
    mocks.getOpencodeProviders.mockResolvedValue({
      ok: true,
      permission_allowed: true,
      providers: [{
        id: 'fixture', name: 'Fixture provider', description: '', models: [],
        configured: true, custom: true, local: false, oauth_available: false,
      }],
    });
    mocks.saveOpencodeProviderModel.mockResolvedValue({ ok: true });
    render(<OpencodeProviderConfig />);
    fireEvent.click(await screen.findByRole('button', { name: /Fixture provider/ }));
    const off = () => screen.getByRole('button', { name: 'Off', exact: true });
    expect(off().className).not.toContain('bg-cyan-soft');
    for (const effort of EFFORT_BY_BACKEND.opencode) {
      expect(screen.getByRole('button', { name: effort, exact: true }).className).toContain('bg-cyan-soft');
    }
    expect(mocks.saveOpencodeProviderModel).not.toHaveBeenCalled();
    if (optIn) fireEvent.click(off());
    fireEvent.change(screen.getByPlaceholderText('settings.backends.opencodeProviderModelPlaceholder'), {
      target: { value: '模型/declared' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'settings.backends.opencodeProviderModelAdd' }));
    await waitFor(() => expect(mocks.saveOpencodeProviderModel).toHaveBeenCalledWith('fixture', {
      model_id: '模型/declared',
      reasoning_efforts: optIn ? ['none', ...EFFORT_BY_BACKEND.opencode] : EFFORT_BY_BACKEND.opencode,
    }));
    await waitFor(() => expect(off().className).not.toContain('bg-cyan-soft'));
    expect(mocks.saveOpencodeProviderModel).toHaveBeenCalledTimes(1);
  });
});
