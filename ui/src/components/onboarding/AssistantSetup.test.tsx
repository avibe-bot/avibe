// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentDetection } from '../steps/AgentDetection';
import { AssistantRow } from './AssistantRow';
import en from '../../i18n/en.json';

const mock = vi.hoisted(() => ({ api: {
  detectCli: vi.fn(), installAgent: vi.fn(), getConfig: vi.fn(), getBackendRuntime: vi.fn(),
} }));
vi.mock('../../context/ApiContext', () => ({ useApi: () => mock.api }));
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('../settings/models/useModelHubCapability', () => ({ useModelHubCapability: () => false }));
vi.mock('../settings/shared/useOpencodePermission', () => ({ useOpencodePermission: () => ({ permissionAllowed: true, statusLoaded: true }) }));
vi.mock('../settings/providers/BackendProviderConfig', () => ({ BackendProviderConfig: ({ backend }: { backend: string }) => <div>Existing provider: {backend}</div> }));
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const wrap = (node: React.ReactNode) => <MemoryRouter><I18nextProvider i18n={i18n}>{node}</I18nextProvider></MemoryRouter>;
const data = () => ({ __onboardingDetected: true, agents: Object.fromEntries(['claude', 'codex', 'opencode'].map((name) => [name, { enabled: true, cli_path: name, status: 'missing' }])) });
const row = (name: string) => within(screen.getByLabelText(name));
beforeEach(() => {
  vi.resetAllMocks();
  mock.api.getConfig.mockResolvedValue(data());
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
    expect(screen.getAllByRole('heading', { level: 3 }).map((node) => node.textContent)).toEqual(['AI assistants', 'Claude Code', 'Codex', 'OpenCode']);
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
  it('keeps existing configure entry and does not claim connection from installation', async () => {
    const saved = data(); saved.agents.claude.status = 'ok';
    render(wrap(<AgentDetection data={saved} onNext={vi.fn()} />));
    expect(screen.queryByText('Subscription connected')).toBeNull();
    expect(screen.queryByText('API Key connected')).toBeNull();
    fireEvent.click(row('Claude Code').getByRole('button', { name: en.agentDetection.configureProvider }));
    expect(await screen.findByText('Existing provider: claude')).toBeTruthy();
  });
  it('an available update keeps Continue and configuration usable', async () => {
    const saved = data(); saved.agents.claude.status = 'ok';
    mock.api.getBackendRuntime.mockResolvedValue({ installed: true, has_update: true, current_version: '1', latest_version: '2' });
    const next = vi.fn();
    render(wrap(<AgentDetection data={saved} onNext={next} />));
    await screen.findByRole('button', { name: en.backendLifecycle.statusUpdateAvailable });
    expect(row('Claude Code').getByRole('button', { name: en.agentDetection.configureProvider }).hasAttribute('disabled')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(next).toHaveBeenCalledWith({ agents: saved.agents });
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
    fireEvent.click(row('Codex').getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(back).toHaveBeenCalledWith({ agents: expect.objectContaining({
      claude: expect.objectContaining({ status: 'ok', cli_path: '/isolated/bin/assistant' }),
      codex: expect.objectContaining({ enabled: false }),
    }) });
  });
  it.each(['Back', 'Continue'])('serializes provider reconciliation and returns final live rows via %s', async (navigation) => {
    let finishConfig!: (value: unknown) => void;
    let finishDetection!: (value: unknown) => void;
    mock.api.getConfig.mockImplementation(() => new Promise((resolve) => { finishConfig = resolve; }));
    mock.api.detectCli.mockImplementation(() => new Promise((resolve) => { finishDetection = resolve; }));
    const back = vi.fn();
    const next = vi.fn();
    render(wrap(<AgentDetection data={data()} onNext={next} onBack={back} />));
    const assertSyncGates = () => {
      for (const backend of ['Claude Code', 'Codex', 'OpenCode']) {
        const configure = row(backend).getByRole('button', { name: en.agentDetection.configureProvider });
        expect(configure.hasAttribute('disabled')).toBe(true);
        fireEvent.click(configure);
        expect(screen.queryByRole('dialog')).toBeNull();
      }
      for (const name of ['Back', 'Continue']) {
        const button = screen.getByRole('button', { name });
        expect(button.hasAttribute('disabled')).toBe(true);
        fireEvent.click(button);
      }
      expect(back).not.toHaveBeenCalled();
      expect(next).not.toHaveBeenCalled();
    };
    const finalAgents = { ...data().agents };
    for (const [index, [backend, label]] of [['claude', 'Claude Code'], ['codex', 'Codex']].entries()) {
      fireEvent.click(row(label).getByRole('button', { name: en.agentDetection.configureProvider }));
      expect(screen.getByText(`Existing provider: ${backend}`)).toBeTruthy();
      fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
      assertSyncGates();
      expect(mock.api.getConfig).toHaveBeenCalledTimes(index + 1);
      expect(mock.api.detectCli).toHaveBeenCalledTimes(index);
      // Local enablement remains editable while persisted provider fields reload.
      fireEvent.click(row(label).getByRole('checkbox'));
      const savedPath = `/isolated/新的路径/${backend}`;
      const canonicalPath = `/isolated/真实路径/${backend}`;
      const provider = { default_provider: `provider-${backend}`, cli_path: savedPath, enabled: true };
      await act(async () => finishConfig({ agents: { [backend]: provider } }));
      expect(mock.api.detectCli).toHaveBeenLastCalledWith(savedPath);
      assertSyncGates();
      expect(mock.api.getConfig).toHaveBeenCalledTimes(index + 1);
      await act(async () => finishDetection({ found: true, path: canonicalPath }));
      expect(screen.getByRole('button', { name: 'Back' }).hasAttribute('disabled')).toBe(false);
      expect(screen.getByRole('button', { name: 'Continue' }).hasAttribute('disabled')).toBe(false);
      for (const name of ['Claude Code', 'Codex', 'OpenCode']) {
        expect(row(name).getByRole('button', { name: en.agentDetection.configureProvider }).hasAttribute('disabled')).toBe(false);
      }
      finalAgents[backend] = { ...provider, enabled: false, status: 'ok', cli_path: canonicalPath };
      // Consume after each cycle: a settled pre-detection snapshot must not be replayed.
      fireEvent.click(screen.getByRole('button', { name: navigation }));
      await waitFor(() => expect(navigation === 'Back' ? back : next).toHaveBeenCalledWith({ agents: finalAgents }));
      back.mockClear();
      next.mockClear();
    }
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
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} />));
    fireEvent.click(row('Claude Code').getByRole('button', { name: en.agentDetection.configureProvider }));
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    fireEvent.click(row('Codex').getByRole('button', { name: 'Install' }));
    expect(mock.api.installAgent).toHaveBeenCalledWith('codex');
    await act(async () => finishInstall({ ok: true, path: '/isolated/bin/codex' }));
    expect(row('Codex').getByRole('button', { name: 'Installed' })).toBeTruthy();
    expect(row('Codex').getByRole('button', { name: en.agentDetection.configureProvider }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('button', { name: 'Continue' }).hasAttribute('disabled')).toBe(true);
    await act(async () => finishConfig(data()));
    expect(screen.getByRole('button', { name: 'Continue' }).hasAttribute('disabled')).toBe(false);
    expect(row('Codex').getByRole('button', { name: 'Installed' })).toBeTruthy();
  });
  it('ignores an obsolete successful probe when a newer provider-path probe reports missing', async () => {
    let finishConfig!: (value: unknown) => void;
    let finishOldProbe!: (value: unknown) => void;
    mock.api.installAgent.mockResolvedValue({ ok: false, message: 'Install failed', output: 'Failure details' });
    mock.api.getConfig.mockImplementation(() => new Promise((resolve) => { finishConfig = resolve; }));
    mock.api.detectCli.mockImplementation((path) => path === 'claude'
      ? new Promise((resolve) => { finishOldProbe = resolve; }) : Promise.resolve({ found: false }));
    const back = vi.fn();
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} onBack={back} />));
    fireEvent.click(row('Claude Code').getByRole('button', { name: 'Install' }));
    await screen.findByText('Install failed');
    fireEvent.click(row('Claude Code').getByRole('button', { name: en.agentDetection.configureProvider }));
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: en.agentDetection.rescan }));
    expect(mock.api.detectCli).toHaveBeenCalledWith('claude');
    await act(async () => finishConfig({ agents: { claude: { cli_path: '/isolated/new/claude' } } }));
    expect(mock.api.detectCli).toHaveBeenCalledWith('/isolated/new/claude');
    await act(async () => finishOldProbe({ found: true, path: '/isolated/old/claude' }));
    expect(row('Claude Code').getByText('Install failed')).toBeTruthy();
    expect(row('Claude Code').getByText('Failure details')).toBeTruthy();
    expect(row('Claude Code').queryByRole('button', { name: 'Installed' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(back).toHaveBeenCalledWith({ agents: expect.objectContaining({ claude: expect.objectContaining({
      cli_path: '/isolated/new/claude', status: 'missing',
    }) }) });
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
