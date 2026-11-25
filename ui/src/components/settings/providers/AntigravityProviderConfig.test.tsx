// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { BackendConnectionDialog } from '@/components/onboarding/BackendConnectionDialog';
import { modelsApi } from '../models/modelsApi';
import { BackendTestPanel } from '../BackendTestPanel';
import { AntigravityProviderConfig } from './AntigravityProviderConfig';
import type { AntigravityAuthState, BackendConnectionState } from '@/context/ApiContext';

const mock = vi.hoisted(() => ({
  toast: vi.fn(),
  api: {
    getConfig: vi.fn(),
    detectCli: vi.fn(),
    mutateConfig: vi.fn(),
    getBackendRuntime: vi.fn(),
    restartBackend: vi.fn(),
    getBackendConnection: vi.fn(),
    getAntigravityAuth: vi.fn(),
    saveAntigravityAuth: vi.fn(),
    antigravityModels: vi.fn(),
    startOAuthWeb: vi.fn(),
    getOAuthWebStatus: vi.fn(),
    submitOAuthWebCode: vi.fn(),
    cancelOAuthWeb: vi.fn(),
    removeBackendAuth: vi.fn(),
    removeBackendApiKey: vi.fn(),
    testBackendAuth: vi.fn(),
  },
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => mock.api }));
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: mock.toast }) }));
vi.mock('../models/useModelHubCapability', () => ({ useModelHubCapability: () => true }));

const i18n = createInstance();
await i18n.init({
  lng: 'en',
  fallbackLng: false,
  resources: { en: { translation: en }, zh: { translation: zh } },
});

const wrap = (node: React.ReactNode) => (
  <MemoryRouter>
    <I18nextProvider i18n={i18n}>{node}</I18nextProvider>
  </MemoryRouter>
);

const connection = (patch: Partial<BackendConnectionState> = {}): BackendConnectionState => ({
  ok: true,
  backend: 'antigravity',
  installed: true,
  cli_status: 'ok',
  enabled: true,
  auth: 'none',
  application: 'applied',
  ready: true,
  entry_eligible: true,
  supply_mode: 'direct',
  ...patch,
});

const authState = (patch: Partial<AntigravityAuthState> = {}): AntigravityAuthState => ({
  ok: true,
  auth_mode: 'oauth-personal',
  authenticated: false,
  credentials_present: false,
  catalog_available: true,
  cli_installed: true,
  cli_version: '1.5.16',
  has_api_key: false,
  api_key_masked: null,
  base_url: null,
  gcp_project: null,
  gcp_location: null,
  adc_present: false,
  adc_command: 'gcloud auth application-default login',
  ...patch,
});

beforeEach(async () => {
  await i18n.changeLanguage('en');
  vi.resetAllMocks();
  mock.api.getConfig.mockResolvedValue({ agents: { antigravity: { enabled: true, cli_path: 'agy' } } });
  mock.api.detectCli.mockResolvedValue({ found: true, status: 'ok', path: '/usr/local/bin/agy' });
  mock.api.getBackendRuntime.mockResolvedValue({
    ok: true,
    name: 'antigravity',
    cli_path: 'agy',
    resolved_path: '/usr/local/bin/agy',
    status: 'ok',
    enabled: true,
    installed: true,
    current_version: '1.5.16',
    process_status: 'running',
  });
  mock.api.getBackendConnection.mockResolvedValue(connection());
  mock.api.getAntigravityAuth.mockResolvedValue(authState());
  mock.api.saveAntigravityAuth.mockResolvedValue({ ...authState({ authenticated: true }), restart: { ok: true } });
  mock.api.antigravityModels.mockResolvedValue({ ok: true, models: ['gemini-4-neon', 'gemini-3.8-flash'] });
  mock.api.cancelOAuthWeb.mockResolvedValue({ ok: true });
  mock.api.removeBackendApiKey.mockResolvedValue({ ok: true, restart: { ok: true } });
  vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([]);
  vi.spyOn(modelsApi, 'scanMigration').mockResolvedValue({ items: [] });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('Antigravity settings, auth flows, and onboarding', () => {
  it.each(['en', 'zh'] as const)('completes %s Google paste-code OAuth without personal automation notice or Model Hub card', async (lang) => {
    await i18n.changeLanguage(lang);
    mock.api.startOAuthWeb.mockResolvedValue({
      ok: true,
      flow_id: 'flow_agy',
      backend: 'antigravity',
      state: 'awaiting_code',
      callback_kind: 'code',
      url: 'https://accounts.google.com/o/oauth2/v2/auth?code=1',
    });
    mock.api.submitOAuthWebCode.mockResolvedValue({ ok: true });

    render(wrap(<AntigravityProviderConfig />));

    const signInLabel = lang === 'en' ? en.settings.backends.antigravitySignInGoogle : zh.settings.backends.antigravitySignInGoogle;
    const signInButton = await screen.findByRole('button', { name: signInLabel });
    expect(screen.queryByRole('note')).toBeNull();
    expect(screen.queryByText(lang === 'en' ? en.settings.models.supplyMode.title : zh.settings.models.supplyMode.title)).toBeNull();
    expect(modelsApi.listAgents).not.toHaveBeenCalled();
    expect(mock.api.testBackendAuth).not.toHaveBeenCalled();

    fireEvent.click(signInButton);
    await waitFor(() => expect(mock.api.startOAuthWeb).toHaveBeenCalledWith('antigravity', false, 'oauth-personal'));

    await screen.findByRole('link', {
      name: lang === 'en' ? en.settings.models.oauth.openSignIn : zh.settings.models.oauth.openSignIn,
    });

    const codeInput = screen.getByPlaceholderText(
      lang === 'en' ? en.settings.backends.antigravityCallbackCodePlaceholder : zh.settings.backends.antigravityCallbackCodePlaceholder,
    );
    fireEvent.change(codeInput, { target: { value: 'paste-code-123' } });
    fireEvent.click(screen.getByRole('button', { name: lang === 'en' ? en.common.submit : zh.common.submit }));
    await waitFor(() => expect(mock.api.submitOAuthWebCode).toHaveBeenCalledWith('antigravity', 'flow_agy', 'paste-code-123'));
  });

  it('shows Gemini Enterprise terminal block only when OAuth returns terminal guidance, and handles Gemini API key and Agent Platform ADC visibility', async () => {
    mock.api.getAntigravityAuth.mockResolvedValue(
      authState({
        auth_mode: 'gemini-api-key',
        authenticated: true,
        credentials_present: true,
        has_api_key: true,
        api_key_masked: 'AIza…9xyz',
        base_url: 'https://generativelanguage.googleapis.com',
        gcp_project: 'demo-proj',
        gcp_location: 'global',
        adc_present: false,
      }),
    );
    mock.api.startOAuthWeb.mockResolvedValue({
      ok: true,
      flow_id: 'flow_enterprise',
      backend: 'antigravity',
      state: 'awaiting_code',
      terminal_command: 'agy auth login --enterprise',
      instructions: 'Run this command in a terminal to complete Gemini Enterprise sign-in.',
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(wrap(<AntigravityProviderConfig />));

    // Starts on saved Gemini API key mode
    expect(await screen.findByText('AIza…9xyz')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: en.settings.backends.antigravityApiKeyRemove }));
    await waitFor(() => expect(mock.api.removeBackendApiKey).toHaveBeenCalledWith('antigravity'));

    // Switch to Gemini Enterprise: terminal block is NOT shown by default
    fireEvent.click(screen.getByRole('radio', { name: en.settings.backends.antigravityMethodEnterprise }));
    expect(screen.queryByText(en.settings.backends.antigravityEnterpriseTerminalTitle)).toBeNull();
    expect(screen.queryByText('agy')).toBeNull();

    // Starting OAuth with terminal command/instructions reveals the terminal block
    fireEvent.click(screen.getByRole('button', { name: en.settings.backends.antigravitySignInEnterprise }));
    await waitFor(() => expect(mock.api.startOAuthWeb).toHaveBeenCalledWith('antigravity', false, 'oauth-business'));
    expect(await screen.findByText(en.settings.backends.antigravityEnterpriseTerminalTitle)).toBeTruthy();
    expect(screen.getByText('agy auth login --enterprise')).toBeTruthy();
    expect(screen.getByText('Run this command in a terminal to complete Gemini Enterprise sign-in.')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: en.common.cancel }));
    await waitFor(() => expect(screen.queryByText('agy auth login --enterprise')).toBeNull());

    // Switch to Agent Platform when ADC is missing: ADC command IS shown
    fireEvent.click(screen.getByRole('radio', { name: en.settings.backends.antigravityMethodAgentPlatform }));
    expect(screen.getByText('gcloud auth application-default login')).toBeTruthy();
    fireEvent.change(screen.getByLabelText(en.settings.backends.antigravityGcpProjectLabel), {
      target: { value: 'prod-agent-proj' },
    });
    fireEvent.change(screen.getByLabelText(en.settings.backends.antigravityGcpLocationLabel), {
      target: { value: 'us-central1' },
    });
    fireEvent.click(screen.getByRole('button', { name: en.common.save }));
    await waitFor(() =>
      expect(mock.api.saveAntigravityAuth).toHaveBeenCalledWith({
        auth_mode: 'agent-platform',
        gcp_project: 'prod-agent-proj',
        gcp_location: 'us-central1',
      }),
    );
  });

  it('hides the ADC terminal command on Agent Platform when ADC is already detected', async () => {
    mock.api.getAntigravityAuth.mockResolvedValue(
      authState({
        auth_mode: 'agent-platform',
        authenticated: true,
        credentials_present: true,
        gcp_project: 'prod-agent-proj',
        gcp_location: 'global',
        adc_present: true,
        adc_command: 'gcloud auth application-default login',
      }),
    );

    render(wrap(<AntigravityProviderConfig />));

    expect(await screen.findByText(en.settings.backends.antigravityAdcPresent)).toBeTruthy();
    expect(screen.queryByText(en.settings.backends.antigravityAdcCommandHint)).toBeNull();
    expect(screen.queryByText('gcloud auth application-default login')).toBeNull();
  });

  it('pre-selects an Avibe-owned model in BackendTestPanel and shows structured auth/catalog/inference results', async () => {
    mock.api.testBackendAuth.mockResolvedValue({
      ok: true,
      duration_ms: 210,
      excerpt: 'Hi there!',
      authenticated: true,
      catalog_available: true,
      inference_verified: true,
    });

    render(wrap(<BackendTestPanel backend="antigravity" />));
    const select = (await screen.findByLabelText(en.settings.backends.testConnectionModelLabel)) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe('gemini-4-neon'));
    expect(screen.queryByRole('option', { name: en.settings.backends.testConnectionModelDefault })).toBeNull();
    expect(mock.api.testBackendAuth).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: en.settings.backends.testConnectionRun }));
    await waitFor(() =>
      expect(mock.api.testBackendAuth).toHaveBeenCalledWith('antigravity', { model: 'gemini-4-neon' }),
    );
    expect(await screen.findByText(en.settings.backends.testConnectionAuthOk)).toBeTruthy();
    expect(screen.getByText(en.settings.backends.testConnectionCatalogOk)).toBeTruthy();
    expect(screen.getByText(en.settings.backends.testConnectionInferenceOk)).toBeTruthy();
  });

  it('opens onboarding BackendConnectionDialog for Antigravity without personal automation notice or default Enterprise terminal block', async () => {
    render(
      wrap(
        <BackendConnectionDialog
          backend="antigravity"
          method="oauth"
          onClose={vi.fn()}
          onConnected={vi.fn(async () => {})}
          onWriteState={vi.fn()}
        />,
      ),
    );
    expect(await screen.findByText(en.onboarding.connection.antigravityDialogIntro)).toBeTruthy();
    expect(screen.queryByRole('note')).toBeNull();

    fireEvent.click(screen.getByRole('radio', { name: en.settings.backends.antigravityMethodEnterpriseCompact }));
    expect(screen.queryByText(en.settings.backends.antigravityEnterpriseTerminalTitle)).toBeNull();
  });

  it('keeps BackendConnectionDialog open with ADC recovery guidance when Agent Platform saves without ADC, and connects once refresh confirms ADC', async () => {
    const onClose = vi.fn();
    const onConnected = vi.fn(async () => {});
    const missingAdcAuth = authState({
      auth_mode: 'agent-platform',
      authenticated: false,
      credentials_present: false,
      adc_present: false,
      gcp_project: 'prod-agent-proj',
      gcp_location: 'us-central1',
      adc_command: 'gcloud auth application-default login',
      error: 'adc_missing',
    });
    mock.api.getBackendConnection.mockResolvedValue(connection({ auth: 'none', ready: false }));
    mock.api.getAntigravityAuth.mockResolvedValue(missingAdcAuth);
    mock.api.saveAntigravityAuth.mockResolvedValue({ ...missingAdcAuth, restart: { ok: true } });

    render(
      wrap(
        <BackendConnectionDialog
          backend="antigravity"
          method="api_key"
          onClose={onClose}
          onConnected={onConnected}
          onWriteState={vi.fn()}
        />,
      ),
    );

    const platformRadio = await screen.findByRole('radio', { name: en.settings.backends.antigravityMethodAgentPlatformCompact });
    await waitFor(() => expect((platformRadio as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(platformRadio);

    fireEvent.change(screen.getByLabelText(en.settings.backends.antigravityGcpProjectLabel), {
      target: { value: 'prod-agent-proj' },
    });
    fireEvent.change(screen.getByLabelText(en.settings.backends.antigravityGcpLocationLabel), {
      target: { value: 'us-central1' },
    });
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.saveConnect }));

    await waitFor(() =>
      expect(mock.api.saveAntigravityAuth).toHaveBeenCalledWith({
        auth_mode: 'agent-platform',
        gcp_project: 'prod-agent-proj',
        gcp_location: 'us-central1',
      }),
    );

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain(en.settings.backends.antigravityAdcSaveNeedsAuth);
    expect(screen.getByText('gcloud auth application-default login')).toBeTruthy();
    expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull();
    expect(onConnected).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    // Once ADC is set up on the host, refreshing status confirms auth and completes the dialog.
    mock.api.getBackendConnection.mockResolvedValue(connection({ auth: 'external', ready: true }));
    mock.api.getAntigravityAuth.mockResolvedValue(
      authState({
        auth_mode: 'agent-platform',
        authenticated: true,
        credentials_present: true,
        adc_present: true,
        gcp_project: 'prod-agent-proj',
        gcp_location: 'us-central1',
        adc_command: 'gcloud auth application-default login',
        error: null,
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.refresh }));

    await waitFor(() => expect(onConnected).toHaveBeenCalledOnce());
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  });

  it('completes BackendConnectionDialog when Agent Platform save confirms authenticated ADC', async () => {
    const onClose = vi.fn();
    const onConnected = vi.fn(async () => {});
    const readyAuth = authState({
      auth_mode: 'agent-platform',
      authenticated: true,
      credentials_present: true,
      adc_present: true,
      gcp_project: 'ready-proj',
      gcp_location: 'global',
      error: null,
    });
    mock.api.getBackendConnection.mockResolvedValue(connection({ auth: 'external', ready: true }));
    mock.api.getAntigravityAuth
      .mockResolvedValueOnce(authState({ adc_present: true }))
      .mockResolvedValue(readyAuth);
    mock.api.saveAntigravityAuth.mockResolvedValue({ ...readyAuth, restart: { ok: true } });

    render(
      wrap(
        <BackendConnectionDialog
          backend="antigravity"
          method="api_key"
          onClose={onClose}
          onConnected={onConnected}
          onWriteState={vi.fn()}
        />,
      ),
    );

    const platformRadio = await screen.findByRole('radio', { name: en.settings.backends.antigravityMethodAgentPlatformCompact });
    await waitFor(() => expect((platformRadio as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(platformRadio);

    fireEvent.change(screen.getByLabelText(en.settings.backends.antigravityGcpProjectLabel), {
      target: { value: 'ready-proj' },
    });
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.saveConnect }));

    await waitFor(() => expect(onConnected).toHaveBeenCalledOnce());
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  });

  it('does not complete BackendConnectionDialog when Gemini API key save returns unauthenticated', async () => {
    const onClose = vi.fn();
    const onConnected = vi.fn(async () => {});
    const unverifiedKeyAuth = authState({
      auth_mode: 'gemini-api-key',
      authenticated: false,
      credentials_present: true,
      has_api_key: true,
      api_key_masked: 'AIza…badk',
      error: 'invalid_credentials',
    });
    mock.api.getBackendConnection.mockResolvedValue(connection({ auth: 'none', ready: true }));
    mock.api.getAntigravityAuth
      .mockResolvedValueOnce(authState())
      .mockResolvedValue(unverifiedKeyAuth);
    mock.api.saveAntigravityAuth.mockResolvedValue({ ...unverifiedKeyAuth, restart: { ok: true } });

    render(
      wrap(
        <BackendConnectionDialog
          backend="antigravity"
          method="api_key"
          onClose={onClose}
          onConnected={onConnected}
          onWriteState={vi.fn()}
        />,
      ),
    );

    const keyInput = await screen.findByLabelText(en.settings.backends.antigravityApiKeyLabel);
    fireEvent.change(keyInput, { target: { value: 'AIzaSyInvalidKey1234' } });
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.saveConnect }));

    await waitFor(() =>
      expect(mock.api.saveAntigravityAuth).toHaveBeenCalledWith({
        auth_mode: 'gemini-api-key',
        api_key: 'AIzaSyInvalidKey1234',
        base_url: null,
      }),
    );

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain(en.settings.backends.antigravityApiKeyUnconfirmed);
    expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull();
    expect(onConnected).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('does not complete BackendConnectionDialog when OAuth finishes without confirmed authentication', async () => {
    const onClose = vi.fn();
    const onConnected = vi.fn(async () => {});
    mock.api.getBackendConnection.mockResolvedValue(connection({ auth: 'none', ready: false }));
    mock.api.getAntigravityAuth.mockResolvedValue(
      authState({
        auth_mode: 'oauth-personal',
        authenticated: false,
        credentials_present: false,
        error: 'not_logged_in',
      }),
    );
    mock.api.startOAuthWeb.mockResolvedValue({
      ok: true,
      flow_id: 'flow_unconfirmed',
      backend: 'antigravity',
      state: 'success',
    });

    render(
      wrap(
        <BackendConnectionDialog
          backend="antigravity"
          method="oauth"
          onClose={onClose}
          onConnected={onConnected}
          onWriteState={vi.fn()}
        />,
      ),
    );

    const signInButton = await screen.findByRole('button', { name: en.settings.backends.antigravitySignInGoogle });
    fireEvent.click(signInButton);

    const alerts = await screen.findAllByRole('alert');
    expect(alerts).toHaveLength(1);
    expect(alerts[0].textContent).toContain(en.onboarding.connection.unconfirmed);
    expect(onConnected).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
});
