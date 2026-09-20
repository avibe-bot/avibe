// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentDetection } from '../steps/AgentDetection';
import { AssistantRow } from './AssistantRow';
import en from '../../i18n/en.json';
import type { BackendConnectionState } from '../../context/ApiContext';
import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';

const mock = vi.hoisted(() => ({ api: {
  detectCli: vi.fn(), installAgent: vi.fn(), getConfig: vi.fn(), getBackendRuntime: vi.fn(), getBackendConnection: vi.fn(), mutateConfig: vi.fn(), getClaudeAuth: vi.fn(), getCodexAuth: vi.fn(), getOpencodeProviders: vi.fn(),
} }));
vi.mock('../../context/ApiContext', () => ({ useApi: () => mock.api }));
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('../settings/models/useModelHubCapability', () => ({ useModelHubCapability: () => false }));
vi.mock('../settings/shared/useOpencodePermission', () => ({ useOpencodePermission: () => ({ permissionAllowed: true, statusLoaded: true }) }));
vi.mock('../settings/providers/BackendProviderConfig', () => ({ BackendProviderConfig: ({ backend }: { backend: string }) => <div>Existing provider: {backend}</div> }));
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const LocationProbe = () => <span data-testid="location">{useLocation().pathname}</span>;
const wrap = (node: React.ReactNode, active = true) => <MemoryRouter><I18nextProvider i18n={i18n}><RouteSurfaceActiveContext.Provider value={active}>{node}</RouteSurfaceActiveContext.Provider><LocationProbe /></I18nextProvider></MemoryRouter>;
const data = () => ({ __onboardingDetected: true, agents: Object.fromEntries(['claude', 'codex', 'opencode'].map((name) => [name, { enabled: true, cli_path: name, status: 'missing' }])) });
const row = (name: string) => within(screen.getByLabelText(name));
beforeEach(() => {
  vi.resetAllMocks();
  mock.api.getConfig.mockResolvedValue(data());
  mock.api.mutateConfig.mockResolvedValue({});
  mock.api.getBackendConnection.mockImplementation((backend) => Promise.resolve({ ok: true, backend, installed: true, enabled: true, auth: 'api_key', application: 'applied', ready: true, entry_eligible: true }));
  mock.api.getClaudeAuth.mockResolvedValue({ ok: true, active_auth_mode: 'none' });
  mock.api.getCodexAuth.mockResolvedValue({ ok: true, active_auth_mode: 'none' });
  mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [] });
  mock.api.detectCli.mockResolvedValue({ found: true, path: '/isolated/bin/assistant' });
  mock.api.getBackendRuntime.mockResolvedValue({ installed: true, has_update: false });
});
afterEach(cleanup);

describe('assistant installation presentation', () => {
  it('keeps fixed row order, independent installs and retry details', async () => {
    let finishClaude!: (value: unknown) => void;
    mock.api.installAgent.mockImplementation((name) => name === 'claude'
      ? new Promise((resolve) => { finishClaude = resolve; })
      : Promise.resolve({ ok: true, path: '/isolated/bin/codex' }));
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} />));
    // The three cards are the whole section: the design keeps each assistant's name at
    // the top of its own card and gives the section no heading of its own.
    expect(screen.getAllByRole('heading', { level: 3 }).map((node) => node.textContent)).toEqual(['Claude Code', 'Codex', 'OpenCode']);
    // "Connection state replaces roles with enable switches": the switch belongs to the
    // same identity header as the name, not to a separate strip.
    for (const name of ['Claude Code', 'Codex', 'OpenCode']) {
      const identity = screen.getByRole('heading', { name }).closest('.onboarding-card-identity')!;
      expect(within(identity as HTMLElement).getByRole('switch', { name: `Enable ${name}` })).toBeTruthy();
    }
    fireEvent.click(row('Claude Code').getByRole('button', { name: 'Install' }));
    fireEvent.click(row('Codex').getByRole('button', { name: 'Install' }));
    await waitFor(() => expect(row('Codex').getByRole('button', { name: 'Installed' })).toBeTruthy());
    await act(async () => finishClaude({ ok: false, message: 'Network unavailable', output: 'Installer exited 1' }));
    expect(row('Claude Code').getByText('Network unavailable')).toBeTruthy();
    expect(row('Claude Code').getByText('View details')).toBeTruthy();
    expect(row('Codex').getByRole('button', { name: 'Installed' })).toBeTruthy();
    expect(row('OpenCode').getByText('Not installed')).toBeTruthy();
    mock.api.installAgent.mockResolvedValue({ ok: true, path: '/isolated/bin/claude' });
    fireEvent.click(row('Claude Code').getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(row('Claude Code').getByRole('button', { name: 'Installed' })).toBeTruthy());
    expect(mock.api.detectCli).toHaveBeenCalledWith('/isolated/bin/claude');
    expect(row('Codex').getByRole('button', { name: 'Installed' })).toBeTruthy();
  });
  it('a failed install settlement refreshes connection state without admitting stale readiness', async () => {
    mock.api.getBackendConnection.mockImplementation(async (backend) => ({ ok: true, backend, installed: false, enabled: true, auth: 'none', application: 'applied', ready: false, entry_eligible: false }));
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} />));
    await waitFor(() => expect(mock.api.getBackendConnection).toHaveBeenCalledTimes(3));
    mock.api.installAgent.mockResolvedValue({ ok: false, path: '/fixture/new-cli', message: 'fixture apply failed' });
    mock.api.getBackendConnection.mockImplementation(async (backend) => ({ ok: true, backend, installed: true, enabled: true, auth: 'api_key', application: 'failed', ready: false, entry_eligible: false }));
    fireEvent.click(row('Claude Code').getByRole('button', { name: 'Install' }));
    await waitFor(() => expect(mock.api.getBackendConnection).toHaveBeenCalledTimes(4));
    expect(screen.getByRole('button', { name: 'Enter workspace' }).hasAttribute('disabled')).toBe(true);
    expect(mock.api.installAgent).toHaveBeenCalledOnce(); expect(mock.api.mutateConfig).not.toHaveBeenCalled();
  });
  it('keeps existing configure entry and does not claim connection from installation', async () => {
    const saved = data(); saved.agents.claude.status = 'ok';
    render(wrap(<AgentDetection data={saved} onNext={vi.fn()} />));
    expect(screen.queryByText('Subscription connected')).toBeNull();
    expect(screen.queryByText('API Key connected')).toBeNull();
    fireEvent.click(row('Claude Code').getByRole('button', { name: /Add subscription|API Key connected|Subscription connected/ }));
    expect(await screen.findByRole('dialog')).toBeTruthy();
  });
  it('refreshes connection readiness when a retained settings surface returns', async () => {
    const saved = data(); saved.agents.claude.status = 'ok';
    mock.api.getBackendConnection.mockImplementation(async (backend) => ({
      ok: true,
      backend,
      installed: true,
      enabled: true,
      auth: 'none',
      application: 'applied',
      ready: false,
      entry_eligible: false,
    }));
    const view = render(wrap(<AgentDetection data={saved} onNext={vi.fn()} />, false));
    await waitFor(() => expect(mock.api.getBackendConnection).toHaveBeenCalledTimes(3));
    mock.api.getBackendConnection.mockImplementation(async (backend) => ({
      ok: true,
      backend,
      installed: true,
      enabled: true,
      auth: 'api_key',
      application: 'applied',
      ready: true,
      entry_eligible: true,
    }));
    view.rerender(wrap(<AgentDetection data={saved} onNext={vi.fn()} />, true));
    await waitFor(() => expect(mock.api.getBackendConnection).toHaveBeenCalledTimes(6));
    expect(await row('Claude Code').findByRole('button', { name: 'API Key connected' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Enter workspace' }).hasAttribute('disabled')).toBe(false);
  });
  it('opens Model Hub directly when the backend is already Hub-owned', async () => {
    const saved = data(); saved.agents.claude.status = 'ok';
    mock.api.getBackendConnection.mockImplementation(async (backend) => ({
      ok: true,
      backend,
      installed: true,
      enabled: true,
      auth: 'none',
      application: 'applied',
      ready: false,
      entry_eligible: false,
      supply_mode: 'hub',
    }));
    render(wrap(<AgentDetection data={saved} onNext={vi.fn()} />));
    const action = await row('Claude Code').findByRole('button', { name: en.settings.backends.openModelHub });
    fireEvent.click(action);
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/settings/models'));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('button', { name: 'Enter workspace' }).hasAttribute('disabled')).toBe(true);
  });
  it('an available update keeps Continue and configuration usable', async () => {
    const saved = data(); saved.agents.claude.status = 'ok';
    mock.api.getBackendRuntime.mockResolvedValue({ installed: true, has_update: true, current_version: '1', latest_version: '2' });
    const next = vi.fn();
    render(wrap(<AgentDetection data={saved} onNext={next} />));
    await screen.findByRole('button', { name: en.backendLifecycle.statusUpdateAvailable });
    expect(row('Claude Code').getByRole('button', { name: /Add subscription|API Key connected|Subscription connected/ }).hasAttribute('disabled')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Enter workspace' }));
    await waitFor(() => expect(next).toHaveBeenCalled());
    expect(mock.api.installAgent).not.toHaveBeenCalled();
  });
  it('retains a detection error separately from missing installation', async () => {
    mock.api.detectCli.mockRejectedValue(new Error('Probe failed'));
    render(wrap(<AgentDetection data={{ ...data(), __onboardingDetected: false }} onNext={vi.fn()} />));
    await waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(3));
    expect(row('Claude Code').queryByRole('button', { name: 'Install' })).toBeNull();
    expect(row('Claude Code').getByRole('button', { name: 'Retry' })).toBeTruthy();
  });
  it('returns current row state to Welcome without saving it', async () => {
    mock.api.installAgent.mockResolvedValue({ ok: true, path: '/isolated/bin/claude' });
    const back = vi.fn();
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} onBack={back} />));
    fireEvent.click(row('Claude Code').getByRole('button', { name: 'Install' }));
    await waitFor(() => expect(row('Claude Code').getByRole('button', { name: 'Installed' })).toBeTruthy());
    fireEvent.click(row('Codex').getByRole('switch'));
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(back).toHaveBeenCalledWith({ agents: expect.objectContaining({
      claude: expect.objectContaining({ status: 'ok', cli_path: '/isolated/bin/assistant' }),
      codex: expect.objectContaining({ enabled: false }),
    }) });
  });
  it('reconciles saved non-ASCII CLI paths before navigation resumes', async () => {
    let finishConfig!: (value: unknown) => void;
    let finishDetection!: (value: unknown) => void;
    mock.api.getConfig.mockImplementation(() => new Promise((resolve) => { finishConfig = resolve; }));
    mock.api.detectCli.mockImplementation(() => new Promise((resolve) => { finishDetection = resolve; }));
    const saved = data(); saved.agents.claude.status = 'ok';
    const back = vi.fn();
    render(wrap(<AgentDetection data={saved} onNext={vi.fn()} onBack={back} />));
    fireEvent.click(row('Claude Code').getByRole('button', { name: /Add subscription|API Key connected/ }));
    fireEvent.keyDown(await screen.findByRole('dialog'), { key: 'Escape' });
    expect(screen.getByRole('button', { name: 'Back' }).hasAttribute('disabled')).toBe(true);
    await act(async () => finishConfig({ agents: { claude: { enabled: true, cli_path: '/isolated/新的路径/claude' } } }));
    expect(mock.api.detectCli).toHaveBeenCalledWith('/isolated/新的路径/claude');
    expect(screen.getByRole('button', { name: 'Back' }).hasAttribute('disabled')).toBe(true);
    await act(async () => finishDetection({ found: true, path: '/isolated/真实路径/claude' }));
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(back).toHaveBeenCalledWith({ agents: expect.objectContaining({ claude: expect.objectContaining({ cli_path: '/isolated/真实路径/claude', status: 'ok' }) }) });
  });
  it('clears only obsolete install failures after a successful external rescan', async () => {
    mock.api.installAgent.mockImplementation((name) => Promise.resolve({ ok: false, message: `Failed ${name}`, output: `Details ${name}` }));
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} />));
    for (const label of ['Claude Code', 'Codex', 'OpenCode']) {
      fireEvent.click(row(label).getByRole('button', { name: 'Install' }));
    }
    await waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(3));
    mock.api.detectCli.mockImplementation((name) => name === 'claude'
      ? Promise.resolve({ found: true, path: '/isolated/external/claude' })
      : name === 'codex' ? Promise.resolve({ found: false }) : Promise.reject(new Error('Probe unavailable')));
    fireEvent.click(screen.getByRole('button', { name: en.agentDetection.rescan }));
    await waitFor(() => expect(row('Claude Code').getByRole('button', { name: 'Installed' })).toBeTruthy());
    expect(row('Claude Code').queryByRole('alert')).toBeNull();
    expect(row('Claude Code').queryByText('Details claude')).toBeNull();
    expect(row('Codex').getByText('Failed codex')).toBeTruthy();
    expect(row('Codex').getByText('Details codex')).toBeTruthy();
    expect(row('OpenCode').getByText('Error: Probe unavailable')).toBeTruthy();
    expect(row('OpenCode').queryByRole('button', { name: 'Installed' })).toBeNull();
    // A rejected probe retains install evidence; a later missing result shows it again.
    mock.api.detectCli.mockResolvedValue({ found: false });
    fireEvent.click(row('OpenCode').getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(row('OpenCode').getByText('Failed opencode')).toBeTruthy());
    expect(row('OpenCode').getByText('Details opencode')).toBeTruthy();
    expect(row('Codex').getByText('Failed codex')).toBeTruthy();
  });
  it('keeps another backend installation independent of provider reconciliation', async () => {
    let finishConfig!: (value: unknown) => void;
    let finishInstall!: (value: unknown) => void;
    mock.api.getConfig.mockImplementation(() => new Promise((resolve) => { finishConfig = resolve; }));
    mock.api.installAgent.mockImplementation(() => new Promise((resolve) => { finishInstall = resolve; }));
    const saved = data(); saved.agents.claude.status = 'ok';
    render(wrap(<AgentDetection data={saved} onNext={vi.fn()} />));
    fireEvent.click(row('Claude Code').getByRole('button', { name: /Add subscription|API Key connected|Subscription connected/ }));
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    fireEvent.click(row('Codex').getByRole('button', { name: 'Install' }));
    expect(mock.api.installAgent).toHaveBeenCalledWith('codex');
    await act(async () => finishInstall({ ok: true, path: '/isolated/bin/codex' }));
    expect(row('Codex').getByRole('button', { name: 'Installed' })).toBeTruthy();
    expect(row('Codex').getByRole('button', { name: /Add subscription|API Key connected|Subscription connected/ }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('button', { name: 'Enter workspace' }).hasAttribute('disabled')).toBe(true);
    await act(async () => finishConfig(data()));
    expect(screen.getByRole('button', { name: 'Enter workspace' }).hasAttribute('disabled')).toBe(false);
    expect(row('Codex').getByRole('button', { name: 'Installed' })).toBeTruthy();
  });
  it('does not enable entry from installed, draining, failed or unknown states', async () => {
    for (const application of ['draining', 'failed', 'unknown']) {
      mock.api.getBackendConnection.mockResolvedValue({ ok: true, auth: 'api_key', application, ready: false, entry_eligible: false });
      const saved = data(); saved.agents.claude.status = 'ok';
      const { unmount } = render(wrap(<AgentDetection data={saved} onNext={vi.fn()} />));
      await act(async () => {});
      expect(screen.getByRole('button', { name: 'Enter workspace' }).hasAttribute('disabled')).toBe(true);
      expect(screen.queryByText('API Key connected')).toBeNull();
      unmount();
    }
  });
  it('renders connected controls only from explicit presentation state', () => {
    const configure = vi.fn();
    const connectedRow = (configuringDisabled: boolean) => wrap(<AssistantRow backend="claude" status="ok" installing={false} detecting={false} lifecycle={<span>Installed</span>}
      enabledControl={null} onInstall={vi.fn()} onDetect={vi.fn()} onConfigure={configure} connection="subscription" configuringDisabled={configuringDisabled} />);
    const { rerender } = render(connectedRow(true));
    fireEvent.click(screen.getByRole('button', { name: 'Subscription connected' }));
    expect(configure).not.toHaveBeenCalled();
    rerender(connectedRow(false));
    fireEvent.click(screen.getByRole('button', { name: 'Subscription connected' }));
    expect(configure).toHaveBeenCalledOnce();
  });
});

const pending = <T,>() => { let resolve!: (value: T) => void; let reject!: (reason: Error) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const stateFor = (backend: string, enabled = true, application: BackendConnectionState['application'] = 'applied') => ({ ok: true, backend, enabled, installed: true, auth: 'api_key', application, ready: backend === 'claude' && enabled && application === 'applied', entry_eligible: backend === 'claude' && enabled && application === 'applied' });
/** The enable control is a `role="switch"` button, so its state is its aria-checked. */
const checkedOf = (node: HTMLElement) => node.getAttribute('aria-checked') === 'true';
describe('settled wizard enablement follows persistence and latest intent', () => {
  function mountReady() {
    const saved = data(); saved.agents.claude.status = 'ok';
    mock.api.getBackendConnection.mockImplementation(async (name) => stateFor(name));
    render(wrap(<AgentDetection data={saved} onNext={vi.fn()} />));
    return row('Claude Code').getByRole('switch') as HTMLElement;
  }
  it.each(['rejected', 'committed-failed', 'unreadable'] as const)('reconciles a single toggle (%s) and Retry repairs enabled without another write', async (outcome) => {
    const checkbox = mountReady();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enter workspace' }).hasAttribute('disabled')).toBe(false));
    if (outcome === 'committed-failed') {
      mock.api.mutateConfig.mockResolvedValue({ agents: { claude: { enabled: false } }, agent_backend_runtime: { hot_reconciled: false, restart_error: 'fixture apply failure' } });
      mock.api.getBackendConnection.mockImplementation(async (name) => stateFor(name, false, 'failed'));
    } else {
      mock.api.mutateConfig.mockRejectedValue(new Error('fixture persist failure'));
      if (outcome === 'unreadable') mock.api.getBackendConnection.mockRejectedValue(new Error('fixture read failure'));
    }
    fireEvent.click(checkbox);
    await row('Claude Code').findByRole('alert');
    expect(checkedOf(checkbox)).toBe(outcome === 'rejected');
    expect(screen.getByRole('button', { name: 'Enter workspace' }).hasAttribute('disabled')).toBe(true);
    mock.api.getBackendConnection.mockImplementation(async (name) => stateFor(name));
    fireEvent.click(row('Claude Code').getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(checkedOf(checkbox)).toBe(true));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enter workspace' }).hasAttribute('disabled')).toBe(false));
    expect(mock.api.mutateConfig).toHaveBeenCalledOnce();
  });
  it('an older rejected off cannot replace queued on intent or accept a read started before the toggle', async () => {
    const checkbox = mountReady(); await row('Claude Code').findByRole('button', { name: 'API Key connected' });
    const oldRead = pending<ReturnType<typeof stateFor>>();
    mock.api.getBackendConnection.mockImplementation((name) => name === 'claude' ? oldRead.promise : Promise.resolve(stateFor(name)));
    fireEvent.click(screen.getByRole('button', { name: en.agentDetection.rescan }));
    await waitFor(() => expect(mock.api.detectCli).toHaveBeenCalledWith('claude'));
    const off = pending<unknown>(); const on = pending<unknown>();
    mock.api.mutateConfig.mockReturnValueOnce(off.promise).mockReturnValueOnce(on.promise);
    fireEvent.click(checkbox); fireEvent.click(checkbox);
    await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledOnce());
    await act(async () => oldRead.resolve(stateFor('claude', false)));
    expect(checkedOf(checkbox)).toBe(true);
    await act(async () => off.reject(new Error('old off rejected')));
    expect(checkedOf(checkbox)).toBe(true);
    expect(screen.getByRole('button', { name: 'Enter workspace' }).hasAttribute('disabled')).toBe(true);
    mock.api.getBackendConnection.mockImplementation(async (name) => stateFor(name));
    await act(async () => on.resolve({ agents: { claude: { enabled: true } } }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enter workspace' }).hasAttribute('disabled')).toBe(false));
    expect(checkedOf(checkbox)).toBe(true); expect(mock.api.mutateConfig).toHaveBeenCalledTimes(2);
  });
  it('delayed modal config cannot undo a newer authoritative enablement result', async () => {
    const checkbox = mountReady(); await row('Claude Code').findByRole('button', { name: 'API Key connected' });
    const config = pending<unknown>(); mock.api.getConfig.mockReturnValue(config.promise);
    fireEvent.click(row('Claude Code').getByRole('button', { name: 'API Key connected' }));
    fireEvent.keyDown(await screen.findByRole('dialog'), { key: 'Escape' });
    mock.api.getBackendConnection.mockImplementation(async (name) => stateFor(name, false));
    mock.api.mutateConfig.mockResolvedValue({ agents: { claude: { enabled: false } } });
    fireEvent.click(checkbox);
    await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledOnce());
    await act(async () => config.resolve(data()));
    expect(checkedOf(checkbox)).toBe(false);
    expect(screen.getByRole('button', { name: 'Enter workspace' }).hasAttribute('disabled')).toBe(true);
  });
});
