// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ClaudeProviderConfig } from '../settings/providers/ClaudeProviderConfig';
import { CodexProviderConfig } from '../settings/providers/CodexProviderConfig';
import { BackendConnectionForm } from '../settings/providers/BackendConnectionForm';
import { BackendConnectionDialog } from './BackendConnectionDialog';
import en from '../../i18n/en.json';
import type { BackendConnectionState } from '../../context/ApiContext';

const mock = vi.hoisted(() => ({ toast: vi.fn(), api: {
  getConfig: vi.fn(), detectCli: vi.fn(), mutateConfig: vi.fn(), getBackendRuntime: vi.fn(), claudeModels: vi.fn(), codexModels: vi.fn(),
  getClaudeAuth: vi.fn(), getCodexAuth: vi.fn(), getOpencodeProviders: vi.fn(),
  setOpencodeProviderAuth: vi.fn(), getBackendConnection: vi.fn(), saveClaudeAuth: vi.fn(), saveCodexAuth: vi.fn(),
  startOAuthWeb: vi.fn(), startOAuthWebForOpencodeProvider: vi.fn(),
  getOAuthWebStatus: vi.fn(), submitOAuthWebCode: vi.fn(), cancelOAuthWeb: vi.fn(),
  removeClaudeOAuthCredentials: vi.fn(), removeBackendAuth: vi.fn(),
} }));
vi.mock('../../context/ApiContext', () => ({ useApi: () => mock.api }));
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: mock.toast }) }));
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
const wrap = (node: React.ReactNode) => <I18nextProvider i18n={i18n}>{node}</I18nextProvider>;
const connection = (patch: Partial<BackendConnectionState> = {}): BackendConnectionState => ({ ok: true, backend: 'claude', installed: true, enabled: true, auth: 'api_key', application: 'applied', ready: true, entry_eligible: true, ...patch });
const native = () => ({ ok: true, active_auth_mode: 'api_key', has_api_key: true, api_key_masked: 'sk-•••old', base_url: 'https://old.example', credential_type: 'api_key', has_oauth_credentials: true });
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; };
beforeEach(() => {
  vi.resetAllMocks();
  mock.api.getClaudeAuth.mockResolvedValue(native());
  mock.api.getCodexAuth.mockResolvedValue(native());
  mock.api.getBackendConnection.mockImplementation(async (backend) => connection({ backend }));
  mock.api.saveClaudeAuth.mockResolvedValue({ ok: true, restart: { ok: true } });
  mock.api.cancelOAuthWeb.mockResolvedValue({ ok: true });
  mock.api.getConfig.mockResolvedValue({ agents: { claude: { enabled: false, cli_path: 'claude' }, codex: { enabled: false, cli_path: 'codex' } } });
  mock.api.detectCli.mockImplementation(async (binary) => ({ found: true, path: binary }));
  mock.api.getBackendRuntime.mockImplementation(async (backend) => ({ ok: true, name: backend, enabled: true, installed: true, process_status: 'running' }));
  mock.api.claudeModels.mockResolvedValue({ ok: true, models: [] });
  mock.api.codexModels.mockResolvedValue({ ok: true, models: [] });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

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
    mock.api.getCodexAuth.mockResolvedValue({ ...native(), auth_mode_uncertain: true });
    mock.api.getBackendConnection.mockResolvedValue(connection({ backend: 'codex', enabled: false, auth: 'unknown', ready: false, entry_eligible: false }));
    mock.api.saveCodexAuth.mockResolvedValue({ ok: true, restart: { ok: false, message: 'apply failed' } });
    const refreshed = vi.fn();
    render(wrap(<BackendConnectionForm backend="codex" initialMethod="api_key" onConnected={refreshed} />));
    fireEvent.click(await screen.findByRole('button', { name: en.common.save }));
    await screen.findByText('apply failed');
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
      fireEvent.click(screen.getByRole('button', { name: en.common.save, exact: true }));
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
      expect(mock.api.mutateConfig).toHaveBeenCalledWith([{ kind: 'set', path: ['agents', backend, 'enabled'], value: true }]);
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
    mock.api.getCodexAuth.mockResolvedValue({ ...native(), auth_mode_uncertain: true });
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
