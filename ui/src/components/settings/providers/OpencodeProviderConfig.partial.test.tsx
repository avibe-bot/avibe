/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { OpencodeProvider } from '@/context/ApiContext';
import { OpencodeProviderConfig } from './OpencodeProviderConfig';

const api = vi.hoisted(() => ({
  getOpencodeProviders: vi.fn(),
  saveOpencodeCustomProvider: vi.fn(),
  setOpencodeProviderAuth: vi.fn(),
}));
const showToast = vi.hoisted(() => vi.fn());

vi.mock('@/context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('@/context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('@/context/ToastContext', () => ({
  useToast: () => ({ showToast }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('../shared/useBackendRuntime', () => ({
  useBackendRuntime: () => ({
    loaded: true,
    enabled: true,
    cliStatus: 'ok',
    configError: null,
  }),
}));

vi.mock('../shared/useOpencodePermission', () => ({
  useOpencodePermission: () => ({
    permissionAllowed: true,
    state: 'idle',
    message: null,
    setupPermission: vi.fn(),
    setPermissionAllowed: vi.fn(),
  }),
}));

vi.mock('../models/useModelHubCapability', () => ({ useModelHubCapability: () => false }));
vi.mock('../shared/BackendRuntimeCard', () => ({ BackendRuntimeCard: () => null }));
vi.mock('../shared/OpencodePermissionSetup', () => ({ OpencodePermissionSetup: () => null }));
vi.mock('../BackendOAuthPanel', () => ({ BackendOAuthPanel: () => null }));
vi.mock('../OpencodeProviderTestPanel', () => ({ OpencodeProviderTestPanel: () => null }));

const provider = (overrides: Partial<OpencodeProvider> = {}): OpencodeProvider => ({
  id: 'deepseek',
  name: 'DeepSeek',
  description: 'DeepSeek provider',
  configured: false,
  oauth_available: false,
  local: false,
  models: [],
  default_model: null,
  ...overrides,
});

beforeEach(() => {
  api.getOpencodeProviders.mockReset();
  api.saveOpencodeCustomProvider.mockReset();
  api.setOpencodeProviderAuth.mockReset();
  showToast.mockReset();
});

afterEach(() => {
  cleanup();
});

describe('OpencodeProviderConfig partial save settlement', () => {
  it('moves a committed custom provider into the catalog and clears its secret', async () => {
    const committed = provider({
      id: 'my-relay',
      name: 'My Relay',
      configured: true,
      custom: true,
      has_auth: true,
      api_key_masked: 'sk-•••elay',
      base_url: 'https://relay.example/v1',
    });
    api.getOpencodeProviders
      .mockResolvedValueOnce({ ok: true, providers: [] })
      .mockResolvedValue({ ok: true, providers: [committed] });
    api.saveOpencodeCustomProvider.mockResolvedValue({
      ok: false,
      partial: true,
      saved: true,
      mutation_attempted: true,
      provider_id: 'my-relay',
      message: 'provider saved, key cleanup failed',
    });
    const modelOptionsChanged = vi.fn();
    window.addEventListener('avibe:opencode-model-options-changed', modelOptionsChanged);
    const user = userEvent.setup();

    render(<OpencodeProviderConfig hideEnableToggle />);
    await screen.findByText('settings.backends.opencodeProvidersEmpty');
    await user.click(screen.getByRole('button', { name: 'settings.backends.opencodeCustomProviderAdd' }));
    await user.type(
      screen.getByPlaceholderText('settings.backends.opencodeCustomProviderNamePlaceholder'),
      'My Relay',
    );
    await user.type(
      screen.getByPlaceholderText('settings.backends.opencodeProviderBaseUrlPlaceholder'),
      'https://relay.example/v1',
    );
    await user.type(
      screen.getByPlaceholderText('settings.backends.opencodeProviderApiKeyPlaceholder'),
      'sk-top-secret',
    );
    await user.click(screen.getByRole('button', { name: 'settings.backends.opencodeCustomProviderSave' }));

    await waitFor(() => expect(api.saveOpencodeCustomProvider).toHaveBeenCalledOnce());
    await screen.findByText('sk-•••elay');
    expect(screen.queryByDisplayValue('sk-top-secret')).toBeNull();
    expect(screen.getByRole('button', { name: 'settings.backends.opencodeProviderCollapse' })).toBeTruthy();
    expect(api.getOpencodeProviders).toHaveBeenCalledTimes(2);
    expect(modelOptionsChanged).toHaveBeenCalledOnce();
    expect(showToast).toHaveBeenCalledWith('provider saved, key cleanup failed', 'warning');
    window.removeEventListener('avibe:opencode-model-options-changed', modelOptionsChanged);
  });

  it('clears and collapses a direct auth secret after a partial save', async () => {
    const initial = provider({
      configured: true,
      has_auth: true,
      api_key_masked: 'sk-•••old',
    });
    const committed = provider({
      configured: true,
      has_auth: true,
      api_key_masked: 'sk-•••cret',
    });
    api.getOpencodeProviders
      .mockResolvedValueOnce({ ok: true, providers: [initial] })
      .mockResolvedValue({ ok: true, providers: [committed] });
    api.setOpencodeProviderAuth.mockResolvedValue({
      ok: false,
      partial: true,
      saved: true,
      mutation_attempted: true,
      provider_id: 'deepseek',
      message: 'saved, cleanup failed',
    });
    const modelOptionsChanged = vi.fn();
    window.addEventListener('avibe:opencode-model-options-changed', modelOptionsChanged);
    const user = userEvent.setup();

    render(<OpencodeProviderConfig hideEnableToggle />);
    await user.click(await screen.findByRole('button', { name: /DeepSeek/ }));
    await user.click(screen.getByRole('button', { name: 'settings.backends.replaceApiKey' }));
    const secret = screen.getByLabelText('settings.backends.opencodeProviderApiKey');
    await user.type(secret, 'sk-top-secret');
    await user.click(screen.getByRole('button', { name: 'settings.backends.opencodeProviderSave' }));

    await waitFor(() => expect(api.setOpencodeProviderAuth).toHaveBeenCalledOnce());
    await screen.findByText('sk-•••cret');
    expect(screen.queryByDisplayValue('sk-top-secret')).toBeNull();
    expect(screen.queryByLabelText('settings.backends.opencodeProviderApiKey')).toBeNull();
    expect(api.getOpencodeProviders).toHaveBeenCalledTimes(2);
    expect(modelOptionsChanged).toHaveBeenCalledOnce();
    window.removeEventListener('avibe:opencode-model-options-changed', modelOptionsChanged);
  });
});
