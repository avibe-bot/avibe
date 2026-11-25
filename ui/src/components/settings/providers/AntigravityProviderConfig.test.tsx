// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { BackendConnectionDialog } from '@/components/onboarding/BackendConnectionDialog';
import type { AntigravityAuthState, BackendConnectionState } from '@/context/ApiContext';
import { BackendTestPanel } from '../BackendTestPanel';
import { modelsApi } from '../models/modelsApi';
import { AntigravityProviderConfig } from './AntigravityProviderConfig';

const mock = vi.hoisted(() => ({
  toast: vi.fn(),
  api: {
    getConfig: vi.fn(), detectCli: vi.fn(), mutateConfig: vi.fn(), getBackendRuntime: vi.fn(),
    restartBackend: vi.fn(), getBackendConnection: vi.fn(), getAntigravityAuth: vi.fn(),
    saveAntigravityAuth: vi.fn(), antigravityModels: vi.fn(), startOAuthWeb: vi.fn(),
    getOAuthWebStatus: vi.fn(), submitOAuthWebCode: vi.fn(), cancelOAuthWeb: vi.fn(),
    removeBackendAuth: vi.fn(), removeBackendApiKey: vi.fn(), testBackendAuth: vi.fn(),
  },
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => mock.api }));
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: mock.toast }) }));
vi.mock('../models/useModelHubCapability', () => ({ useModelHubCapability: () => true }));

const i18n = createInstance();
await i18n.init({ lng: 'en', fallbackLng: false, resources: { en: { translation: en }, zh: { translation: zh } } });

const B = en.settings.backends;
const C = en.onboarding.connection;
const ADC_CMD = 'gcloud auth application-default login';

const wrap = (node: React.ReactNode) => <MemoryRouter><I18nextProvider i18n={i18n}>{node}</I18nextProvider></MemoryRouter>;
const connection = (patch: Partial<BackendConnectionState> = {}): BackendConnectionState => ({
  ok: true, backend: 'antigravity', installed: true, cli_status: 'ok', enabled: true,
  auth: 'none', application: 'applied', ready: true, entry_eligible: true, supply_mode: 'direct', ...patch,
});
const authState = (patch: Partial<AntigravityAuthState> = {}): AntigravityAuthState => ({
  ok: true, auth_mode: 'oauth-personal', authenticated: false, credentials_present: false,
  catalog_available: true, cli_installed: true, cli_version: '1.5.16', has_api_key: false,
  api_key_masked: null, base_url: null, gcp_project: null, gcp_location: null, adc_present: false, adc_command: ADC_CMD, ...patch,
});
const mockAuth = (saved: Partial<AntigravityAuthState>, initial?: Partial<AntigravityAuthState>) => {
  const next = authState(saved);
  mock.api.getAntigravityAuth.mockReset();
  if (initial) mock.api.getAntigravityAuth.mockResolvedValueOnce(authState(initial));
  mock.api.getAntigravityAuth.mockResolvedValue(next);
  mock.api.saveAntigravityAuth.mockResolvedValue({ ...next, restart: { ok: true } });
};
const renderProvider = (patch?: Partial<AntigravityAuthState>) => {
  if (patch) mock.api.getAntigravityAuth.mockResolvedValue(authState(patch));
  return render(wrap(<AntigravityProviderConfig />));
};
const renderDialog = (method: 'oauth' | 'api_key' = 'api_key') => {
  const onClose = vi.fn();
  const onConnected = vi.fn(async () => {});
  render(wrap(<BackendConnectionDialog backend="antigravity" method={method} onClose={onClose} onConnected={onConnected} onWriteState={vi.fn()} />));
  return {
    expectStaysOpen: async (expectedAlert: string) => {
      const alerts = await screen.findAllByRole('alert');
      expect(alerts).toHaveLength(1);
      expect(alerts[0].textContent).toContain(expectedAlert);
      expect(screen.queryByText(C.connected)).toBeNull();
      expect(onConnected).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
    },
    expectClosed: async () => {
      await waitFor(() => expect(onConnected).toHaveBeenCalledOnce());
      await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    },
  };
};
const setField = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const selectAgentPlatformCompact = async () => {
  const radio = await screen.findByRole('radio', { name: B.antigravityMethodAgentPlatformCompact });
  await waitFor(() => expect((radio as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(radio);
};

beforeEach(async () => {
  await i18n.changeLanguage('en');
  vi.resetAllMocks();
  mock.api.getConfig.mockResolvedValue({ agents: { antigravity: { enabled: true, cli_path: 'agy' } } });
  mock.api.detectCli.mockResolvedValue({ found: true, status: 'ok', path: '/usr/local/bin/agy' });
  mock.api.getBackendRuntime.mockResolvedValue({
    ok: true, name: 'antigravity', cli_path: 'agy', resolved_path: '/usr/local/bin/agy',
    status: 'ok', enabled: true, installed: true, current_version: '1.5.16', process_status: 'running',
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

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('Antigravity settings, auth flows, and onboarding', () => {
  it.each(['en', 'zh'] as const)('completes %s Google paste-code OAuth without personal automation notice or Model Hub card', async (lang) => {
    await i18n.changeLanguage(lang);
    const t = lang === 'en' ? en : zh;
    mock.api.startOAuthWeb.mockResolvedValue({
      ok: true, flow_id: 'flow_agy', backend: 'antigravity', state: 'awaiting_code',
      callback_kind: 'code', url: 'https://accounts.google.com/o/oauth2/v2/auth?code=1',
    });
    mock.api.submitOAuthWebCode.mockResolvedValue({ ok: true });
    renderProvider();

    const signInButton = await screen.findByRole('button', { name: t.settings.backends.antigravitySignInGoogle });
    expect(screen.queryByRole('note')).toBeNull();
    expect(screen.queryByText(t.settings.models.supplyMode.title)).toBeNull();
    expect(modelsApi.listAgents).not.toHaveBeenCalled();
    expect(mock.api.testBackendAuth).not.toHaveBeenCalled();

    fireEvent.click(signInButton);
    await waitFor(() => expect(mock.api.startOAuthWeb).toHaveBeenCalledWith('antigravity', false, 'oauth-personal'));
    await screen.findByRole('link', { name: t.settings.models.oauth.openSignIn });
    fireEvent.change(screen.getByPlaceholderText(t.settings.backends.antigravityCallbackCodePlaceholder), { target: { value: 'paste-code-123' } });
    fireEvent.click(screen.getByRole('button', { name: t.common.submit }));
    await waitFor(() => expect(mock.api.submitOAuthWebCode).toHaveBeenCalledWith('antigravity', 'flow_agy', 'paste-code-123'));
  });

  it('shows Gemini Enterprise terminal block only when OAuth returns terminal guidance, and handles Gemini API key and Agent Platform ADC visibility', async () => {
    mock.api.startOAuthWeb.mockResolvedValue({
      ok: true, flow_id: 'flow_enterprise', backend: 'antigravity', state: 'awaiting_code',
      terminal_command: 'agy auth login --enterprise',
      instructions: 'Run this command in a terminal to complete Gemini Enterprise sign-in.',
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderProvider({
      auth_mode: 'gemini-api-key', authenticated: true, credentials_present: true, has_api_key: true,
      api_key_masked: 'AIza…9xyz', base_url: 'https://generativelanguage.googleapis.com', gcp_project: 'demo-proj', gcp_location: 'global', adc_present: false,
    });

    expect(await screen.findByText('AIza…9xyz')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: B.antigravityApiKeyRemove }));
    await waitFor(() => expect(mock.api.removeBackendApiKey).toHaveBeenCalledWith('antigravity'));

    fireEvent.click(screen.getByRole('radio', { name: B.antigravityMethodEnterprise }));
    expect(screen.queryByText(B.antigravityEnterpriseTerminalTitle)).toBeNull();
    expect(screen.queryByText('agy')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: B.antigravitySignInEnterprise }));
    await waitFor(() => expect(mock.api.startOAuthWeb).toHaveBeenCalledWith('antigravity', false, 'oauth-business'));
    expect(await screen.findByText(B.antigravityEnterpriseTerminalTitle)).toBeTruthy();
    expect(screen.getByText('agy auth login --enterprise')).toBeTruthy();
    expect(screen.getByText('Run this command in a terminal to complete Gemini Enterprise sign-in.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: en.common.cancel }));
    await waitFor(() => expect(screen.queryByText('agy auth login --enterprise')).toBeNull());

    fireEvent.click(screen.getByRole('radio', { name: B.antigravityMethodAgentPlatform }));
    expect(screen.getByText(ADC_CMD)).toBeTruthy();
    setField(B.antigravityGcpProjectLabel, 'prod-agent-proj');
    setField(B.antigravityGcpLocationLabel, 'us-central1');
    fireEvent.click(screen.getByRole('button', { name: en.common.save }));
    await waitFor(() => expect(mock.api.saveAntigravityAuth).toHaveBeenCalledWith({
      auth_mode: 'agent-platform', gcp_project: 'prod-agent-proj', gcp_location: 'us-central1',
    }));
  });

  it('hides the ADC terminal command on Agent Platform when ADC is already detected', async () => {
    renderProvider({ auth_mode: 'agent-platform', authenticated: true, credentials_present: true, gcp_project: 'prod-agent-proj', gcp_location: 'global', adc_present: true });
    expect(await screen.findByText(B.antigravityAdcPresent)).toBeTruthy();
    expect(screen.queryByText(B.antigravityAdcCommandHint)).toBeNull();
    expect(screen.queryByText(ADC_CMD)).toBeNull();
  });

  it('pre-selects an Avibe-owned model in BackendTestPanel and shows structured auth/catalog/inference results', async () => {
    mock.api.testBackendAuth.mockResolvedValue({ ok: true, duration_ms: 210, excerpt: 'Hi there!', authenticated: true, catalog_available: true, inference_verified: true });
    render(wrap(<BackendTestPanel backend="antigravity" />));
    const select = (await screen.findByLabelText(B.testConnectionModelLabel)) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe('gemini-4-neon'));
    expect(screen.queryByRole('option', { name: B.testConnectionModelDefault })).toBeNull();
    expect(mock.api.testBackendAuth).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: B.testConnectionRun }));
    await waitFor(() => expect(mock.api.testBackendAuth).toHaveBeenCalledWith('antigravity', { model: 'gemini-4-neon' }));
    expect(await screen.findByText(B.testConnectionAuthOk)).toBeTruthy();
    expect(screen.getByText(B.testConnectionCatalogOk)).toBeTruthy();
    expect(screen.getByText(B.testConnectionInferenceOk)).toBeTruthy();
  });

  it('opens onboarding BackendConnectionDialog for Antigravity without personal automation notice or default Enterprise terminal block', async () => {
    renderDialog('oauth');
    expect(await screen.findByText(C.antigravityDialogIntro)).toBeTruthy();
    expect(screen.queryByRole('note')).toBeNull();
    fireEvent.click(screen.getByRole('radio', { name: B.antigravityMethodEnterpriseCompact }));
    expect(screen.queryByText(B.antigravityEnterpriseTerminalTitle)).toBeNull();
  });

  it('completes BackendConnectionDialog when Agent Platform save confirms authenticated ADC', async () => {
    mock.api.getBackendConnection.mockResolvedValue(connection({ auth: 'external', ready: true }));
    mockAuth(
      { auth_mode: 'agent-platform', authenticated: true, credentials_present: true, adc_present: true, gcp_project: 'ready-proj', gcp_location: 'global', error: null },
      { adc_present: true },
    );
    const dialog = renderDialog('api_key');
    await selectAgentPlatformCompact();
    setField(B.antigravityGcpProjectLabel, 'ready-proj');
    fireEvent.click(screen.getByRole('button', { name: C.saveConnect }));
    await dialog.expectClosed();
  });

  it.each([
    [
      'Agent Platform saves without ADC (and connects once refresh confirms ADC)', 'api_key' as const,
      () => {
        mock.api.getBackendConnection.mockResolvedValue(connection({ auth: 'none', ready: false }));
        mockAuth({ auth_mode: 'agent-platform', authenticated: false, adc_present: false, gcp_project: 'prod-agent-proj', gcp_location: 'us-central1', error: 'adc_missing' });
      },
      async () => {
        await selectAgentPlatformCompact();
        setField(B.antigravityGcpProjectLabel, 'prod-agent-proj');
        setField(B.antigravityGcpLocationLabel, 'us-central1');
        fireEvent.click(screen.getByRole('button', { name: C.saveConnect }));
      },
      { auth_mode: 'agent-platform', gcp_project: 'prod-agent-proj', gcp_location: 'us-central1' },
      B.antigravityAdcSaveNeedsAuth,
      async (dialog: ReturnType<typeof renderDialog>) => {
        expect(screen.getByText(ADC_CMD)).toBeTruthy();
        mock.api.getBackendConnection.mockResolvedValue(connection({ auth: 'external', ready: true }));
        mockAuth({ auth_mode: 'agent-platform', authenticated: true, credentials_present: true, adc_present: true, gcp_project: 'prod-agent-proj', gcp_location: 'us-central1', error: null });
        fireEvent.click(screen.getByRole('button', { name: C.refresh }));
        await dialog.expectClosed();
      },
    ],
    [
      'Gemini API key save returns unauthenticated', 'api_key' as const,
      () => {
        mock.api.getBackendConnection.mockResolvedValue(connection({ auth: 'none', ready: true }));
        mockAuth({ auth_mode: 'gemini-api-key', authenticated: false, credentials_present: true, has_api_key: true, api_key_masked: 'AIza…badk', error: 'invalid_credentials' }, {});
      },
      async () => {
        fireEvent.change(await screen.findByLabelText(B.antigravityApiKeyLabel), { target: { value: 'AIzaSyInvalidKey1234' } });
        fireEvent.click(screen.getByRole('button', { name: C.saveConnect }));
      },
      { auth_mode: 'gemini-api-key', api_key: 'AIzaSyInvalidKey1234', base_url: null }, B.antigravityApiKeyUnconfirmed, null,
    ],
    [
      'OAuth finishes without confirmed authentication', 'oauth' as const,
      () => {
        mock.api.getBackendConnection.mockResolvedValue(connection({ auth: 'none', ready: false }));
        mock.api.getAntigravityAuth.mockResolvedValue(authState({ auth_mode: 'oauth-personal', error: 'not_logged_in' }));
        mock.api.startOAuthWeb.mockResolvedValue({ ok: true, flow_id: 'flow_unconfirmed', backend: 'antigravity', state: 'success' });
      },
      async () => { fireEvent.click(await screen.findByRole('button', { name: B.antigravitySignInGoogle })); },
      null, C.unconfirmed, null,
    ],
  ])('keeps BackendConnectionDialog open when %s', async (_label, method, setup, act, expectedSave, expectedAlert, verifyAfter) => {
    setup();
    const dialog = renderDialog(method);
    await act();
    if (expectedSave) await waitFor(() => expect(mock.api.saveAntigravityAuth).toHaveBeenCalledWith(expectedSave));
    await dialog.expectStaysOpen(expectedAlert);
    if (verifyAfter) await verifyAfter(dialog);
  });
});
