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
import type { BackendConnectionState, ClaudeAuthState, CodexAuthState, OpencodeProvider } from '../../context/ApiContext';

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
/**
 * A radio group, once it will actually answer.
 *
 * The method row is rendered while the connection read behind it is still in
 * flight, and `BackendConnectionForm` disables its radios until that read lands
 * (`disabled={busy || loading}`, and `onChange` refuses for the same reason). A
 * click fired on existence alone is therefore not dropped by the test — it is
 * refused by the product, the switch never happens, and whatever the test waits
 * for next can never arrive. How many ticks the read takes is not this file's
 * business, so every interaction starts where a person's would: at the point the
 * control can be acted on.
 */
const interactiveRadios = async (name: string) => {
  const group = await screen.findByRole('radiogroup', { name });
  await waitFor(() => expect(within(group).getAllByRole('radio')
    .every((radio) => !(radio as HTMLButtonElement).disabled)).toBe(true));
  return group;
};
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
  // An API key and an auth token are different credentials, not two spellings
  // of one. So the two sides of the credential switch own their own unsent
  // value AND their own answer to "is something already stored here" — a mask
  // saved as an API key says nothing about the token side.
  it('keeps the Claude API Key and Auth Token drafts independent of each other', async () => {
    mock.api.getClaudeAuth.mockResolvedValue(native());
    render(wrap(<BackendConnectionForm backend="claude" compact initialMethod="api_key" />));
    const credential = async (id: 'API Key' | 'Auth Token') => {
      const group = await interactiveRadios(en.onboarding.connection.credentialType);
      fireEvent.click(within(group).getByRole('radio', { name: id }));
    };
    // The stored key is an API key, so that side opens on its mask.
    fireEvent.click(await screen.findByRole('button', { name: 'Replace' }));
    fireEvent.change(screen.getByLabelText('API Key', { exact: true }), { target: { value: 'sk-ant-半成品' } });

    await credential('Auth Token');
    // Nothing stored under this type, so no mask to replace and nothing typed.
    expect(screen.queryByRole('button', { name: 'Replace' })).toBeNull();
    expect((screen.getByLabelText('Auth Token', { exact: true }) as HTMLInputElement).value).toBe('');
    // Saving is not allowed on the strength of the other type's stored key.
    expect((screen.getByRole('button', { name: en.onboarding.connection.saveConnect }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Auth Token', { exact: true }), { target: { value: '中转令牌' } });

    await credential('API Key');
    expect((screen.getByLabelText('API Key', { exact: true }) as HTMLInputElement).value).toBe('sk-ant-半成品');
    await credential('Auth Token');
    expect((screen.getByLabelText('Auth Token', { exact: true }) as HTMLInputElement).value).toBe('中转令牌');

    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.saveConnect }));
    await waitFor(() => expect(mock.api.saveClaudeAuth).toHaveBeenCalledWith({ auth_mode: 'api_key', credential_type: 'auth_token', api_key: '中转令牌', base_url: 'https://old.example' }));
  });
  // Saving is the other half of keeping the two drafts apart. A receipt settles
  // ONE submission, so it may only spend the draft that submission actually sent:
  // the type that was not saved still holds work nobody has stored anywhere, and
  // clearing it is the same loss as typing it into the wrong field.
  const settingsCredential = async (label: string) => {
    const group = await interactiveRadios(en.settings.backends.claudeCredentialTypeLabel);
    fireEvent.click(within(group).getByRole('radio', { name: label }));
  };
  const API_KEY = en.settings.backends.claudeCredentialTypeApiKey;
  const AUTH_TOKEN = en.settings.backends.claudeCredentialTypeAuthToken;
  const draftValue = (label: string) => (screen.getByLabelText(label, { exact: true }) as HTMLInputElement).value;
  it('a Settings save spends only the credential it sent, in both directions', async () => {
    const saved = vi.fn();
    render(wrap(<BackendConnectionForm backend="claude" initialMethod="api_key" onConnected={saved} />));
    // The stored credential is an API key, so that side opens on its mask.
    fireEvent.click(await screen.findByRole('button', { name: en.settings.backends.replaceApiKey }));
    fireEvent.change(screen.getByLabelText(API_KEY, { exact: true }), { target: { value: 'sk-ant-半成品' } });
    await settingsCredential(AUTH_TOKEN);
    fireEvent.change(screen.getByLabelText(AUTH_TOKEN, { exact: true }), { target: { value: '中转令牌' } });

    fireEvent.click(screen.getByRole('button', { name: en.common.save }));
    await waitFor(() => expect(mock.api.saveClaudeAuth).toHaveBeenCalledWith({ auth_mode: 'api_key', credential_type: 'auth_token', api_key: '中转令牌', base_url: 'https://old.example' }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    // What was sent is spent; what was not sent is still exactly where it was left.
    expect(draftValue(AUTH_TOKEN)).toBe('');
    await settingsCredential(API_KEY);
    expect(draftValue(API_KEY)).toBe('sk-ant-半成品');

    // And the same in the other direction, with fresh work waiting on the token side.
    await settingsCredential(AUTH_TOKEN);
    fireEvent.change(screen.getByLabelText(AUTH_TOKEN, { exact: true }), { target: { value: '中转令牌-改' } });
    await settingsCredential(API_KEY);
    fireEvent.click(screen.getByRole('button', { name: en.common.save }));
    await waitFor(() => expect(mock.api.saveClaudeAuth).toHaveBeenLastCalledWith({ auth_mode: 'api_key', credential_type: 'api_key', api_key: 'sk-ant-半成品', base_url: 'https://old.example' }));
    // Spending the API key draft returns that side to the stored mask...
    await screen.findByRole('button', { name: en.settings.backends.replaceApiKey });
    // ...and leaves the token nobody saved untouched.
    await settingsCredential(AUTH_TOKEN);
    expect(draftValue(AUTH_TOKEN)).toBe('中转令牌-改');
  });
  // A confirmation can arrive long after the write: through Refresh, and with the
  // person now looking at the other credential. What it settles is the submission,
  // so the type on screen at that moment decides nothing.
  it('a deferred confirmation spends the credential that was submitted, not the one on screen', async () => {
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'claude', application: 'draining', ready: false }));
    const saved = vi.fn();
    render(wrap(<BackendConnectionForm backend="claude" initialMethod="api_key" onConnected={saved} />));
    await settingsCredential(AUTH_TOKEN);
    fireEvent.change(await screen.findByLabelText(AUTH_TOKEN, { exact: true }), { target: { value: '中转令牌' } });
    fireEvent.click(screen.getByRole('button', { name: en.common.save }));
    await waitFor(() => expect(mock.api.saveClaudeAuth).toHaveBeenCalledWith({ auth_mode: 'api_key', credential_type: 'auth_token', api_key: '中转令牌', base_url: 'https://old.example' }));
    // The write landed but the runtime has not confirmed it, so nothing is spent yet.
    const refresh = await screen.findByRole('button', { name: en.onboarding.connection.refresh });
    expect(saved).not.toHaveBeenCalled();
    expect(draftValue(AUTH_TOKEN)).toBe('中转令牌');

    // Meanwhile the other side is filled in, and left on screen.
    await settingsCredential(API_KEY);
    fireEvent.click(screen.getByRole('button', { name: en.settings.backends.replaceApiKey }));
    fireEvent.change(screen.getByLabelText(API_KEY, { exact: true }), { target: { value: 'sk-ant-半成品' } });

    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'claude' }));
    fireEvent.click(refresh);
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    // The API key on screen was never submitted, so the receipt has no claim on it.
    expect(draftValue(API_KEY)).toBe('sk-ant-半成品');
    await settingsCredential(AUTH_TOKEN);
    expect(draftValue(AUTH_TOKEN)).toBe('');
    expect(mock.api.saveClaudeAuth).toHaveBeenCalledOnce();
  });
  // The narrower version of the same rule: the submitted type is right, but the
  // value is not the one that was sent any more. A receipt settles the write it
  // belongs to, and a replacement typed since is a newer intention than that write.
  it('a newer edit outlives the receipt for the write it replaced', async () => {
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'claude', application: 'draining', ready: false }));
    const saved = vi.fn();
    render(wrap(<BackendConnectionForm backend="claude" initialMethod="api_key" onConnected={saved} />));
    await settingsCredential(AUTH_TOKEN);
    fireEvent.change(await screen.findByLabelText(AUTH_TOKEN, { exact: true }), { target: { value: '中转令牌' } });
    fireEvent.click(screen.getByRole('button', { name: en.common.save }));
    await waitFor(() => expect(mock.api.saveClaudeAuth).toHaveBeenCalledOnce());
    const refresh = await screen.findByRole('button', { name: en.onboarding.connection.refresh });

    fireEvent.change(screen.getByLabelText(AUTH_TOKEN, { exact: true }), { target: { value: '中转令牌-改' } });
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'claude' }));
    fireEvent.click(refresh);
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(draftValue(AUTH_TOKEN)).toBe('中转令牌-改');
  });
  // Signing in settles the account. It sends no credential at all, so there is
  // nothing for its receipt to spend — including the key someone was part-way
  // through typing when they decided to try the subscription instead.
  it('an OAuth confirmation spends neither credential draft', async () => {
    mock.api.getClaudeAuth.mockResolvedValue({ ...native(), active_auth_mode: 'api_key' });
    const saved = vi.fn();
    render(wrap(<BackendConnectionForm backend="claude" compact initialMethod="api_key" onConnected={saved} />));
    fireEvent.click(await screen.findByRole('button', { name: en.settings.backends.replaceApiKey }));
    fireEvent.change(screen.getByLabelText('API Key', { exact: true }), { target: { value: 'sk-ant-半成品' } });
    const credentials = await interactiveRadios(en.onboarding.connection.credentialType);
    fireEvent.click(within(credentials).getByRole('radio', { name: 'Auth Token' }));
    fireEvent.change(screen.getByLabelText('Auth Token', { exact: true }), { target: { value: '中转令牌' } });

    const method = await interactiveRadios(en.onboarding.connection.method);
    fireEvent.click(within(method).getByRole('radio', { name: en.onboarding.connection.claudeLogin }));
    mock.api.startOAuthWeb.mockResolvedValue({ ok: true, flow_id: 'draft-safe', state: 'success' });
    mock.api.getClaudeAuth.mockResolvedValue({ ...native(), active_auth_mode: 'oauth' });
    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.connection.claudeSignIn }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());

    fireEvent.click(within(await interactiveRadios(en.onboarding.connection.method)).getByRole('radio', { name: en.onboarding.connection.claudeCredentials }));
    expect(draftValue('Auth Token')).toBe('中转令牌');
    fireEvent.click(within(await interactiveRadios(en.onboarding.connection.credentialType)).getByRole('radio', { name: 'API Key' }));
    expect(draftValue('API Key')).toBe('sk-ant-半成品');
    expect(mock.api.saveClaudeAuth).not.toHaveBeenCalled();
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

// What a fixed frame means in behavior rather than in pixels: the parts that
// anchor it must not be able to say anything that depends on what is inside it.
// jsdom has no layout, so these hold the cause — a heading, a description and a
// footer that are the same strings before and after every method change — and
// `geometry.spec.ts` holds the resulting geometry in a real browser.
describe('connection dialog frame', () => {
  const frame = () => {
    const dialog = screen.getByRole('dialog');
    return {
      title: within(dialog).getByRole('heading', { level: 2 }).textContent,
      description: dialog.querySelector('.connection-description')?.textContent,
      // The regions the grid anchors, by the classes `connection.css` places.
      rows: ['.connection-heading', '.connection-description', '.connection-actions']
        .map((selector) => dialog.querySelectorAll(selector).length),
    };
  };
  it.each(['claude', 'codex'] as const)('%s keeps one heading, description and footer across every method switch', async (backend) => {
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend, auth: 'none', ready: false, entry_eligible: false }));
    render(wrap(<BackendConnectionDialog backend={backend} method="oauth" onConnected={vi.fn()} onClose={vi.fn()} onWriteState={vi.fn()} />));
    const group = await interactiveRadios(en.onboarding.connection.method);
    const opened = frame();
    expect(opened.title).toBe(`Connect ${backend === 'claude' ? 'Claude Code' : 'Codex'}`);
    expect(opened.rows).toEqual([1, 1, 1]);

    const tabs = within(group).getAllByRole('radio');
    // Repeatedly, both directions: a heading computed from the method would
    // change here, and a changed heading is what used to move the frame.
    for (const tab of [...tabs].reverse().concat(tabs, [...tabs].reverse(), tabs)) {
      fireEvent.click(tab);
      expect(frame()).toEqual(opened);
    }
    // The method still actually changed the body underneath the fixed frame.
    fireEvent.click(within(group).getByRole('radio', { name: backend === 'claude' ? en.onboarding.connection.claudeCredentials : en.onboarding.connection.openaiKey }));
    // `find`, not `get`: the dialog's own readiness read resolves on its own schedule and
    // re-renders the body, so the credential form is not guaranteed on the click's tick.
    expect(await screen.findByRole('button', { name: en.onboarding.connection.saveConnect })).toBeTruthy();
    expect(frame()).toEqual(opened);
  });

  // The readiness the helper waits for, made deterministic rather than left to
  // how many ticks a resolved mock happens to take: while the connection read is
  // in flight the method row is already on screen and refuses to switch, so
  // acting on its mere presence changes nothing — and nothing that a switch
  // would have produced can ever arrive.
  it('refuses a method switch until the connection read lands, then takes it', async () => {
    const pending = deferred<BackendConnectionState>();
    mock.api.getBackendConnection.mockReturnValue(pending.promise);
    render(wrap(<BackendConnectionDialog backend="claude" method="oauth" onConnected={vi.fn()} onClose={vi.fn()} onWriteState={vi.fn()} />));
    const group = await screen.findByRole('radiogroup', { name: en.onboarding.connection.method });
    const credentials = () => within(group).getByRole('radio', { name: en.onboarding.connection.claudeCredentials });
    expect((credentials() as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(credentials());
    expect(credentials().getAttribute('aria-checked')).toBe('false');
    expect(screen.queryByRole('button', { name: en.onboarding.connection.saveConnect })).toBeNull();

    await act(async () => pending.resolve(connection({ backend: 'claude', auth: 'none', ready: false, entry_eligible: false })));
    await interactiveRadios(en.onboarding.connection.method);
    fireEvent.click(credentials());
    expect(credentials().getAttribute('aria-checked')).toBe('true');
    expect(await screen.findByRole('button', { name: en.onboarding.connection.saveConnect })).toBeTruthy();
  });

  it('moves the method with arrows, Home and End, and leaves focus on the selected tab', async () => {
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'claude', auth: 'none', ready: false, entry_eligible: false }));
    render(wrap(<BackendConnectionDialog backend="claude" method="oauth" onConnected={vi.fn()} onClose={vi.fn()} onWriteState={vi.fn()} />));
    const group = await interactiveRadios(en.onboarding.connection.method);
    const [subscription, apiKey] = within(group).getAllByRole('radio');
    expect(subscription.getAttribute('aria-checked')).toBe('true');
    // One tab stop, so Tab lands on the selection the arrows then continue from — the
    // other half of the pattern, and what stops focus resting on an unselected tab.
    expect([subscription.tabIndex, apiKey.tabIndex]).toEqual([0, -1]);
    for (const [key, expected] of [['ArrowRight', apiKey], ['ArrowLeft', subscription], ['End', apiKey], ['Home', subscription]] as const) {
      fireEvent.keyDown(group, { key });
      expect(expected.getAttribute('aria-checked')).toBe('true');
      expect(document.activeElement).toBe(expected);
      expect([subscription.tabIndex, apiKey.tabIndex]).toEqual(expected === subscription ? [0, -1] : [-1, 0]);
    }
    // A key the group does not own is left to the dialog (Escape closes it).
    fireEvent.keyDown(group, { key: 'Escape' });
    expect(subscription.getAttribute('aria-checked')).toBe('true');
  });

  // The picker's whole job in one fixture: every alias the brand table knows,
  // two brands that arrive under more than one id, a configured provider that
  // is NOT one of the eight, and the two ids the contract refuses to promote.
  const PICKER = [
    ['mistral', 'Mistral AI', { configured: true }], ['openrouter', 'OpenRouter', {}],
    ['alibaba-cn', 'Alibaba (China)', {}], ['alibaba', 'alibaba', {}], ['dashscope', 'Alibaba DashScope', { configured: true }],
    ['openai', 'OpenAI', {}], ['moonshot', 'moonshot', {}], ['moonshotai', 'Moonshot AI', {}],
    ['google', 'google', {}], ['google-vertex', 'google-vertex', {}], ['zhipu', 'zhipu', {}],
    ['deepseek', 'DeepSeek', {}], ['anthropic', 'Anthropic', {}], ['xai', 'xAI', {}],
    ['cerebras', 'Cerebras', {}], ['groq', 'Groq', { models: ['llama-3.3-70b'] }],
    ['zen', 'Zen', {}], ['go', 'Go', {}],
    ['ollama', 'Ollama local', { local: true }],
  ] as const;
  const pickerProvider = ([id, name, patch]: (typeof PICKER)[number]) =>
    ({ id, name, description: '', configured: false, oauth_available: false, local: false, models: [] as string[], ...patch });

  it('gives each prioritised brand exactly one row, and keeps every other native provider reachable', async () => {
    mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: PICKER.map(pickerProvider) });
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'opencode', auth: 'none', ready: false, entry_eligible: false }));
    render(wrap(<BackendConnectionDialog backend="opencode" method="api_key" onConnected={vi.fn()} onClose={vi.fn()} onWriteState={vi.fn()} />));

    // A row is its title and the id it is filed under — the second line, which
    // is what the rest of OpenCode, its config and its model ids actually use.
    const rows = () => screen.getAllByRole('button').filter((node) => node.querySelector('strong'))
      .map((node) => ({ title: node.querySelector('strong')?.textContent ?? '', id: node.querySelector('small')?.textContent ?? '' }));
    const names = () => rows().map((row) => row.title);
    await waitFor(() => expect(names().length).toBeGreaterThan(0));

    // Eight rows, one per brand, in the approved order and under the approved
    // names — `google`, `moonshot` and the Alibaba ids are not what anyone
    // calls them, and none of them may appear twice.
    expect(names()).toEqual(['OpenAI', 'Anthropic', 'xAI', 'Gemini', 'DeepSeek', 'Qwen', 'Kimi', 'OpenRouter']);
    // Which entry stands in a slot is decided, not incidental: the configured
    // one where a brand has one, else the lowest id.
    expect(rows().map((row) => row.id)).toEqual(['openai', 'anthropic', 'xai', 'google', 'deepseek', 'dashscope', 'moonshot', 'openrouter']);
    // Being configured is not a promotion: Mistral holds credentials and still
    // does not take a ninth slot.
    expect(names()).not.toContain('Mistral AI');
    // And the two ids the contract refuses to offer as an official choice
    // cannot reach the default list, because only the eight brands can.
    expect(names()).not.toContain('Zen');
    expect(names()).not.toContain('Go');

    const eligible = PICKER.filter(([, , patch]) => !('local' in patch)).map(([id]) => id);
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`More providers \\(${eligible.length - 8}\\)`) }));
    // Nothing the runtime offers is unreachable: the two lists together are
    // exactly the providers an API key can be saved for, each one once.
    expect(rows().map((row) => row.id).sort()).toEqual([...eligible].sort());
    const [shortlist, more] = [rows().slice(0, 8), rows().slice(8)];
    // Credentials already held are a reason to be easy to find, so More opens
    // on them; the rest follow by name.
    expect(more[0]).toEqual({ title: 'Mistral AI', id: 'mistral' });
    const rest = more.slice(1).map((row) => row.title);
    expect(rest).toEqual([...rest].sort((left, right) => left.localeCompare(right)));
    expect(more.map((row) => row.title)).toContain('Zen');
    expect(more.map((row) => row.title)).toContain('Go');
    // No two rows may read the same. A brand's remaining entries keep their own
    // name when the server sent one, and fall back to their id when it did not
    // — `alibaba` would otherwise read "Qwen" twice over.
    expect(new Set(names()).size).toBe(names().length);
    expect(more).toContainEqual({ title: 'Alibaba (China)', id: 'alibaba-cn' });
    expect(more).toContainEqual({ title: 'alibaba', id: 'alibaba' });
    expect(more).toContainEqual({ title: 'Moonshot AI', id: 'moonshotai' });
    expect(more).toContainEqual({ title: 'Zhipu AI', id: 'zhipu' });
    expect(shortlist).toContainEqual({ title: 'Gemini', id: 'google' });

    // Search reaches every variant, under the brand name as well as the id.
    fireEvent.change(screen.getByLabelText(en.settings.backends.opencodeSearchPlaceholder), { target: { value: 'qwen' } });
    expect(rows().map((row) => row.id)).toEqual(['alibaba-cn', 'alibaba', 'dashscope']);
    expect(screen.queryByRole('button', { name: /More providers/ })).toBeNull();
    fireEvent.change(screen.getByLabelText(en.settings.backends.opencodeSearchPlaceholder), { target: { value: 'llama-3.3' } });
    expect(names()).toEqual(['Groq']);

    // Picking one hands the middle to the form and keeps the same frame rows.
    fireEvent.change(screen.getByLabelText(en.settings.backends.opencodeSearchPlaceholder), { target: { value: 'dashscope' } });
    fireEvent.click(screen.getByRole('button', { name: /DashScope/ }));
    expect(await screen.findByLabelText(en.onboarding.connection.apiKeyLabel)).toBeTruthy();
    expect(frame().title).toBe('Connect OpenCode');
    expect(frame().rows).toEqual([1, 1, 1]);
    expect(screen.getByRole('button', { name: en.onboarding.connection.changeProvider }).closest('.connection-chosen')?.textContent).toContain('Alibaba DashScope');
  });

  // A custom provider's id is whatever its author typed into Settings, and
  // OpenCode reserves only the ids it ships itself — so a personal relay can be
  // filed under `dashscope`, and every custom provider is reported configured,
  // which is exactly what decides a brand slot. Left alone, the relay would BE
  // the row someone picks to connect Qwen, and the key would go to the relay.
  it('never lets a custom provider stand in for, or be named as, a native brand', async () => {
    const entry = (id: string, name: string, patch: Partial<OpencodeProvider> = {}) =>
      ({ id, name, description: '', configured: false, oauth_available: false, local: false, models: [] as string[], ...patch });
    mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [
      entry('openai', 'OpenAI'), entry('alibaba-cn', 'Alibaba (China)'),
      // Filed under a brand's alias, and holding a name of its author's own.
      entry('dashscope', '内部中转', { configured: true, custom: true }),
      // And one whose author left the name blank: the server echoes the id back,
      // so the only thing left to read it as a brand with is the id itself.
      entry('gemini', 'gemini', { configured: true, custom: true }),
    ] });
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'opencode', auth: 'none', ready: false, entry_eligible: false }));
    render(wrap(<BackendConnectionDialog backend="opencode" method="api_key" onConnected={vi.fn()} onClose={vi.fn()} onWriteState={vi.fn()} />));
    const nodes = () => screen.getAllByRole('button').filter((node) => node.querySelector('strong'));
    const rows = () => nodes().map((node) => ({ title: node.querySelector('strong')?.textContent ?? '', id: node.querySelector('small')?.textContent ?? '' }));
    await waitFor(() => expect(rows().length).toBeGreaterThan(0));

    // The slot goes to the entry OpenCode ships, even though the relay is the
    // configured one; and a brand with nothing but a relay behind it gets no
    // row at all, rather than one that would name an endpoint after it.
    expect(rows()).toEqual([{ title: 'OpenAI', id: 'openai' }, { title: 'Qwen', id: 'alibaba-cn' }]);

    fireEvent.click(screen.getByRole('button', { name: /More providers \(2\)/ }));
    // Both stay reachable under More, under what they were configured as.
    expect(rows().slice(2)).toEqual([{ title: 'gemini', id: 'gemini' }, { title: '内部中转', id: 'dashscope' }]);
    // And they are drawn as what they are — an address you supply, the same
    // mark the custom vendor has always used — not as the brand they sit next to.
    const mark = (id: string) => nodes().find((node) => node.querySelector('small')?.textContent === id)
      ?.querySelector('.connection-provider-mark')?.innerHTML;
    expect(mark('dashscope')).toBe(mark('gemini'));
    expect(mark('dashscope')).not.toBe(mark('alibaba-cn'));

    // Searching a brand reaches the entries that brand actually ships — under
    // their own names, since search lists rather than collapses — and never the
    // relay wearing its id.
    fireEvent.change(screen.getByLabelText(en.settings.backends.opencodeSearchPlaceholder), { target: { value: 'qwen' } });
    expect(rows()).toEqual([{ title: 'Alibaba (China)', id: 'alibaba-cn' }]);
    // ...and the relay is found by the name its author gave it.
    fireEvent.change(screen.getByLabelText(en.settings.backends.opencodeSearchPlaceholder), { target: { value: '内部' } });
    expect(rows()).toEqual([{ title: '内部中转', id: 'dashscope' }]);

    // Including where it matters most: the capsule over the key field names the
    // endpoint the key is about to be written to.
    fireEvent.click(screen.getByRole('button', { name: /内部中转/ }));
    expect(await screen.findByLabelText(en.onboarding.connection.apiKeyLabel)).toBeTruthy();
    const capsule = screen.getByRole('button', { name: en.onboarding.connection.changeProvider }).closest('.connection-chosen');
    expect(capsule?.textContent).toContain('内部中转');
    expect(capsule?.textContent).not.toContain('Qwen');
  });

  it('never offers a local provider for an API Key, and follows OAuth capability for subscriptions', async () => {
    const entry = (id: string, name: string, patch: Partial<{ local: boolean; oauth_available: boolean }> = {}) =>
      ({ id, name, description: '', configured: false, oauth_available: false, local: false, models: [], ...patch });
    mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [
      entry('openai', 'OpenAI'), entry('ollama', 'Ollama', { local: true }), entry('github-copilot', 'GitHub Copilot', { oauth_available: true }),
    ] });
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'opencode', auth: 'none', ready: false, entry_eligible: false }));
    const { unmount } = render(wrap(<BackendConnectionDialog backend="opencode" method="api_key" onConnected={vi.fn()} onClose={vi.fn()} onWriteState={vi.fn()} />));
    const names = () => screen.getAllByRole('button').map((node) => node.querySelector('strong')?.textContent).filter(Boolean);
    await waitFor(() => expect(names()).toContain('OpenAI'));
    expect(names()).not.toContain('Ollama');
    unmount();

    render(wrap(<BackendConnectionDialog backend="opencode" method="oauth" onConnected={vi.fn()} onClose={vi.fn()} onWriteState={vi.fn()} />));
    // A subscription list is whatever can actually sign in, not the eight brands.
    await waitFor(() => expect(names()).toEqual(['GitHub Copilot']));
  });
});
