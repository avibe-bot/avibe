// @vitest-environment jsdom
import { StrictMode } from 'react';
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
import type { SetupAction } from './setupFlow';

const mock = vi.hoisted(() => ({ api: {
  detectCli: vi.fn(), installAgent: vi.fn(), getConfig: vi.fn(), getBackendRuntime: vi.fn(), getBackendConnection: vi.fn(), mutateConfig: vi.fn(), getClaudeAuth: vi.fn(), getCodexAuth: vi.fn(), getOpencodeProviders: vi.fn(), saveClaudeAuth: vi.fn(),
  listVibeAgents: vi.fn(), getVibeAgent: vi.fn(),
}, models: { getAgentChain: vi.fn(), previewAgentChain: vi.fn(), putAgentChain: vi.fn(), listSources: vi.fn() } }));
vi.mock('../../context/ApiContext', () => ({ useApi: () => mock.api }));
vi.mock('../settings/models/modelsApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../settings/models/modelsApi')>();
  return { ...actual, modelsApi: { ...actual.modelsApi, ...mock.models } };
});
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
// The shell's own adapter: every screen stays mounted, Back is a deactivation rather
// than an unmount, and the screen publishes its action instead of drawing one. The
// cases below go through that adapter because the retained-screen bugs only exist there.
const next = vi.fn();
const shown = (saved: ReturnType<typeof data>, active: boolean, onActionChange: (action: SetupAction) => void) =>
  wrap(<AgentDetection data={saved} active={active} onActionChange={onActionChange} onNext={next} />, active);
const configureAction = () => row('Claude Code').getByRole('button', { name: /Add subscription|API Key connected|Subscription connected/ });
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
  mock.api.listVibeAgents.mockResolvedValue({ ok: true, agents: [], default_agent_name: null });
  mock.api.getVibeAgent.mockResolvedValue({ ok: false, agent: null });
  mock.models.listSources.mockResolvedValue([]);
  mock.models.getAgentChain.mockRejectedValue(new Error('chain unread'));
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
    // The method rows disable while the initial connection read is in flight, so
    // the click that opens the dialog has to wait for the read to settle.
    await waitFor(() => expect(row('Claude Code').getByRole('button', { name: /Add subscription|API Key connected|Subscription connected/ }).hasAttribute('disabled')).toBe(false));
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
  it('opens the setup route editor when the backend is already Hub-owned', async () => {
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
    const agent = {
      id: 'claude-claude', name: 'claude', display_name: 'claude', description: null, backend: 'claude',
      model: 'opus-5', reasoning_effort: null, enabled: true, archived: false, archived_at: null, source: 'file',
      updated_at: '', system_prompt: null, created_at: '', metadata: { builtin_default: true },
    };
    mock.api.listVibeAgents.mockResolvedValue({ ok: true, agents: [agent], default_agent_name: 'claude' });
    mock.api.getVibeAgent.mockResolvedValue({ ok: true, agent });
    mock.models.listSources.mockResolvedValue([]);
    mock.models.getAgentChain.mockResolvedValue({
      contract_version: 10, backend: 'claude', model_id: 'opus-5',
      manual_override: { hops: [{ source_id: 'src_a', model_id: 'opus-5' }] },
      route_origin: 'manual', current: { source_id: 'src_a', model_id: 'opus-5' },
      chain: [{ source_id: 'src_a', model_id: 'opus-5', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }],
      supply_state: 'ok',
    });
    const flowState = { providerSelection: { scan: null, selectedBackends: [] }, importedCount: 0, addedThroughMore: [], routeOrder: [], routeOrderDirty: false };
    const agentReads = {
      read: async () => ({ kind: 'current' as const, value: [{ backend: 'claude' as const, cli_present: true, mode: 'hub' as const, menu_kind: 'fixed' as const, named_agents: [{ name: 'claude', effective_model_id: 'opus-5', supply_status: 'ok' as const }] }] }),
      refresh: async () => ({ kind: 'current' as const, value: [] }),
      readValue: async () => [],
      invalidate: () => undefined,
    };
    render(wrap(<AgentDetection data={saved} onNext={vi.fn()} flowState={flowState} setFlowState={vi.fn()} onNavigate={vi.fn()} agentReads={agentReads} />));
    const action = await row('Claude Code').findByRole('button', { name: en.onboarding.setup.configureRoute });
    const enter = screen.getByRole('button', { name: 'Enter workspace' });
    expect(enter.hasAttribute('disabled')).toBe(true);
    fireEvent.click(action);
    expect(await screen.findByRole('dialog', { name: en.onboarding.route.title })).toBeTruthy();
    expect(screen.getByTestId('location').textContent).toBe('/');
    expect(enter.hasAttribute('disabled')).toBe(true);
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
    await waitFor(() => expect(row('Claude Code').getByRole('button', { name: /Add subscription|API Key connected/ }).hasAttribute('disabled')).toBe(false));
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
    await waitFor(() => expect(row('Claude Code').getByRole('button', { name: /Add subscription|API Key connected|Subscription connected/ }).hasAttribute('disabled')).toBe(false));
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
  it('settles an enable write that lands while the screen is away, and reads nothing from hiding', async () => {
    const saved = data(); saved.agents.claude.status = 'ok';
    // The persisted value the uncached projection would report, moved by the write.
    let persisted = false;
    mock.api.getBackendConnection.mockImplementation(async (backend: string) => ({
      ok: true, backend, installed: true, auth: 'api_key', application: 'applied',
      enabled: backend === 'claude' ? persisted : true,
      ready: backend === 'claude' && persisted,
      entry_eligible: backend === 'claude' && persisted,
    }));
    let finishWrite!: (value: unknown) => void;
    const actions: SetupAction[] = [];
    const publish = (action: SetupAction) => { actions.push(action); };
    const view = render(shown(saved, false, publish));
    view.rerender(shown(saved, true, publish));
    const enable = () => row('Claude Code').getByRole('switch');
    await waitFor(() => expect(enable().getAttribute('aria-checked')).toBe('false'));
    await waitFor(() => expect(actions.at(-1)?.disabled).toBe(true));

    mock.api.mutateConfig.mockImplementation(() => new Promise((resolve) => { finishWrite = resolve; }));
    fireEvent.click(enable());
    await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledOnce());

    // Back, while the write the person asked for is still in flight.
    view.rerender(shown(saved, false, publish));
    const readsWhileHidden = mock.api.getBackendConnection.mock.calls.length;
    const publishedWhileHidden = actions.length;
    persisted = true;
    await act(async () => { finishWrite({ ok: true }); });
    // A screen nobody is reading owns no side effects: settling is bookkeeping only.
    expect(mock.api.getBackendConnection.mock.calls.length).toBe(readsWhileHidden);
    expect(actions.length).toBe(publishedWhileHidden);

    // Returning pays what that write owed, instead of latching the row pending behind
    // an intent check that can never pass again.
    view.rerender(shown(saved, true, publish));
    await waitFor(() => expect(actions.at(-1)?.disabled).toBe(false));
    expect(enable().getAttribute('aria-checked')).toBe('true');
    expect(configureAction().hasAttribute('disabled')).toBe(false);
    expect(mock.api.mutateConfig).toHaveBeenCalledOnce();
  });

  it('reports an enable write that failed while the screen was away instead of losing it', async () => {
    const saved = data(); saved.agents.claude.status = 'ok';
    let failWrite!: (reason: Error) => void;
    const publish = vi.fn();
    const view = render(shown(saved, false, publish));
    view.rerender(shown(saved, true, publish));
    await waitFor(() => expect(configureAction().hasAttribute('disabled')).toBe(false));
    mock.api.mutateConfig.mockImplementation(() => new Promise((_resolve, reject) => { failWrite = reject; }));
    fireEvent.click(row('Claude Code').getByRole('switch'));
    await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledOnce());
    view.rerender(shown(saved, false, publish));
    await act(async () => { failWrite(new Error('fixture persist failure')); });
    view.rerender(shown(saved, true, publish));
    // The receipt is what the write actually did, and returning is when there is finally
    // someone to tell. Dropping it would leave a silent failure behind a clean read.
    expect((await row('Claude Code').findByRole('alert')).textContent).toContain('fixture persist failure');
  });

  it('probes the path this screen learned, not the parent snapshot, when it is shown again', async () => {
    const saved = data();
    saved.agents.claude.cli_path = '/stale/claude';
    // Only the retained path resolves, so a probe that fell back to the snapshot is
    // visible twice over: in the call log, and as a backend reported missing again.
    mock.api.detectCli.mockImplementation((binary: string) => Promise.resolve(
      binary === '/retained/claude' ? { found: true, path: '/retained/claude' } : { found: false }));
    mock.api.installAgent.mockResolvedValue({ ok: true, path: '/retained/claude', message: '' });
    const probes = (binary: string) => mock.api.detectCli.mock.calls.filter(([called]) => called === binary).length;
    const publish = vi.fn();
    const view = render(shown(saved, false, publish));
    view.rerender(shown(saved, true, publish));
    // The first showing does adopt the snapshot: Welcome may have detected after this
    // retained screen mounted. That is the one probe the stale path ever gets.
    await waitFor(() => expect(probes('/stale/claude')).toBe(1));
    fireEvent.click(row('Claude Code').getByRole('button', { name: 'Install' }));
    await waitFor(() => expect(row('Claude Code').getByRole('button', { name: 'Installed' })).toBeTruthy());
    const retainedProbes = probes('/retained/claude');

    view.rerender(shown(saved, false, publish));
    view.rerender(shown(saved, true, publish));
    await waitFor(() => expect(probes('/retained/claude')).toBe(retainedProbes + 1));
    expect(probes('/stale/claude')).toBe(1);
    expect(row('Claude Code').getByRole('button', { name: 'Installed' })).toBeTruthy();
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
  // XpVM: what a settled write decided is owed to this screen until somebody who can
  // answer for it reports it. A read starting is not that somebody.
  const failedToggle = async (checkbox: HTMLElement) => {
    mock.api.mutateConfig.mockRejectedValue(new Error('fixture persist failure'));
    fireEvent.click(checkbox);
    expect((await row('Claude Code').findByRole('alert')).textContent).toContain('fixture persist failure');
  };
  const cardAlert = () => row('Claude Code').queryByRole('alert')?.textContent ?? null;
  it('a read that started before the write cannot clear the verdict it never saw', async () => {
    const checkbox = mountReady();
    await row('Claude Code').findByRole('button', { name: 'API Key connected' });
    const early = pending<ReturnType<typeof stateFor>>();
    mock.api.getBackendConnection.mockImplementation((name) => name === 'claude' ? early.promise : Promise.resolve(stateFor(name)));
    fireEvent.click(screen.getByRole('button', { name: en.agentDetection.rescan }));
    await waitFor(() => expect(mock.api.detectCli).toHaveBeenCalledWith('claude'));
    mock.api.getBackendConnection.mockImplementation(async (name) => stateFor(name));
    await failedToggle(checkbox);
    await act(async () => early.resolve(stateFor('claude')));
    expect(cardAlert()).toContain('fixture persist failure');
  });
  it('competing refreshes settling in reverse order each still report the write verdict', async () => {
    const checkbox = mountReady();
    await row('Claude Code').findByRole('button', { name: 'API Key connected' });
    const slow = pending<ReturnType<typeof stateFor>>();
    let reads = 0;
    mock.api.getBackendConnection.mockImplementation((name) => {
      if (name !== 'claude') return Promise.resolve(stateFor(name));
      reads += 1;
      return reads === 1 ? slow.promise : Promise.resolve(stateFor(name));
    });
    await failedToggle(checkbox);
    await waitFor(() => expect(reads).toBe(1));
    // A second ordinary refresh overtakes the first and answers for the same write.
    fireEvent.click(screen.getByRole('button', { name: en.agentDetection.rescan }));
    await waitFor(() => expect(reads).toBe(2));
    expect(cardAlert()).toContain('fixture persist failure');
    await act(async () => slow.resolve(stateFor('claude')));
    expect(cardAlert()).toContain('fixture persist failure');
    // Reported, not latched: the card is still offering the retry that answers it.
    await waitFor(() => expect(row('Claude Code').getByRole('button', { name: 'Retry' }).hasAttribute('disabled')).toBe(false));
  });
  it('only an acknowledged refresh retires the verdict', async () => {
    const checkbox = mountReady();
    await row('Claude Code').findByRole('button', { name: 'API Key connected' });
    await failedToggle(checkbox);
    const rescan = () => fireEvent.click(screen.getByRole('button', { name: en.agentDetection.rescan }));
    const probes = () => mock.api.detectCli.mock.calls.length;
    let seen = probes(); rescan();
    await waitFor(() => expect(probes()).toBeGreaterThan(seen));
    expect(cardAlert()).toContain('fixture persist failure');
    fireEvent.click(row('Claude Code').getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(cardAlert()).toBeNull());
    // Retired for good: an ordinary refresh afterwards does not bring it back.
    seen = probes(); rescan();
    await waitFor(() => expect(probes()).toBeGreaterThan(seen));
    expect(cardAlert()).toBeNull();
    expect(mock.api.mutateConfig).toHaveBeenCalledOnce();
  });
  // Lifecycle is not an answer. A dialog that only opened, a write that failed and
  // said so by reporting nothing pending, a read that failed, an acknowledgement that
  // lost the screen — each of these used to make an unresolved apply failure
  // disappear. They run through the shell adapter, which is the host the wizard uses.
  const owedInShell = async () => {
    const saved = data(); saved.agents.claude.status = 'ok';
    mock.api.getConfig.mockResolvedValue(saved);
    mock.api.getBackendConnection.mockImplementation(async (name) => stateFor(name));
    const publish = vi.fn();
    const view = render(shown(saved, false, publish));
    view.rerender(shown(saved, true, publish));
    await row('Claude Code').findByRole('button', { name: 'API Key connected' });
    await failedToggle(row('Claude Code').getByRole('switch') as HTMLElement);
    await waitFor(() => expect(configureAction().hasAttribute('disabled')).toBe(false));
    return { view, publish, saved };
  };
  const retryLink = () => row('Claude Code').getByRole('button', { name: 'Retry' });
  it('a provider dialog that only opened and closed answers for nothing', async () => {
    await owedInShell();
    fireEvent.click(row('Claude Code').getByRole('button', { name: /Add subscription/ }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    // Closing re-reads persisted config and the connection. Neither of them connected
    // anything, so the failure the person still has to deal with is still there.
    await waitFor(() => expect(configureAction().hasAttribute('disabled')).toBe(false));
    expect(cardAlert()).toContain('fixture persist failure');
    expect(retryLink().hasAttribute('disabled')).toBe(false);
  });
  it('a provider write that failed reports nothing pending, which answers for nothing', async () => {
    await owedInShell();
    fireEvent.click(row('Claude Code').getByRole('button', { name: 'Add API Key' }));
    const dialog = await screen.findByRole('dialog');
    const secret = await waitFor(() => dialog.querySelector('input[type="password"]') as HTMLInputElement);
    fireEvent.change(secret, { target: { value: 'sk-ant-fixture' } });
    mock.api.saveClaudeAuth.mockResolvedValue({ ok: false, message: 'fixture provider failure' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save and connect' }));
    await waitFor(() => expect(within(dialog).getByRole('alert').textContent).toContain('fixture provider failure'));
    await act(async () => {});
    // The card sits behind an open modal here, so its line is read from the document
    // rather than from the accessibility tree the dialog has taken over.
    expect(screen.getByLabelText('Claude Code').querySelector('.onboarding-assistant-error')?.textContent).toContain('fixture persist failure');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(configureAction().hasAttribute('disabled')).toBe(false));
    expect(cardAlert()).toContain('fixture persist failure');
  });
  it('an acknowledgement that lost the screen cannot clear what it no longer owns', async () => {
    const { view, publish, saved } = await owedInShell();
    const slow = pending<ReturnType<typeof stateFor>>();
    let reads = 0;
    mock.api.getBackendConnection.mockImplementation((name) => {
      if (name !== 'claude') return Promise.resolve(stateFor(name));
      reads += 1;
      return reads === 1 ? slow.promise : Promise.resolve(stateFor(name));
    });
    // Retry is the one caller here that may spend the verdict — until the person
    // leaves the screen under it and comes back to a different activation.
    fireEvent.click(retryLink());
    await waitFor(() => expect(reads).toBe(1));
    view.rerender(shown(saved, false, publish));
    view.rerender(shown(saved, true, publish));
    await waitFor(() => expect(reads).toBeGreaterThan(1));
    await act(async () => slow.resolve(stateFor('claude')));
    expect(cardAlert()).toContain('fixture persist failure');
    // And ordinary recovery still works: a Retry that does own the screen retires it.
    fireEvent.click(retryLink());
    await waitFor(() => expect(cardAlert()).toBeNull());
    expect(mock.api.mutateConfig).toHaveBeenCalledOnce();
  });
  it('an explicit Retry whose read failed keeps the verdict it could not answer', async () => {
    const { view, publish, saved } = await owedInShell();
    let reads = 0;
    mock.api.getBackendConnection.mockImplementation((name) => {
      if (name === 'claude') reads += 1;
      return Promise.reject(new Error('fixture read failure'));
    });
    fireEvent.click(retryLink());
    await waitFor(() => expect(reads).toBe(1));
    await act(async () => {});
    expect(cardAlert()).toContain('fixture persist failure');
    expect(cardAlert()).not.toContain('fixture read failure');
    // Still owed, and a healthy ordinary read on return proves it: had the failed
    // acknowledgement spent it, that read would have had nothing to report.
    mock.api.getBackendConnection.mockImplementation(async (name) => stateFor(name));
    view.rerender(shown(saved, false, publish));
    view.rerender(shown(saved, true, publish));
    await waitFor(() => expect(configureAction().hasAttribute('disabled')).toBe(false));
    expect(cardAlert()).toContain('fixture persist failure');
    fireEvent.click(retryLink());
    await waitFor(() => expect(cardAlert()).toBeNull());
  });
  it('a read that also failed does not bury what the write reported', async () => {
    const checkbox = mountReady();
    await row('Claude Code').findByRole('button', { name: 'API Key connected' });
    mock.api.getBackendConnection.mockRejectedValue(new Error('fixture read failure'));
    await failedToggle(checkbox);
    expect(cardAlert()).not.toContain('fixture read failure');
  });
  it('a successful read still reports the write that failed under it', async () => {
    const checkbox = mountReady();
    await row('Claude Code').findByRole('button', { name: 'API Key connected' });
    await failedToggle(checkbox);
    // The projection is healthy and says enabled; the apply underneath it was not.
    expect(checkedOf(checkbox)).toBe(true);
    expect(cardAlert()).toContain('fixture persist failure');
  });
  it('a StrictMode replay of the return neither loses nor doubles the verdict', async () => {
    const saved = data(); saved.agents.claude.status = 'ok';
    mock.api.getBackendConnection.mockImplementation(async (name) => stateFor(name));
    const publish = vi.fn();
    const strict = (active: boolean) => <StrictMode>{wrap(<AgentDetection data={saved} active={active} onActionChange={publish} onNext={vi.fn()} />, active)}</StrictMode>;
    const view = render(strict(false));
    view.rerender(strict(true));
    const checkbox = () => row('Claude Code').getByRole('switch');
    await waitFor(() => expect(checkedOf(checkbox())).toBe(true));
    let failWrite!: (reason: Error) => void;
    mock.api.mutateConfig.mockImplementation(() => new Promise((_resolve, reject) => { failWrite = reject; }));
    fireEvent.click(checkbox());
    await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledOnce());
    view.rerender(strict(false));
    await act(async () => { failWrite(new Error('fixture persist failure')); });
    view.rerender(strict(true));
    await waitFor(() => expect(cardAlert()).toContain('fixture persist failure'));
    expect(row('Claude Code').getAllByRole('alert')).toHaveLength(1);
    expect(mock.api.mutateConfig).toHaveBeenCalledOnce();
  });
  // The presence read is the last thing a queued enable does, and the only one that can
  // reject. A rejection used to settle the serial queue itself rejected, so every later
  // toggle chained onto a continuation that never ran: the next enable wrote nothing,
  // and nothing on the card said why. The write and the read after it answer separately.
  it('a presence read that failed is reported and still leaves the next toggle able to run', async () => {
    const saved = data(); saved.agents.claude.status = 'ok';
    let backendEnabled = true;
    mock.api.mutateConfig.mockImplementation(async () => { backendEnabled = !backendEnabled; return {}; });
    mock.api.getBackendConnection.mockImplementation(async (name) => stateFor(name, name === 'claude' ? backendEnabled : true));
    let presenceFails = true;
    const refresh = vi.fn(async () => {
      if (presenceFails) { presenceFails = false; throw new Error('fixture presence failure'); }
      return { kind: 'current' as const, value: [] };
    });
    const agentReads = { read: async () => ({ kind: 'current' as const, value: [] }), refresh, readValue: async () => [], invalidate: () => undefined };
    render(wrap(<AgentDetection data={saved} onNext={vi.fn()} agentReads={agentReads} />));
    const checkbox = row('Claude Code').getByRole('switch') as HTMLElement;
    const enter = screen.getByRole('button', { name: 'Enter workspace' });
    await waitFor(() => expect(enter.hasAttribute('disabled')).toBe(false));

    fireEvent.click(checkbox);
    expect((await row('Claude Code').findByRole('alert')).textContent).toContain('fixture presence failure');
    await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledOnce());
    expect(enter.hasAttribute('disabled')).toBe(true);

    // Still a queue. The next toggle writes, and the connection read it carries settles
    // the card — which only happens once the finished write released its pending enable.
    fireEvent.click(checkbox);
    await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(enter.hasAttribute('disabled')).toBe(false));
    expect(cardAlert()).toBeNull();
    expect(refresh).toHaveBeenCalledTimes(2);
    expect(checkedOf(checkbox)).toBe(true);
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
