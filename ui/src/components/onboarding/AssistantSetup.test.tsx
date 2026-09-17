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
  it('renders connected controls only from explicit presentation state', () => {
    const configure = vi.fn();
    render(wrap(<AssistantRow backend="claude" status="ok" installing={false} detecting={false} lifecycle={<span>Installed</span>}
      enabledControl={null} onInstall={vi.fn()} onDetect={vi.fn()} onConfigure={configure} connection="subscription" />));
    fireEvent.click(screen.getByRole('button', { name: 'Subscription connected' }));
    expect(configure).toHaveBeenCalledOnce();
  });
});
