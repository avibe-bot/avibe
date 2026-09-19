// @vitest-environment jsdom
import { I18nextProvider } from 'react-i18next';
import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { BackendConnectionForm } from './providers/BackendConnectionForm';
import { BackendOAuthPanel } from './BackendOAuthPanel';
import { BackendTestPanel } from './BackendTestPanel';
import { OpencodeProviderTestPanel } from './OpencodeProviderTestPanel';

const mock = vi.hoisted(() => ({
  api: {
    getClaudeAuth: vi.fn(),
    getCodexAuth: vi.fn(),
    getOpencodeProviders: vi.fn(),
    getBackendConnection: vi.fn(),
    saveClaudeAuth: vi.fn(),
    saveCodexAuth: vi.fn(),
    setOpencodeProviderAuth: vi.fn(),
    startOAuthWeb: vi.fn(),
    startOAuthWebForOpencodeProvider: vi.fn(),
    getOAuthWebStatus: vi.fn(),
    submitOAuthWebCode: vi.fn(),
    cancelOAuthWeb: vi.fn(),
    removeClaudeOAuthCredentials: vi.fn(),
    removeBackendAuth: vi.fn(),
    removeBackendApiKey: vi.fn(),
    deleteOpencodeProviderAuth: vi.fn(),
    claudeModels: vi.fn(),
    codexModels: vi.fn(),
    testBackendAuth: vi.fn(),
    testOpencodeProvider: vi.fn(),
  },
  toast: vi.fn(),
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => mock.api }));
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: mock.toast }) }));

const i18n = createInstance();
await i18n.init({
  lng: 'en',
  fallbackLng: false,
  resources: { en: { translation: en }, zh: { translation: zh } },
});

const connection = (backend: 'claude' | 'codex' | 'opencode' = 'claude') => ({
  ok: true,
  backend,
  installed: true,
  enabled: true,
  auth: 'api_key',
  application: 'applied',
  ready: true,
  entry_eligible: true,
});

const claudeAuth = () => ({
  ok: true,
  auth_mode: 'api_key',
  active_auth_mode: 'api_key',
  has_api_key: true,
  api_key_length: 20,
  api_key_masked: 'sk-•••old',
  base_url: 'https://old.example',
  credential_type: 'api_key',
  has_oauth_credentials: true,
  settings_path: '/fixture/settings.json',
  settings_exists: true,
  settings_env_has_key: true,
  settings_env_key_length: 20,
  settings_env_key_var: 'ANTHROPIC_API_KEY',
  settings_env_base_url: 'https://old.example',
  settings_conflict: false,
});

const LocationProbe = () => {
  const location = useLocation();
  return <span data-testid="location">{location.pathname}</span>;
};

const renderInRouter = (node: React.ReactNode, initialPath = '/setup') =>
  render(
    <MemoryRouter initialEntries={[initialPath]}>
      <I18nextProvider i18n={i18n}>
        {node}
        <LocationProbe />
      </I18nextProvider>
    </MemoryRouter>,
  );

beforeEach(async () => {
  await i18n.changeLanguage('en');
  vi.resetAllMocks();
  mock.api.getClaudeAuth.mockResolvedValue(claudeAuth());
  mock.api.getCodexAuth.mockResolvedValue({
    ...claudeAuth(),
    has_chatgpt_tokens: true,
    credentials_store: 'file',
    file_store_active: true,
  });
  mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [] });
  mock.api.getBackendConnection.mockImplementation(async (backend: 'claude' | 'codex' | 'opencode') => connection(backend));
  mock.api.saveClaudeAuth.mockResolvedValue({ ok: true, restart: { ok: true } });
  mock.api.saveCodexAuth.mockResolvedValue({ ok: true, restart: { ok: true } });
  mock.api.setOpencodeProviderAuth.mockResolvedValue({ ok: true, restart: { ok: true } });
  mock.api.cancelOAuthWeb.mockResolvedValue({ ok: true });
  mock.api.claudeModels.mockResolvedValue({ ok: true, models: [] });
  mock.api.codexModels.mockResolvedValue({ ok: true, models: [] });
});

afterEach(() => cleanup());

describe('native authentication ownership presentation', () => {
  it('uses Hub supply mode before touching native auth stores', async () => {
    mock.api.getBackendConnection.mockResolvedValue({
      ...connection('claude'),
      supply_mode: 'hub',
      ready: false,
      entry_eligible: false,
      auth: 'none',
    });
    renderInRouter(
      <BackendConnectionForm
        backend="claude"
        compact
        initialMethod="api_key"
        onCancel={vi.fn()}
      />,
    );

    expect(await screen.findByText(en.settings.backends.nativeAuthHubOwned)).toBeTruthy();
    expect(mock.api.getClaudeAuth).not.toHaveBeenCalled();
  });

  it('shows the localized compact onboarding notice and navigates to Model Hub', async () => {
    const onCancel = vi.fn();
    mock.api.saveClaudeAuth.mockResolvedValue({
      ok: false,
      error: 'native_auth_hub_owned',
      reauth_channel: 'hub',
    });
    renderInRouter(
      <BackendConnectionForm
        backend="claude"
        compact
        initialMethod="api_key"
        onCancel={onCancel}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.connection.saveConnect }));
    expect(await screen.findByText(en.settings.backends.nativeAuthHubOwned)).toBeTruthy();
    expect(screen.queryByText('native_auth_hub_owned')).toBeNull();

    fireEvent.click(screen.getByRole('link', { name: en.settings.backends.openModelHub }));
    expect(onCancel).toHaveBeenCalledOnce();
    expect(screen.getByTestId('location').textContent).toBe('/settings/models');
  });

  it('shows the same ownership notice in Settings in Chinese without exposing the code', async () => {
    await i18n.changeLanguage('zh');
    mock.api.saveClaudeAuth.mockResolvedValue({
      ok: false,
      error: 'native_auth_hub_owned',
      reauth_channel: 'hub',
    });
    renderInRouter(
      <BackendConnectionForm backend="claude" initialMethod="api_key" />,
      '/settings/backends/claude',
    );

    fireEvent.click(await screen.findByRole('button', { name: zh.common.save }));
    expect(await screen.findByText(zh.settings.backends.nativeAuthHubOwned)).toBeTruthy();
    expect(screen.getByRole('link', { name: zh.settings.backends.openModelHub })).toBeTruthy();
    expect(screen.queryByText('native_auth_hub_owned')).toBeNull();
  });

  it('classifies an OAuth start refusal before requiring a flow id', async () => {
    const onCancel = vi.fn();
    mock.api.startOAuthWeb.mockResolvedValue({
      ok: false,
      error: 'native_auth_hub_owned',
      reauth_channel: 'hub',
    });
    renderInRouter(
      <BackendConnectionForm backend="claude" compact onCancel={onCancel} />,
    );

    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.connection.claudeSignIn }));
    expect(await screen.findByText(en.settings.backends.nativeAuthHubOwned)).toBeTruthy();
    expect(screen.queryByText('native_auth_hub_owned')).toBeNull();
    expect(mock.api.saveClaudeAuth).not.toHaveBeenCalled();
  });

  it('maps OAuth removal refusal to the existing Model Hub action', async () => {
    mock.api.removeBackendAuth.mockResolvedValue({
      ok: false,
      error: 'native_auth_hub_owned',
      reauth_channel: 'hub',
    });
    renderInRouter(
      <BackendOAuthPanel
        backend="claude"
        signedIn
        canRemoveAuth
        title="Claude"
        subtitle="Fixture"
      />,
      '/settings/backends/claude',
    );

    fireEvent.click(await screen.findByRole('button', { name: en.settings.backends.oauthRemove }));
    expect(await screen.findByText(en.settings.backends.nativeAuthHubOwned)).toBeTruthy();
    expect(screen.getByRole('link', { name: en.settings.backends.openModelHub })).toBeTruthy();
    expect(screen.queryByText('native_auth_hub_owned')).toBeNull();
  });

  it.each([
    ['Claude', BackendTestPanel, undefined],
    ['OpenCode', OpencodeProviderTestPanel, 'provider'],
  ] as const)('maps %s probe refusal without a raw error code', async (_name, Panel, providerId) => {
    if (providerId) {
      mock.api.testOpencodeProvider.mockResolvedValue({
        ok: false,
        error: 'native_auth_hub_owned',
        reauth_channel: 'hub',
      });
      renderInRouter(
        <Panel providerId="fixture" providerName="Fixture" models={['model']} />,
        '/settings/backends/opencode',
      );
      fireEvent.click(await screen.findByRole('button', { name: en.settings.backends.testConnectionRun }));
    } else {
      mock.api.testBackendAuth.mockResolvedValue({
        ok: false,
        error: 'native_auth_hub_owned',
        reauth_channel: 'hub',
      });
      renderInRouter(<Panel backend="claude" />, '/settings/backends/claude');
      fireEvent.click(await screen.findByRole('button', { name: en.settings.backends.testConnectionRun }));
    }

    expect(await screen.findByText(en.settings.backends.nativeAuthHubOwned)).toBeTruthy();
    expect(screen.getByRole('link', { name: en.settings.backends.openModelHub })).toBeTruthy();
    expect(screen.queryByText('native_auth_hub_owned')).toBeNull();
  });
});
