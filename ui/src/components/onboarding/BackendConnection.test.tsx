// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ClaudeProviderConfig } from '../settings/providers/ClaudeProviderConfig';
import { OpencodeProviderConfig } from '../settings/providers/OpencodeProviderConfig';
import { CodexProviderConfig } from '../settings/providers/CodexProviderConfig';
import { BackendConnectionForm } from '../settings/providers/BackendConnectionForm';
import { BackendConnectionDialog } from './BackendConnectionDialog';
import en from '../../i18n/en.json';
import type { BackendConnectionState, ClaudeAuthState, CodexAuthState } from '../../context/ApiContext';

const mock = vi.hoisted(() => ({ toast: vi.fn(), api: {
  getConfig: vi.fn(), detectCli: vi.fn(), mutateConfig: vi.fn(), getBackendRuntime: vi.fn(), restartBackend: vi.fn(), claudeModels: vi.fn(), codexModels: vi.fn(),
  getClaudeAuth: vi.fn(), getCodexAuth: vi.fn(), getOpencodeProviders: vi.fn(),
  setOpencodeProviderAuth: vi.fn(), getBackendConnection: vi.fn(), saveClaudeAuth: vi.fn(), saveCodexAuth: vi.fn(),
  startOAuthWeb: vi.fn(), startOAuthWebForOpencodeProvider: vi.fn(),
  getOAuthWebStatus: vi.fn(), submitOAuthWebCode: vi.fn(), cancelOAuthWeb: vi.fn(),
  removeClaudeOAuthCredentials: vi.fn(), removeBackendAuth: vi.fn(), removeBackendApiKey: vi.fn(), deleteOpencodeProviderAuth: vi.fn(),
} }));
vi.mock('../../context/ApiContext', () => ({ useApi: () => mock.api }));
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: mock.toast }) }));
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
const wrap = (node: React.ReactNode) => <I18nextProvider i18n={i18n}>{node}</I18nextProvider>;
const connection = (patch: Partial<BackendConnectionState> = {}): BackendConnectionState => ({ ok: true, backend: 'claude', installed: true, enabled: true, auth: 'api_key', application: 'applied', ready: true, entry_eligible: true, ...patch });
const native = (): ClaudeAuthState => ({ ok: true, auth_mode: 'api_key', active_auth_mode: 'api_key', has_api_key: true, api_key_length: 20, api_key_masked: 'sk-•••old', base_url: 'https://old.example', credential_type: 'api_key', has_oauth_credentials: true, settings_path: '/fixture/claude/settings.json', settings_exists: true, settings_env_has_key: true, settings_env_key_length: 20, settings_env_key_var: 'ANTHROPIC_API_KEY', settings_env_base_url: 'https://old.example', settings_conflict: false });
const codexNative = (patch: Partial<CodexAuthState> = {}): CodexAuthState => ({ ok: true, auth_mode: 'api_key', active_auth_mode: 'api_key', has_api_key: true, api_key_length: 20, api_key_masked: 'sk-•••old', base_url: 'https://old.example', has_chatgpt_tokens: true, credentials_store: 'file', file_store_active: true, ...patch });
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; };
beforeEach(() => {
  vi.resetAllMocks();
  mock.api.getClaudeAuth.mockResolvedValue(native());
  mock.api.getCodexAuth.mockResolvedValue(codexNative());
  mock.api.getBackendConnection.mockImplementation(async (backend) => connection({ backend }));
  mock.api.saveClaudeAuth.mockResolvedValue({ ok: true, restart: { ok: true } });
  mock.api.cancelOAuthWeb.mockResolvedValue({ ok: true });
  mock.api.getConfig.mockResolvedValue({ agents: { claude: { enabled: false, cli_path: 'claude' }, codex: { enabled: false, cli_path: 'codex' } } });
  mock.api.detectCli.mockImplementation(async (binary) => ({ found: true, path: binary }));
  mock.api.getBackendRuntime.mockImplementation(async (backend) => ({ ok: true, name: backend, enabled: true, installed: true, process_status: 'running' }));
  mock.api.claudeModels.mockResolvedValue({ ok: true, models: [] });
  mock.api.codexModels.mockResolvedValue({ ok: true, models: [] });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe('shared Settings and onboarding connection owner', () => {
  it.each(['claude', 'codex', 'opencode'] as const)('saves disabled %s credentials and keeps saved-not-connected after reopen', async (backend) => {
    const provider = { id: 'fixture', name: 'Fixture', description: '', configured: true, oauth_available: false, local: false, models: [], active_auth_type: 'api', api_key_masked: 'sk-•••old' };
    mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [provider] });
    mock.api.saveCodexAuth.mockResolvedValue({ ok: true, restart: { ok: true } });
    mock.api.setOpencodeProviderAuth.mockResolvedValue({ ok: true, restart: { ok: true } });
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend, enabled: false, ready: false, entry_eligible: false }));
    const refreshed = vi.fn();
    const props = { backend, provider: backend === 'opencode' ? provider : undefined, initialMethod: 'api_key' as const, onConnected: refreshed };
    const first = render(wrap(<BackendConnectionForm {...props} />));
    await screen.findByText(en.onboarding.connection.savedDisabled);
    fireEvent.click(screen.getByRole('button', { name: en.common.save }));
    await waitFor(() => expect(refreshed).toHaveBeenCalledOnce());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull();
    first.unmount(); render(wrap(<BackendConnectionForm {...props} />));
    await screen.findByText(en.onboarding.connection.savedDisabled);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull();
    expect(refreshed).toHaveBeenCalledOnce();
  });
  it.each(['unknown', 'failed', 'draining'] as const)('disabled backend does not mask %s application', async (application) => {
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'codex', enabled: false, ready: false, entry_eligible: false, application }));
    mock.api.saveCodexAuth.mockResolvedValue({ ok: true, restart: { ok: true } });
    const refreshed = vi.fn();
    render(wrap(<BackendConnectionForm backend="codex" initialMethod="api_key" onConnected={refreshed} />));
    fireEvent.click(await screen.findByRole('button', { name: en.common.save }));
    await screen.findByRole('alert');
    expect(screen.queryByText(en.onboarding.connection.savedDisabled)).toBeNull();
    expect(refreshed).not.toHaveBeenCalled();
  });
  it('disabled backend preserves explicit failed receipt and uncertain auth', async () => {
    mock.api.getCodexAuth.mockResolvedValue(codexNative({ auth_mode_uncertain: true }));
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'codex', enabled: false, auth: 'unknown', ready: false, entry_eligible: false }));
    mock.api.saveCodexAuth.mockResolvedValue({ ok: true, restart: { ok: false, message: 'apply failed' } });
    const refreshed = vi.fn();
    render(wrap(<BackendConnectionForm backend="codex" initialMethod="api_key" onConnected={refreshed} />));
    fireEvent.click(await screen.findByRole('button', { name: en.common.save }));
    await screen.findByText(/apply failed/);
    expect(screen.queryByText(en.onboarding.connection.savedDisabled)).toBeNull();
    expect(refreshed).not.toHaveBeenCalled();
  });
  for (const backend of ['claude', 'codex'] as const) {
    it.each(['applied', 'draining', 'failed', 'unreadable'] as const)(`${backend} Settings refreshes after toggle settlement (%s), preserving unsaved drafts`, async (outcome) => {
      const mutation = deferred<{ agent_backend_runtime: { hot_reconciled: boolean; restart_error?: string } }>();
      mock.api.mutateConfig.mockReturnValue(mutation.promise);
      mock.api.saveCodexAuth.mockResolvedValue({ ok: true, restart: { ok: true } });
      mock.api.getBackendConnection.mockResolvedValue(connection({ backend, enabled: false, ready: false, entry_eligible: false }));
      render(wrap(backend === 'claude' ? <ClaudeProviderConfig /> : <CodexProviderConfig />));
      await screen.findByText(en.onboarding.connection.savedDisabled);
      fireEvent.click(screen.getByRole('button', { name: en.common.save }));
      await waitFor(() => expect(mock.api.getBackendConnection).toHaveBeenCalledTimes(2));
      await screen.findByText(en.onboarding.connection.savedDisabled);
      expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
      const keyInput = document.getElementById(`${backend}-connection-key`) as HTMLInputElement;
      fireEvent.change(keyInput, { target: { value: 'unsaved-fixture-key' } });
      fireEvent.change(screen.getByLabelText(en.onboarding.connection.baseUrl), { target: { value: 'https://unsaved.invalid' } });
      const toggle = screen.getByRole('switch');
      expect(toggle.getAttribute('aria-checked')).toBe('false');
      fireEvent.click(toggle);
      await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledWith([{ kind: 'set', path: ['agents', backend, 'enabled'], value: true }]));
      expect(mock.api.getBackendConnection).toHaveBeenCalledTimes(2); // optimistic enabled is not settlement
      if (outcome === 'unreadable') mock.api.getBackendConnection.mockRejectedValue(new Error('fixture IPC unavailable'));
      else mock.api.getBackendConnection.mockResolvedValue(connection({ backend, application: outcome, ready: outcome === 'applied', entry_eligible: outcome === 'applied', message: outcome === 'failed' ? 'fixture apply failed' : undefined }));
      await act(async () => mutation.resolve({ agent_backend_runtime: { hot_reconciled: outcome !== 'failed', restart_error: outcome === 'failed' ? 'fixture apply failed' : undefined } }));
      await waitFor(() => expect(mock.api.getBackendConnection).toHaveBeenCalledTimes(3));
      await waitFor(() => expect(screen.queryByText(en.onboarding.connection.savedDisabled)).toBeNull());
      if (outcome === 'applied') await screen.findByText(en.onboarding.connection.connected);
      else { await screen.findByRole('alert'); expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull(); }
      expect(keyInput.value).toBe('unsaved-fixture-key');
      expect((screen.getByLabelText(en.onboarding.connection.baseUrl) as HTMLInputElement).value).toBe('https://unsaved.invalid');
      expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true');
    });
  }
  const providerFixture = () => ({ id: 'fixture', name: 'Fixture', description: '', configured: true, oauth_available: false, local: false, models: ['model'], active_auth_type: 'api', api_key_masked: 'sk-•••old' });
  for (const backend of ['claude', 'codex', 'opencode'] as const) {
    it.each(['failed', 'unreadable'] as const)(`${backend} committed removal publishes auth despite %s application; Retry does not mutate`, async (outcome) => {
      vi.spyOn(window, 'confirm').mockReturnValue(true);
      const provider = providerFixture();
      mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [provider] });
      render(wrap(<BackendConnectionForm backend={backend} provider={backend === 'opencode' ? provider : undefined} initialMethod="api_key" />));
      await screen.findByText('sk-•••old');
      const removedNative = { ...native(), active_auth_mode: 'oauth', has_api_key: false, api_key_masked: null, base_url: null };
      mock.api.getClaudeAuth.mockResolvedValue(removedNative); mock.api.getCodexAuth.mockResolvedValue(codexNative({ active_auth_mode: 'oauth', has_api_key: false, api_key_masked: null, base_url: null }));
      mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [{ ...provider, configured: false, active_auth_type: null, api_key_masked: null }] });
      mock.api.removeBackendApiKey.mockResolvedValue({ ok: true, restart: { ok: false, message: 'fixture refresh failed' } });
      mock.api.deleteOpencodeProviderAuth.mockResolvedValue({ ok: true, restart: { ok: false, message: 'fixture refresh failed' } });
      if (outcome === 'unreadable') mock.api.getBackendConnection.mockRejectedValue(new Error('fixture IPC unreadable'));
      else mock.api.getBackendConnection.mockResolvedValue(connection({ backend, application: 'failed', ready: false, entry_eligible: false }));
      fireEvent.click(screen.getByRole('button', { name: en.settings.backends.claudeApiKeyRemove }));
      await waitFor(() => expect(screen.queryByText('sk-•••old')).toBeNull());
      expect(screen.queryByRole('button', { name: en.settings.backends.claudeApiKeyRemove })).toBeNull();
      expect((await screen.findByRole('alert')).textContent).toContain('fixture refresh failed');
      expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull();
      mock.api.getBackendConnection.mockResolvedValue(connection({ backend, auth: backend === 'opencode' ? 'none' : 'subscription', ready: backend !== 'opencode', entry_eligible: backend !== 'opencode' }));
      fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.refresh }));
      await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
      expect(backend === 'opencode' ? mock.api.deleteOpencodeProviderAuth : mock.api.removeBackendApiKey).toHaveBeenCalledOnce();
      expect(mock.api.saveClaudeAuth).not.toHaveBeenCalled(); expect(mock.api.saveCodexAuth).not.toHaveBeenCalled(); expect(mock.api.setOpencodeProviderAuth).not.toHaveBeenCalled();
    });
    it(`${backend} committed save publishes new mask while preserving replacement draft on failed apply`, async () => {
      const provider = providerFixture();
      mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [provider] });
      const done = vi.fn();
      render(wrap(<BackendConnectionForm backend={backend} provider={backend === 'opencode' ? provider : undefined} compact initialMethod="api_key" onConnected={done} />));
      fireEvent.click(await screen.findByRole('button', { name: 'Replace' }));
      const input = document.getElementById(`${backend}-connection-key`) as HTMLInputElement;
      fireEvent.change(input, { target: { value: 'fixture-new-key' } });
      mock.api.getClaudeAuth.mockResolvedValue({ ...native(), api_key_masked: 'new-mask' });
      mock.api.getCodexAuth.mockResolvedValue(codexNative({ api_key_masked: 'new-mask' }));
      mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [{ ...provider, api_key_masked: 'new-mask' }] });
      for (const save of [mock.api.saveClaudeAuth, mock.api.saveCodexAuth, mock.api.setOpencodeProviderAuth]) save.mockResolvedValue({ ok: true, restart: { ok: false, message: 'fixture apply receipt' } });
      mock.api.getBackendConnection.mockRejectedValue(new Error('fixture IPC'));
      fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.saveConnect }));
      expect((await screen.findByRole('alert')).textContent).toContain('fixture apply receipt');
      expect(input.value).toBe('fixture-new-key'); expect(done).not.toHaveBeenCalled();
      fireEvent.click(screen.getAllByRole('button', { name: 'Cancel' })[0]);
      await screen.findByText('new-mask');
      expect(screen.queryByText('sk-•••old')).toBeNull();
    });
  }
  it('an unreadable post-removal native store is unknown rather than the old confirmed mask', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(wrap(<BackendConnectionForm backend="codex" initialMethod="api_key" />)); await screen.findByText('sk-•••old');
    mock.api.removeBackendApiKey.mockResolvedValue({ ok: true, restart: { ok: false, message: 'apply failed' } });
    mock.api.getCodexAuth.mockRejectedValue(new Error('native read unavailable'));
    fireEvent.click(screen.getByRole('button', { name: en.settings.backends.claudeApiKeyRemove }));
    expect((await screen.findByRole('alert')).textContent).toContain('native read unavailable');
    expect(screen.queryByText('sk-•••old')).toBeNull(); expect(screen.queryByRole('button', { name: en.settings.backends.claudeApiKeyRemove })).toBeNull();
    expect(screen.getByRole('button', { name: en.common.save }).hasAttribute('disabled')).toBe(true);
  });
  it.each(['claude', 'codex'] as const)('%s partial signout reads native state and does not toast unconditional success', async (backend) => {
    mock.api.getClaudeAuth.mockResolvedValue({ ...native(), active_auth_mode: 'oauth', has_api_key: false });
    mock.api.getCodexAuth.mockResolvedValue(codexNative({ active_auth_mode: 'oauth', has_api_key: false }));
    render(wrap(<BackendConnectionForm backend={backend} />));
    const button = await screen.findByRole('button', { name: en.settings.backends.oauthRemove });
    mock.api.removeBackendAuth.mockResolvedValue({ ok: true, partial: true, detail: 'fixture partial logout', restart: { ok: false, message: 'fixture signout apply' } });
    mock.api.getClaudeAuth.mockResolvedValue({ ...native(), active_auth_mode: 'none', has_api_key: false, has_oauth_credentials: false });
    mock.api.getCodexAuth.mockResolvedValue(codexNative({ active_auth_mode: 'none', has_api_key: false, has_chatgpt_tokens: false }));
    fireEvent.click(button);
    await waitFor(() => expect(screen.queryByRole('button', { name: en.settings.backends.oauthRemove })).toBeNull());
    expect((await screen.findByRole('alert')).textContent).toContain('fixture signout apply');
    expect(mock.toast.mock.calls.some(([, kind]) => kind === 'success')).toBe(false);
    expect(mock.toast.mock.calls.some(([, kind]) => kind === 'warning')).toBe(true);
  });
  it.each(['applied', 'draining', 'failed', 'unreadable'] as const)('expanded OpenCode parent consumes runtime CLI settlement (%s)', async (outcome) => {
    mock.api.getConfig.mockResolvedValue({ agents: { opencode: { enabled: true, cli_path: 'opencode' } } });
    const provider = providerFixture();
    mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [provider], permission_allowed: true });
    render(wrap(<OpencodeProviderConfig />));
    fireEvent.click(await screen.findByText('Fixture', { exact: true }));
    await screen.findByText(en.onboarding.connection.connected);
    const form = within(document.querySelector('.backend-connection-form') as HTMLElement);
    fireEvent.change(form.getByLabelText(en.onboarding.connection.baseUrl), { target: { value: 'https://draft.invalid' } });
    fireEvent.change(screen.getByLabelText(en.agentDetection.cliPath), { target: { value: '/fixture/new-opencode' } });
    if (outcome === 'unreadable') mock.api.getBackendConnection.mockRejectedValue(new Error('fixture IPC'));
    else mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'opencode', application: outcome, ready: outcome === 'applied', entry_eligible: outcome === 'applied' }));
    mock.api.mutateConfig.mockResolvedValue({ agents: { opencode: { enabled: true, cli_path: '/fixture/new-opencode' } }, agent_backend_runtime: { hot_reconciled: outcome !== 'failed', restart_error: 'fixture apply failed' } });
    const runtimeSave = screen.getAllByRole('button', { name: en.common.save }).find((button) => !document.querySelector('.backend-connection-form')?.contains(button))!;
    fireEvent.click(runtimeSave);
    await waitFor(() => expect(mock.api.getBackendConnection).toHaveBeenCalledTimes(2));
    expect(mock.api.mutateConfig).toHaveBeenCalledWith([{ kind: 'set', path: ['agents', 'opencode', 'cli_path'], value: '/fixture/new-opencode' }]);
    if (outcome === 'applied') await screen.findByText(en.onboarding.connection.connected);
    else { await screen.findByRole('alert'); expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull(); }
    expect((form.getByLabelText(en.onboarding.connection.baseUrl) as HTMLInputElement).value).toBe('https://draft.invalid');
    expect(runtimeSave.isConnected).toBe(false); // committed CLI is not an unsaved draft after apply failure
  });
  const parents = { claude: ClaudeProviderConfig, codex: CodexProviderConfig, opencode: OpencodeProviderConfig };
  for (const backend of Object.keys(parents) as Array<keyof typeof parents>) {
    const Parent = parents[backend];
    const mountParent = async () => {
      mock.api.getConfig.mockResolvedValue({ agents: { [backend]: { enabled: true, cli_path: backend } } });
      mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [providerFixture()], permission_allowed: true });
      render(wrap(<Parent />));
      if (backend === 'opencode') fireEvent.click(await screen.findByText('Fixture', { exact: true }));
      await screen.findByText(en.onboarding.connection.connected);
    };
    it(`${backend} Settings preserves queued on after an older off persistence rejection`, async () => {
      let rejectOff!: (error: Error) => void;
      const rejection = new Promise((_, reject) => { rejectOff = reject; });
      mock.api.mutateConfig.mockReturnValueOnce(rejection).mockResolvedValueOnce({ agents: { [backend]: { enabled: true } }, agent_backend_runtime: { hot_reconciled: true } });
      await mountParent();
      fireEvent.click(screen.getByRole('switch'));
      await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledOnce());
      fireEvent.click(screen.getByRole('switch'));
      await act(async () => rejectOff(new Error('old off rejected')));
      await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledTimes(2));
      expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true');
      expect(mock.api.mutateConfig.mock.calls.map(([ops]) => ops[0].value)).toEqual([false, true]);
      expect(mock.toast).not.toHaveBeenCalledWith('old off rejected', 'error');
    });
    it(`${backend} Settings reconciles rejected toggle from uncached persisted enabled`, async () => {
      await mountParent();
      const configReads = mock.api.getConfig.mock.calls.length;
      mock.api.mutateConfig.mockRejectedValue(new Error('write rejected'));
      fireEvent.click(screen.getByRole('switch'));
      await waitFor(() => expect(mock.toast).toHaveBeenCalledWith('write rejected', 'error'));
      await waitFor(() => expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true'));
      expect(mock.api.getConfig).toHaveBeenCalledTimes(configReads);
      expect(mock.api.getBackendConnection).toHaveBeenCalledWith(backend);
    });
    it.each(['applied', 'failed', 'unreadable'] as const)(`${backend} Settings restart settlement reads fresh auth/application (%s)`, async (outcome) => {
      mock.api.getBackendRuntime.mockResolvedValue({ ok: true, name: backend, enabled: true, installed: true, supports_restart: true, process_status: 'running' });
      await mountParent();
      const form = within(document.querySelector('.backend-connection-form') as HTMLElement);
      fireEvent.change(form.getByLabelText(en.onboarding.connection.baseUrl), { target: { value: 'https://draft.invalid' } });
      fireEvent.click(screen.getByRole('button', { name: en.backendLifecycle.statusReady }));
      const restart = await screen.findByRole('button', { name: en.backendLifecycle.restart });
      await waitFor(() => expect(restart.hasAttribute('disabled')).toBe(false));
      if (outcome === 'unreadable') mock.api.getBackendConnection.mockRejectedValue(new Error('fixture IPC'));
      else mock.api.getBackendConnection.mockResolvedValue(connection({ backend, application: outcome, ready: outcome === 'applied', entry_eligible: outcome === 'applied' }));
      mock.api.restartBackend.mockResolvedValue({ ok: outcome === 'applied', message: 'fixture restart' });
      const reads = mock.api.getBackendConnection.mock.calls.length;
      fireEvent.click(restart);
      await waitFor(() => expect(mock.api.getBackendConnection.mock.calls.length).toBe(reads + 1));
      if (outcome === 'applied') await screen.findByText(en.onboarding.connection.connected);
      else { await screen.findByRole('alert'); expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull(); }
      expect((form.getByLabelText(en.onboarding.connection.baseUrl) as HTMLInputElement).value).toBe('https://draft.invalid');
      expect(mock.api.restartBackend).toHaveBeenCalledExactlyOnceWith(backend);
      expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    });
  }
  it.each(['success', 'failed'] as const)('OAuth terminal %s observes committed auth despite failed application and Refresh never rewrites it', async (state) => {
    mock.api.getCodexAuth.mockResolvedValue(codexNative({ active_auth_mode: 'none', has_api_key: false, has_chatgpt_tokens: false }));
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'codex', auth: 'none', ready: false, entry_eligible: false }));
    const done = vi.fn();
    render(wrap(<BackendConnectionForm backend="codex" compact onConnected={done} />));
    const start = await screen.findByRole('button', { name: en.onboarding.connection.codexSignIn });
    mock.api.startOAuthWeb.mockResolvedValue({ ok: true, flow_id: 'settled', state, error: state === 'failed' ? 'fixture committed apply failure' : undefined });
    mock.api.getCodexAuth.mockResolvedValue(codexNative({ active_auth_mode: 'oauth', has_api_key: false }));
    mock.api.getBackendConnection.mockRejectedValue(new Error('fixture IPC'));
    fireEvent.click(start);
    await screen.findByText(en.onboarding.connection.savedSubscription);
    await waitFor(() => expect(screen.getAllByRole('alert').some((node) => node.textContent?.includes('fixture IPC'))).toBe(true));
    if (state === 'failed') expect(screen.getAllByRole('alert').some((node) => node.textContent?.includes('fixture committed apply failure'))).toBe(true);
    expect(done).not.toHaveBeenCalled(); expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull();
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'codex', auth: 'subscription' }));
    const refresh = screen.getByRole('button', { name: en.onboarding.connection.refresh });
    await waitFor(() => expect(refresh.hasAttribute('disabled')).toBe(false));
    fireEvent.click(refresh);
    await waitFor(() => expect(done).toHaveBeenCalledOnce());
    expect(mock.api.startOAuthWeb).toHaveBeenCalledOnce(); expect(mock.api.saveCodexAuth).not.toHaveBeenCalled();
  });
  it('cancelling Settings OAuth during confirmation discards its late native observation', async () => {
    mock.api.getCodexAuth.mockResolvedValue(codexNative({ active_auth_mode: 'none', has_api_key: false, has_chatgpt_tokens: false }));
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'codex', auth: 'none', ready: false, entry_eligible: false }));
    const done = vi.fn();
    render(wrap(<BackendConnectionForm backend="codex" onConnected={done} />));
    const start = await screen.findByRole('button', { name: en.settings.backends.codexSignInButton });
    const auth = deferred<CodexAuthState>();
    mock.api.getCodexAuth.mockReturnValue(auth.promise);
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'codex', auth: 'subscription' }));
    mock.api.startOAuthWeb.mockResolvedValue({ ok: true, flow_id: 'cancel-confirm', state: 'success' });
    fireEvent.click(start);
    await waitFor(() => expect(mock.api.getCodexAuth).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: en.common.cancel }));
    await act(async () => auth.resolve(codexNative({ active_auth_mode: 'oauth', has_api_key: false })));
    expect(done).not.toHaveBeenCalled(); expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull();
    expect(screen.queryByText(en.settings.backends.codexOauthSignedIn)).toBeNull();
    expect(mock.api.cancelOAuthWeb).toHaveBeenCalledWith('codex', 'cancel-confirm');
  });
  it('a newer runtime revision wins over an older native read without erasing drafts', async () => {
    const view = render(wrap(<BackendConnectionForm backend="codex" initialMethod="api_key" />));
    await screen.findByText('sk-•••old');
    const stale = deferred<CodexAuthState>();
    mock.api.getCodexAuth.mockReturnValueOnce(stale.promise);
    view.rerender(wrap(<BackendConnectionForm backend="codex" initialMethod="api_key" connectionRevision={1} />));
    fireEvent.change(screen.getByLabelText(en.onboarding.connection.baseUrl), { target: { value: 'https://draft.invalid' } });
    mock.api.getCodexAuth.mockResolvedValue(codexNative({ active_auth_mode: 'none', has_api_key: false, has_chatgpt_tokens: false }));
    view.rerender(wrap(<BackendConnectionForm backend="codex" initialMethod="api_key" connectionRevision={2} />));
    await waitFor(() => expect(screen.queryByText('sk-•••old')).toBeNull());
    await act(async () => stale.resolve(codexNative()));
    expect(screen.queryByText('sk-•••old')).toBeNull();
    expect((screen.getByLabelText(en.onboarding.connection.baseUrl) as HTMLInputElement).value).toBe('https://draft.invalid');
  });
  it('a late CLI detection cannot overwrite a newer Settings path draft', async () => {
    mock.api.getConfig.mockResolvedValue({ agents: { codex: { enabled: true, cli_path: 'codex' } } });
    render(wrap(<CodexProviderConfig />));
    await screen.findByText(en.onboarding.connection.connected);
    const detected = deferred<{ found: boolean; path: string }>();
    mock.api.detectCli.mockReturnValue(detected.promise);
    fireEvent.click(screen.getByRole('button', { name: en.common.detect }));
    const input = screen.getByLabelText(en.agentDetection.cliPath) as HTMLInputElement;
    fireEvent.change(input, { target: { value: '/fixture/newer-path' } });
    await act(async () => detected.resolve({ found: true, path: '/fixture/older-probe' }));
    expect(input.value).toBe('/fixture/newer-path');
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
  });
  it('a committed CLI save with failed apply keeps a newer path draft independent', async () => {
    mock.api.getConfig.mockResolvedValue({ agents: { codex: { enabled: true, cli_path: 'codex' } } });
    render(wrap(<CodexProviderConfig />));
    await screen.findByText(en.onboarding.connection.connected);
    const path = screen.getByLabelText(en.agentDetection.cliPath) as HTMLInputElement;
    fireEvent.change(path, { target: { value: '/fixture/saved-codex' } });
    const saved = deferred<{ agent_backend_runtime: { hot_reconciled: boolean; restart_error: string } }>();
    mock.api.mutateConfig.mockReturnValue(saved.promise);
    const runtimeSave = () => screen.getAllByRole('button', { name: en.common.save }).find((node) => !document.querySelector('.backend-connection-form')?.contains(node));
    fireEvent.click(runtimeSave()!);
    await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledOnce());
    fireEvent.change(path, { target: { value: '/fixture/newer-draft' } });
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'codex', application: 'failed', ready: false, entry_eligible: false }));
    await act(async () => saved.resolve({ agent_backend_runtime: { hot_reconciled: false, restart_error: 'fixture failed apply' } }));
    await screen.findByRole('alert');
    expect(path.value).toBe('/fixture/newer-draft'); expect(runtimeSave()).toBeTruthy();
    fireEvent.change(path, { target: { value: '/fixture/saved-codex' } });
    expect(runtimeSave()).toBeUndefined();
  });
  it('stale Claude token cleanup publishes partial removal and application failure separately', async () => {
    mock.api.getClaudeAuth.mockResolvedValue({ ...native(), active_auth_mode: 'none' });
    render(wrap(<BackendConnectionForm backend="claude" />));
    const cleanupButton = await screen.findByRole('button', { name: en.settings.backends.oauthCleanStoredCredentials });
    mock.api.removeClaudeOAuthCredentials.mockResolvedValue({ ok: true, partial: true, warning: 'oauth_cleanup_failed', detail: 'fixture cleanup warning' });
    mock.api.getClaudeAuth.mockResolvedValue({ ...native(), active_auth_mode: 'api_key', has_oauth_credentials: false });
    const application = deferred<BackendConnectionState>();
    mock.api.getBackendConnection.mockReturnValue(application.promise);
    fireEvent.click(cleanupButton);
    await screen.findByText('sk-•••old');
    await act(async () => application.resolve(connection({ application: 'failed', ready: false, entry_eligible: false, message: 'fixture apply failed' })));
    expect((await screen.findByRole('alert')).textContent).toContain('fixture apply failed');
    expect(screen.queryByRole('button', { name: en.settings.backends.oauthCleanStoredCredentials })).toBeNull();
    await waitFor(() => expect(mock.toast.mock.calls.some(([message, kind]) => kind === 'warning' && message.includes('fixture cleanup warning'))).toBe(true));
    expect(mock.toast.mock.calls.some(([, kind]) => kind === 'success')).toBe(false);
  });
  it('a lost signout response reads committed token absence and keeps retry observational', async () => {
    mock.api.getCodexAuth.mockResolvedValue(codexNative({ active_auth_mode: 'oauth', has_api_key: false }));
    render(wrap(<BackendConnectionForm backend="codex" />));
    const remove = await screen.findByRole('button', { name: en.settings.backends.oauthRemove });
    mock.api.removeBackendAuth.mockRejectedValue(new Error('fixture signout response lost'));
    mock.api.getCodexAuth.mockResolvedValue(codexNative({ active_auth_mode: 'none', has_api_key: false, has_chatgpt_tokens: false }));
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'codex', auth: 'none', ready: false, entry_eligible: false }));
    fireEvent.click(remove);
    expect((await screen.findByRole('alert')).textContent).toContain('fixture signout response lost');
    expect(screen.queryByRole('button', { name: en.settings.backends.oauthRemove })).toBeNull();
    const refresh = screen.getByRole('button', { name: en.onboarding.connection.refresh });
    await waitFor(() => expect(refresh.hasAttribute('disabled')).toBe(false)); fireEvent.click(refresh);
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    expect(mock.api.removeBackendAuth).toHaveBeenCalledOnce(); expect(mock.api.saveCodexAuth).not.toHaveBeenCalled();
  });
  it('a lost save response still observes persisted credentials and retains its failure receipt', async () => {
    render(wrap(<BackendConnectionForm backend="codex" initialMethod="api_key" />));
    await screen.findByText('sk-•••old');
    mock.api.saveCodexAuth.mockRejectedValue(new Error('fixture lost response'));
    mock.api.getCodexAuth.mockResolvedValue(codexNative({ api_key_masked: 'new-mask' }));
    fireEvent.click(screen.getByRole('button', { name: en.common.save }));
    await screen.findByText('new-mask');
    expect((await screen.findByRole('alert')).textContent).toContain('fixture lost response');
    expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull();
  });
  it.each([true, false])('preserves masked credentials, saves once, confirms effective application (compact=%s)', async (compact) => {
    const saved = deferred<{ ok: boolean; restart: { ok: boolean } }>();
    mock.api.saveClaudeAuth.mockReturnValue(saved.promise);
    const connected = vi.fn();
    render(wrap(<BackendConnectionForm backend="claude" compact={compact} initialMethod="api_key" onConnected={connected} />));
    await screen.findByText('sk-•••old');
    fireEvent.change(screen.getByLabelText(en.onboarding.connection.baseUrl), { target: { value: 'https://新的.example/v1' } });
    const button = screen.getByRole('button', { name: compact ? en.onboarding.connection.saveConnect : en.common.save });
    fireEvent.click(button); fireEvent.click(button);
    expect(mock.api.saveClaudeAuth).toHaveBeenCalledOnce();
    expect(mock.api.saveClaudeAuth).toHaveBeenCalledWith({ auth_mode: 'api_key', credential_type: 'api_key', api_key: undefined, base_url: 'https://新的.example/v1' });
    expect(connected).not.toHaveBeenCalled();
    await act(async () => saved.resolve({ ok: true, restart: { ok: true } }));
    expect(connected).toHaveBeenCalledOnce();
    expect(mock.api.getBackendConnection).toHaveBeenCalledWith('claude');
  });
  it('keeps replacement input and dialog open on runtime apply failure', async () => {
    mock.api.saveClaudeAuth.mockResolvedValue({ ok: true, restart: { ok: false, message: 'apply failed' } });
    const close = vi.fn();
    render(wrap(<BackendConnectionDialog backend="claude" method="api_key" onConnected={vi.fn()} onClose={close} onWriteState={vi.fn()} />));
    fireEvent.click(await screen.findByRole('button', { name: 'Replace' }));
    fireEvent.change(screen.getByLabelText(en.onboarding.connection.apiKeyLabel), { target: { value: 'new-test-key' } });
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.saveConnect }));
    expect(await screen.findByText('apply failed')).toBeTruthy();
    expect((screen.getByLabelText(en.onboarding.connection.apiKeyLabel) as HTMLInputElement).value).toBe('new-test-key');
    expect(close).not.toHaveBeenCalled();
  });
  it('does not claim connection for accepted draining; readback can finish without another save', async () => {
    mock.api.getBackendConnection.mockResolvedValue(connection({ ok: true, application: 'draining', ready: false, entry_eligible: false }));
    const connected = vi.fn();
    render(wrap(<BackendConnectionForm backend="claude" compact initialMethod="api_key" onConnected={connected} />));
    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.connection.saveConnect }));
    await screen.findByText(en.onboarding.connection.applyPending);
    expect(connected).not.toHaveBeenCalled();
    mock.api.getBackendConnection.mockResolvedValue(connection({ ok: true, application: 'applied', ready: true }));
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.refresh }));
    await waitFor(() => expect(connected).toHaveBeenCalledOnce());
    expect(mock.api.saveClaudeAuth).toHaveBeenCalledOnce();
  });
  it('reports pending write settlement after close while ignoring late success presentation', async () => {
    const saved = deferred<{ ok: boolean }>(); mock.api.saveClaudeAuth.mockReturnValue(saved.promise);
    const connected = vi.fn(); const writes = vi.fn();
    const { unmount } = render(wrap(<BackendConnectionForm backend="claude" compact initialMethod="api_key" onConnected={connected} onWriteState={writes} />));
    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.connection.saveConnect }));
    expect(writes).toHaveBeenCalledWith(true); unmount();
    await act(async () => saved.resolve({ ok: true }));
    expect(writes).toHaveBeenLastCalledWith(false); expect(connected).not.toHaveBeenCalled();
  });
  it('cancels a late start with force_reset=false and never accepts its result after close', async () => {
    const started = deferred<{ ok: boolean; flow_id: string; state: 'awaiting_code'; url: string }>();
    mock.api.startOAuthWeb.mockReturnValue(started.promise);
    const connected = vi.fn();
    const { unmount } = render(wrap(<BackendConnectionForm backend="claude" compact onConnected={connected} />));
    const start = await screen.findByRole('button', { name: en.onboarding.connection.claudeSignIn });
    fireEvent.click(start); fireEvent.click(start);
    expect(mock.api.startOAuthWeb).toHaveBeenCalledOnce();
    expect(mock.api.startOAuthWeb).toHaveBeenCalledWith('claude', false);
    unmount();
    await act(async () => started.resolve({ ok: true, flow_id: 'late-flow', state: 'awaiting_code', url: 'https://example.invalid' }));
    expect(mock.api.cancelOAuthWeb).toHaveBeenCalledWith('claude', 'late-flow');
    expect(connected).not.toHaveBeenCalled(); expect(mock.api.getOAuthWebStatus).not.toHaveBeenCalled();
  });
  it('retains Settings stale Claude credential cleanup', async () => {
    mock.api.getClaudeAuth.mockResolvedValue({ ...native(), active_auth_mode: 'none' });
    mock.api.removeClaudeOAuthCredentials.mockResolvedValue({ ok: true });
    render(wrap(<BackendConnectionForm backend="claude" />));
    fireEvent.click(await screen.findByRole('button', { name: en.settings.backends.oauthCleanStoredCredentials }));
    await waitFor(() => expect(mock.api.removeClaudeOAuthCredentials).toHaveBeenCalledOnce());
    expect(mock.api.getBackendConnection).toHaveBeenCalledWith('claude');
  });
  it('ignores late poll and submit results after cancelling the dialog', async () => {
    const status = deferred<{ ok: boolean; state: 'success' }>();
    const submit = deferred<{ ok: boolean }>();
    mock.api.startOAuthWeb.mockResolvedValue({ ok: true, flow_id: 'poll-flow', state: 'awaiting_code', url: 'https://fixture.invalid' });
    mock.api.getOAuthWebStatus.mockReturnValue(status.promise);
    mock.api.submitOAuthWebCode.mockReturnValue(submit.promise);
    const connected = vi.fn(); const writes = vi.fn();
    const { unmount } = render(wrap(<BackendConnectionForm backend="claude" compact onConnected={connected} onWriteState={writes} />));
    const start = await screen.findByRole('button', { name: en.onboarding.connection.claudeSignIn });
    vi.useFakeTimers(); await act(async () => fireEvent.click(start));
    const input = screen.getByLabelText(en.settings.backends.claudeCallbackCodeLabel);
    fireEvent.change(input, { target: { value: 'fixture-code#state' } });
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.finishConnect }));
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(mock.api.getOAuthWebStatus).toHaveBeenCalledOnce();
    unmount();
    await act(async () => { status.resolve({ ok: true, state: 'success' }); submit.resolve({ ok: true }); });
    expect(mock.api.cancelOAuthWeb).toHaveBeenCalledWith('claude', 'poll-flow');
    expect(connected).not.toHaveBeenCalled(); expect(writes).toHaveBeenLastCalledWith(false);
  });
  it('preserves keyless custom-provider URL editing in Settings without claiming a credential connection', async () => {
    const provider = { id: 'local-test', name: 'Local', description: '', configured: true, custom: true, oauth_available: false, local: true, models: [], base_url: 'http://localhost:4567/v1' };
    mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [provider] });
    mock.api.setOpencodeProviderAuth.mockResolvedValue({ ok: true, restart: { ok: true } });
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'opencode', ok: true, application: 'applied', ready: false, entry_eligible: false, auth: 'none' }));
    render(wrap(<BackendConnectionForm backend="opencode" provider={provider} />));
    const button = await screen.findByRole('button', { name: en.common.save });
    expect(button.hasAttribute('disabled')).toBe(false); fireEvent.click(button);
    await waitFor(() => expect(mock.api.setOpencodeProviderAuth).toHaveBeenCalledWith('local-test', undefined, 'http://localhost:4567/v1'));
    expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('does not label an unconnected provider ready because another OpenCode provider is connected', async () => {
    const provider = { id: 'new-provider', name: 'New provider', description: '', configured: false, oauth_available: false, local: false, models: [] };
    mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [provider] });
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'opencode', ok: true, application: 'applied', ready: true, auth: 'api_key' }));
    render(wrap(<BackendConnectionForm backend="opencode" compact provider={provider} />));
    await screen.findByLabelText('API Key');
    expect(screen.queryByText(en.onboarding.connection.connected)).toBeNull();
  });

  it('keeps Claude Auth Token payload distinct from the compact API Key label', async () => {
    mock.api.getClaudeAuth.mockResolvedValue({ ...native(), credential_type: 'auth_token' });
    render(wrap(<BackendConnectionForm backend="claude" compact initialMethod="api_key" />));
    fireEvent.click(await screen.findByRole('button', { name: 'Replace' }));
    fireEvent.change(screen.getByLabelText('Auth Token', { exact: true }), { target: { value: 'fixture-token' } });
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.saveConnect }));
    await waitFor(() => expect(mock.api.saveClaudeAuth).toHaveBeenCalledWith({ auth_mode: 'api_key', credential_type: 'auth_token', api_key: 'fixture-token', base_url: 'https://old.example' }));
  });
  it('Codex saves through its native owner and refuses uncertain keychain readback', async () => {
    mock.api.getCodexAuth.mockResolvedValue(codexNative({ auth_mode_uncertain: true }));
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'codex', ok: true, application: 'applied', ready: false, entry_eligible: false, auth: 'unknown' }));
    mock.api.saveCodexAuth.mockResolvedValue({ ok: true, restart: { ok: true } });
    const connected = vi.fn();
    render(wrap(<BackendConnectionForm backend="codex" compact initialMethod="api_key" onConnected={connected} />));
    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.connection.saveConnect }));
    expect(await screen.findByText(en.onboarding.connection.unconfirmed)).toBeTruthy();
    expect(mock.api.saveCodexAuth).toHaveBeenCalledWith({ auth_mode: 'api_key', api_key: undefined, base_url: 'https://old.example' });
    expect(mock.api.saveClaudeAuth).not.toHaveBeenCalled(); expect(connected).not.toHaveBeenCalled();
  });
  it('OpenCode runtime-declared manual code uses the existing submit owner and blocks duplicate submit', async () => {
    const provider = { id: 'poe', name: 'Poe', description: '', configured: false, oauth_available: true, local: false, models: [] };
    mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [provider] });
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'opencode', ok: true, auth: 'none', application: 'applied', ready: false, entry_eligible: false }));
    mock.api.startOAuthWebForOpencodeProvider.mockResolvedValue({ ok: true, flow_id: 'manual-flow', state: 'awaiting_code', callback_kind: 'code', url: 'https://fixture.invalid' });
    const pending = deferred<{ ok: boolean }>(); mock.api.submitOAuthWebCode.mockReturnValue(pending.promise);
    render(wrap(<BackendConnectionForm backend="opencode" compact provider={provider} />));
    fireEvent.click(await screen.findByRole('button', { name: en.settings.backends.opencodeProviderSignIn }));
    fireEvent.change(await screen.findByLabelText(en.onboarding.connection.manualCode), { target: { value: 'fixture-code' } });
    const submit = screen.getByRole('button', { name: en.onboarding.connection.finishConnect }); fireEvent.click(submit); fireEvent.click(submit);
    expect(mock.api.submitOAuthWebCode).toHaveBeenCalledExactlyOnceWith('opencode', 'manual-flow', 'fixture-code');
    expect(mock.api.setOpencodeProviderAuth).not.toHaveBeenCalled();
    await act(async () => pending.resolve({ ok: true }));
  });

});
