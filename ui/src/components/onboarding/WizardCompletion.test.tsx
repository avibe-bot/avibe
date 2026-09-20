// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Wizard } from '../Wizard';
import en from '../../i18n/en.json';

const mock = vi.hoisted(() => ({ supply: vi.fn(), control: vi.fn(), toast: vi.fn(), permission: vi.fn(), manageAccess: true, api: {
  saveSettings: vi.fn(), discordAuthTest: vi.fn(), discordGuilds: vi.fn(), slackManifest: vi.fn(), slackAuthTest: vi.fn(), getConfig: vi.fn(), detectCli: vi.fn(), getBackendRuntime: vi.fn(), getBackendConnection: vi.fn(),
  getOpencodeProviders: vi.fn(), readOpencodeOptionsForModelPicker: vi.fn(), readModelHubAgentCatalogForModelPicker: vi.fn(), updateVibeAgent: vi.fn(), getVibeAgent: vi.fn(), listVibeAgents: vi.fn(), setDefaultVibeAgent: vi.fn(), mutateConfig: vi.fn(),
} }));
vi.mock('../../context/ApiContext', async (importOriginal) => ({ ...await importOriginal<typeof import('../../context/ApiContext')>(), useApi: () => mock.api }));
vi.mock('../settings/models/modelsApi', () => ({ modelsApi: { getAgentSources: mock.supply } }));
vi.mock('../../context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => ({ capabilities: { can_manage_agents: true, can_manage_access_members: mock.manageAccess } }) }));
vi.mock('../../context/StatusContext', () => ({ useStatus: () => ({ control: mock.control }) }));
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: mock.toast }) }));
vi.mock('../settings/models/useModelHubCapability', () => ({ useModelHubCapability: () => false }));
vi.mock('../settings/shared/useOpencodePermission', () => ({ useOpencodePermission: () => ({ permissionAllowed: false, statusLoaded: true, setupPermission: mock.permission }) }));
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
function Destination() { const location = useLocation(); return <div data-testid="destination">{JSON.stringify(location.state)}</div>; }
function mount() { return render(<MemoryRouter initialEntries={['/setup']}><I18nextProvider i18n={i18n}><Routes><Route path="/setup" element={<Wizard />} /><Route path="/" element={<Destination />} /></Routes></I18nextProvider></MemoryRouter>); }
let running: boolean;
beforeEach(() => {
  vi.resetAllMocks(); running = true; mock.manageAccess = true;
  mock.api.saveSettings.mockResolvedValue({ guild_allowlist: [] });
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  Element.prototype.scrollIntoView = vi.fn();
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
  mock.api.readModelHubAgentCatalogForModelPicker.mockResolvedValue(null);
  mock.api.getConfig.mockResolvedValue({ setup_completed: false, agents: { claude: { enabled: true }, codex: { enabled: true }, opencode: { enabled: true } }, platforms: { enabled: [] } });
  mock.api.detectCli.mockResolvedValue({ found: true, path: '/测试/bin/assistant' });
  mock.api.getBackendRuntime.mockResolvedValue({ installed: true, has_update: false });
  mock.api.getBackendConnection.mockImplementation((backend) => Promise.resolve({ ok: true, backend, enabled: true, installed: true, auth: backend === 'claude' ? 'api_key' : 'none', application: running ? 'applied' : 'stopped', ready: backend === 'claude' && running, entry_eligible: backend === 'claude', permission_required: backend === 'opencode' }));
  mock.api.listVibeAgents.mockResolvedValue({ ok: true, default_agent_name: 'missing-codex', agents: [{ name: 'claude-agent', backend: 'claude', enabled: true }] });
  mock.api.setDefaultVibeAgent.mockResolvedValue({ ok: true });
  mock.api.mutateConfig.mockResolvedValue({ setup_completed: true });
  mock.control.mockImplementation(async () => { running = true; return { ok: true }; });
});
afterEach(cleanup);
async function setup() {
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Get started' }));
  const enter = await screen.findByRole('button', { name: 'Enter workspace' });
  await waitFor(() => expect(enter.hasAttribute('disabled')).toBe(false));
  return enter;
}
describe('explicit any-one-ready completion', () => {
  it('keeps setup until explicit Enter, scopes OpenCode permission, chooses an available Agent, writes only completion', async () => {
    const enter = await setup();
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.queryByTestId('destination')).toBeNull();
    fireEvent.click(enter); fireEvent.click(enter);
    expect(await screen.findByTestId('destination')).toBeTruthy();
    expect(screen.getByTestId('destination').textContent).toBe('{"onboardingCompleted":true}');
    expect(mock.api.mutateConfig).toHaveBeenCalledExactlyOnceWith([{ kind: 'set', path: ['setup_completed'], value: true }]);
    expect(mock.api.setDefaultVibeAgent).toHaveBeenCalledExactlyOnceWith('claude-agent');
    expect(mock.permission).not.toHaveBeenCalled(); expect(mock.control).not.toHaveBeenCalled();
  });
  it('allows stopped entry and confirms start before committing setup', async () => {
    running = false;
    const enter = await setup(); fireEvent.click(enter);
    await screen.findByTestId('destination');
    expect(mock.control).toHaveBeenCalledExactlyOnceWith('start');
    expect(mock.control.mock.invocationCallOrder[0]).toBeLessThan(mock.api.mutateConfig.mock.invocationCallOrder[0]);
  });
  it('preserves an existing usable default', async () => {
    mock.api.listVibeAgents.mockResolvedValue({ ok: true, default_agent_name: 'mine', agents: [{ name: 'mine', backend: 'claude', enabled: true }] });
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
  });
  it.each(['start', 'legacy IM'])('keeps failed completion recoverable (%s)', async (failure) => {
    if (failure === 'start') { running = false; mock.control.mockRejectedValue(new Error('Start unavailable')); }
    else mock.api.mutateConfig.mockRejectedValue(new Error("Config 'slack.bot_token' must be provided"));
    fireEvent.click(await setup());
    await screen.findByRole('alert');
    expect(screen.queryByTestId('destination')).toBeNull();
    if (failure === 'start') expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    mock.control.mockImplementation(async () => { running = true; return { ok: true }; });
    mock.api.mutateConfig.mockResolvedValue({ setup_completed: true });
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Enter workspace' })));
    await screen.findByTestId('destination');
  });
  it('rechecks effective readiness at explicit entry instead of trusting a stale badge', async () => {
    const enter = await setup();
    mock.api.getBackendConnection.mockResolvedValue({ ok: true, ready: false, entry_eligible: false, application: 'failed' });
    fireEvent.click(enter); await screen.findByRole('alert');
    expect(mock.api.mutateConfig).not.toHaveBeenCalled(); expect(mock.control).not.toHaveBeenCalled();
  });
});

const opencodeAgent = { id: 'oc-1', name: 'opencode', display_name: 'OpenCode', backend: 'opencode', model: 'openai/gpt-5.6-sol', enabled: true, archived: false, source: 'builtin', reasoning_effort: 'high' };
function directConnection(provider: string) {
  let agent = { ...opencodeAgent };
  mock.api.getBackendConnection.mockImplementation((backend) => Promise.resolve({ ok: true, backend, enabled: true, installed: true, auth: 'api_key', application: 'applied', ready: backend === 'opencode', entry_eligible: backend === 'opencode' }));
  mock.api.listVibeAgents.mockImplementation(async () => ({ ok: true, default_agent_name: 'opencode', agents: [agent] }));
  mock.api.getVibeAgent.mockImplementation(async () => ({ ok: true, agent }));
  mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, default_provider: 'openai', providers: [{ id: 'openai', configured: true, active_auth_type: null, models: ['gpt-5.6-sol'] }, { id: provider, configured: true, active_auth_type: 'oauth', models: ['selected-model', 'my-model'] }] });
  mock.api.readOpencodeOptionsForModelPicker.mockResolvedValue({ ok: true, data: { models: { providers: [{ id: provider, models: { 'selected-model': {} } }, { id: 'openai', models: { 'gpt-5.6-sol': {} } }] }, reasoning_options: { [`${provider}/selected-model`]: [] } } });
  mock.api.updateVibeAgent.mockImplementation(async (_name, patch) => { agent = { ...agent, ...patch }; return { ok: true, agent }; });
}
// AUTH-SETUP-121: the actual Wizard consumes route eligibility and explicit repair.
describe('OpenCode model recovery', () => {
  it.each(['anthropic', 'poe'])('requires explicit compatible model selection for %s-only auth', async (provider) => {
    directConnection(provider);
    fireEvent.click(await setup());
    await screen.findByRole('region', { name: en.onboarding.connection.modelTitle });
    expect(mock.api.mutateConfig).not.toHaveBeenCalled(); expect(mock.api.updateVibeAgent).not.toHaveBeenCalled();
    const picker = screen.getByRole('combobox', { name: en.onboarding.connection.modelSelect });
    await waitFor(() => expect(picker.hasAttribute('disabled')).toBe(false));
    fireEvent.click(picker);
    fireEvent.click(await screen.findByText(`${provider}/selected-model`, { exact: true }));
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.modelApply }));
    await screen.findByTestId('destination');
    expect(mock.api.updateVibeAgent).toHaveBeenCalledExactlyOnceWith('opencode', { model: `${provider}/selected-model`, reasoning_effort: null });
    expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
  });
  it('cancel leaves the Agent and completion untouched', async () => {
    directConnection('anthropic'); fireEvent.click(await setup());
    await screen.findByRole('region', { name: en.onboarding.connection.modelTitle });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(mock.api.updateVibeAgent).not.toHaveBeenCalled(); expect(mock.api.mutateConfig).not.toHaveBeenCalled();
  });
  it('keeps selected input after a failed save and permits retry', async () => {
    directConnection('poe'); mock.api.updateVibeAgent.mockRejectedValueOnce(new Error('Agent save failed'));
    fireEvent.click(await setup());
    const picker = await screen.findByRole('combobox', { name: en.onboarding.connection.modelSelect });
    await waitFor(() => expect(picker.hasAttribute('disabled')).toBe(false)); fireEvent.click(picker);
    fireEvent.click(await screen.findByText('poe/selected-model', { exact: true }));
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.modelApply }));
    expect((await screen.findByRole('alert')).textContent).toContain('Agent save failed');
    expect(picker.textContent).toContain('poe/selected-model'); expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.modelApply })); await screen.findByTestId('destination');
  });
  it('preserves a custom compatible default without opening the catalog', async () => {
    directConnection('anthropic');
    mock.api.listVibeAgents.mockResolvedValue({ ok: true, default_agent_name: 'custom', agents: [{ ...opencodeAgent, name: 'custom', source: 'user', model: 'anthropic/my-model' }] });
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.readOpencodeOptionsForModelPicker).not.toHaveBeenCalled(); expect(mock.api.updateVibeAgent).not.toHaveBeenCalled(); expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
  });
  it.each(['anthropic/removed', 'anthropic/typo', 'anthropic/'] as const)('rejects unregistered Direct model %s without editing Agent or auth', async (model) => {
    directConnection('anthropic');
    mock.api.listVibeAgents.mockResolvedValue({ ok: true, default_agent_name: 'custom', agents: [{ ...opencodeAgent, name: 'custom', source: 'user', model }] });
    fireEvent.click(await setup()); await screen.findByRole('region', { name: en.onboarding.connection.modelTitle });
    expect(mock.api.updateVibeAgent).not.toHaveBeenCalled(); expect(mock.api.mutateConfig).not.toHaveBeenCalled();
  });
  it.each([[], undefined])('keeps an Agent route unverified with absent model list (%s)', async (models) => {
    directConnection('anthropic');
    mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [{ id: 'anthropic', active_auth_type: 'api', models }] });
    fireEvent.click(await setup()); await screen.findByRole('region', { name: en.onboarding.connection.modelTitle });
    expect(screen.queryByTestId('destination')).toBeNull(); expect(mock.api.updateVibeAgent).not.toHaveBeenCalled();
  });
  it.each(['anthropic/family/custom-model', 'family-model'] as const)('preserves registered slash or explicit-default Direct model %s', async (model) => {
    directConnection('anthropic');
    mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, default_provider: 'anthropic', providers: [{ id: 'anthropic', active_auth_type: 'api', models: ['family/custom-model', 'family-model'] }] });
    mock.api.listVibeAgents.mockResolvedValue({ ok: true, default_agent_name: 'custom', agents: [{ ...opencodeAgent, name: 'custom', source: 'user', model }] });
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.updateVibeAgent).not.toHaveBeenCalled(); expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
  });
  it('does not infer a default provider for a bare model and retries unreadable catalog without auth mutation', async () => {
    directConnection('anthropic');
    mock.api.listVibeAgents.mockResolvedValue({ ok: true, default_agent_name: 'custom', agents: [{ ...opencodeAgent, name: 'custom', source: 'user', model: 'my-model' }] });
    mock.api.getOpencodeProviders.mockRejectedValue(new Error('provider catalog unreadable'));
    fireEvent.click(await setup());
    expect((await screen.findByRole('alert')).textContent).toContain('provider catalog unreadable');
    expect(screen.queryByTestId('destination')).toBeNull();
    mock.api.getOpencodeProviders.mockResolvedValue({ ok: true, providers: [{ id: 'anthropic', active_auth_type: 'api', models: ['my-model'] }] });
    fireEvent.click(screen.getByRole('button', { name: en.common.retry }));
    await screen.findByRole('region', { name: en.onboarding.connection.modelTitle });
    expect(mock.api.mutateConfig).not.toHaveBeenCalled(); expect(mock.api.updateVibeAgent).not.toHaveBeenCalled();
  });
  it('an unreadable OpenCode catalog cannot block an existing usable Claude default', async () => {
    directConnection('anthropic');
    mock.api.getBackendConnection.mockImplementation((backend) => Promise.resolve({ ok: true, backend, enabled: true, installed: true, auth: 'api_key', application: 'applied', ready: true, entry_eligible: true }));
    mock.api.getOpencodeProviders.mockRejectedValue(new Error('provider catalog unreadable'));
    mock.api.listVibeAgents.mockResolvedValue({ ok: true, default_agent_name: 'mine', agents: [opencodeAgent, { name: 'mine', backend: 'claude', enabled: true }] });
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled(); expect(mock.api.updateVibeAgent).not.toHaveBeenCalled();
  });
  it('allows another ready backend despite OpenCode mismatch', async () => {
    directConnection('anthropic');
    mock.api.getBackendConnection.mockImplementation((backend) => Promise.resolve({ ok: true, backend, enabled: true, installed: true, auth: 'api_key', application: 'applied', ready: true, entry_eligible: true }));
    mock.api.listVibeAgents.mockResolvedValue({ ok: true, default_agent_name: 'mine', agents: [opencodeAgent, { name: 'mine', backend: 'claude', enabled: true }] });
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.readOpencodeOptionsForModelPicker).not.toHaveBeenCalled(); expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled(); expect(mock.api.updateVibeAgent).not.toHaveBeenCalled();
  });
});

it('uses canonical Hub supply instead of treating canonical slash IDs as Direct providers', async () => {
  directConnection('anthropic');
  const config = await mock.api.getConfig();
  mock.api.getConfig.mockResolvedValue({ ...config, capabilities: { model_hub: { enabled: true } } });
  mock.api.listVibeAgents.mockResolvedValue({ ok: true, default_agent_name: 'opencode', agents: [{ ...opencodeAgent, model: 'canonical/model' }] });
  mock.supply.mockResolvedValue({ backend: 'opencode', mode: 'hub', catalog_models: [{ id: 'canonical/model', routeable: true }], model_supply: [{ model_id: 'canonical/model', has_runnable_hop: true }] });
  fireEvent.click(await setup()); await screen.findByTestId('destination');
  expect(mock.api.getOpencodeProviders).not.toHaveBeenCalled(); expect(mock.api.updateVibeAgent).not.toHaveBeenCalled();
});
it('keeps incomplete setup when catalog retrieval fails and allows cancellation', async () => {
  directConnection('anthropic'); mock.api.readOpencodeOptionsForModelPicker.mockRejectedValue(new Error('Catalog unavailable'));
  fireEvent.click(await setup());
  expect((await screen.findByRole('alert')).textContent).toContain('Catalog unavailable');
  expect(mock.api.mutateConfig).not.toHaveBeenCalled(); expect(mock.api.updateVibeAgent).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(screen.queryByRole('region', { name: en.onboarding.connection.modelTitle })).toBeNull();
});

// AUTH-SETUP-120: the actual Wizard consumes legacy-IM recovery and narrow mutations.
describe('saved messaging recovery', () => {
  function incompleteSlack() {
    const config = { setup_completed: false, agents: { claude: { enabled: true }, codex: { enabled: true }, opencode: { enabled: true } }, platforms: { enabled: ['slack'] }, platform_catalog: [{ id: 'slack', config_key: 'slack', credential_fields: ['bot_token', 'app_token'] }], slack: { bot_token: '', app_token: '', has_bot_token: false, has_app_token: true, proxy_url: '' }, agent: { default_cwd: '/original' } };
    mock.api.getConfig.mockResolvedValue(config);
    mock.api.slackManifest.mockResolvedValue({ ok: true, manifest: '{}' });
    return config;
  }
  it('mounts only after explicit repair, saves changed credentials only, then rechecks and completes', async () => {
    const config = incompleteSlack(); running = false;
    fireEvent.click(await setup());
    await screen.findByRole('region', { name: en.onboarding.connection.platformRepair });
    expect(mock.control).not.toHaveBeenCalled(); expect(mock.api.slackManifest).not.toHaveBeenCalled();
    mock.api.mutateConfig.mockImplementation(async (mutations) => {
      if (mutations[0].path[0] === 'slack') mock.api.getConfig.mockResolvedValue({ ...config, slack: { ...config.slack, has_bot_token: true }, agent: { default_cwd: '/concurrent' } });
      return {};
    });
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.platformRepair }));
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(en.slackConfig.step2Title) }));
    fireEvent.change(await screen.findByPlaceholderText(en.slackConfig.botTokenPlaceholder), { target: { value: 'xoxb-fixture-only' } });
    fireEvent.click(screen.getByRole('button', { name: en.platform.apply }));
    await screen.findByTestId('destination');
    expect(mock.api.mutateConfig.mock.calls[0][0]).toEqual([{ kind: 'set', path: ['slack', 'bot_token'], value: 'xoxb-fixture-only' }]);
    expect(mock.api.mutateConfig.mock.calls[1][0]).toEqual([{ kind: 'set', path: ['setup_completed'], value: true }]);
    expect(mock.api.slackAuthTest).not.toHaveBeenCalled(); expect(mock.control).toHaveBeenCalledExactlyOnceWith('start');
  });
  it('keeps failed repair drafts editable and cancel preserves credentials', async () => {
    incompleteSlack(); mock.api.mutateConfig.mockRejectedValue(new Error('Platform apply failed'));
    fireEvent.click(await setup()); fireEvent.click(await screen.findByRole('button', { name: en.onboarding.connection.platformRepair }));
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(en.slackConfig.step2Title) }));
    const input = await screen.findByPlaceholderText(en.slackConfig.botTokenPlaceholder);
    fireEvent.change(input, { target: { value: 'xoxb-retained-draft' } });
    fireEvent.click(screen.getByRole('button', { name: en.platform.apply }));
    await screen.findByText('Platform apply failed');
    expect((input as HTMLInputElement).value).toBe('xoxb-retained-draft'); expect(screen.queryByTestId('destination')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(mock.api.mutateConfig).toHaveBeenCalledOnce();
  });
  it('honors redacted credential markers and the WeChat runnable exception', async () => {
    const config = incompleteSlack();
    mock.api.getConfig.mockResolvedValue({ ...config, platforms: { enabled: ['slack', 'wechat'] }, slack: { has_bot_token: true, has_app_token: true }, wechat: {}, platform_catalog: [...config.platform_catalog, { id: 'wechat', credential_fields: ['bot_token'] }] });
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.slackManifest).not.toHaveBeenCalled(); expect(mock.api.slackAuthTest).not.toHaveBeenCalled();
  });
});


// AUTH-SETUP-120: Discord's existing form emits credential and auxiliary settings.
describe('saved Discord recovery', () => {
  async function repairDiscord() {
    const config = { setup_completed: false, agents: { claude: { enabled: true } }, platforms: { enabled: ['discord'] }, platform_catalog: [{ id: 'discord', config_key: 'discord', credential_fields: ['bot_token'] }], discord: { bot_token: '', has_bot_token: false } };
    mock.api.getConfig.mockResolvedValue(config);
    mock.api.discordAuthTest.mockResolvedValue({ ok: true });
    mock.api.discordGuilds.mockResolvedValue({ ok: true, guilds: [{ id: 'g-one', name: 'Guild One' }, { id: 'g-two', name: 'Guild Two' }] });
    mock.api.mutateConfig.mockImplementation(async (changes) => {
      if (changes[0].path[0] === 'discord') mock.api.getConfig.mockResolvedValue({ ...config, discord: { bot_token: '', has_bot_token: true } });
      return {};
    });
    fireEvent.click(await setup());
    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.connection.platformRepair }));
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(en.discordConfig.step4Title) }));
    const input = await screen.findByPlaceholderText(en.discordConfig.botTokenPlaceholder);
    fireEvent.change(input, { target: { value: 'fixture-discord-token' } });
    fireEvent.click(screen.getByRole('button', { name: en.discordConfig.validateToken }));
    await screen.findByRole('checkbox', { name: 'Guild One' });
    return input;
  }
  it.each(['selected', 'cleared', 'unchanged', 'excluded'] as const)('persists only the intended guild selection (%s)', async (mode) => {
    mock.manageAccess = mode !== 'excluded';
    await repairDiscord();
    if (mode === 'selected' || mode === 'cleared') fireEvent.click(screen.getByRole('checkbox', { name: 'Guild One' }));
    if (mode === 'cleared') fireEvent.click(screen.getByRole('button', { name: en.discordConfig.clearGuilds }));
    if (mode === 'excluded') expect(screen.getByRole('checkbox', { name: 'Guild One' }).hasAttribute('disabled')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: en.platform.apply }));
    await screen.findByTestId('destination');
    if (mode === 'selected' || mode === 'cleared') {
      expect(mock.api.saveSettings).toHaveBeenCalledExactlyOnceWith({ guilds: mode === 'selected' ? { 'g-one': { enabled: true } } : {} }, 'discord');
      expect(mock.api.saveSettings.mock.invocationCallOrder[0]).toBeLessThan(mock.api.mutateConfig.mock.invocationCallOrder[1]);
    } else expect(mock.api.saveSettings).not.toHaveBeenCalled();
    expect(mock.api.mutateConfig.mock.calls[0][0]).toEqual([{ kind: 'set', path: ['discord', 'bot_token'], value: 'fixture-discord-token' }]);
  });
  it('retains selected guild and credential draft after partial save failure, then retries before completion', async () => {
    const input = await repairDiscord();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Guild Two' }));
    mock.api.saveSettings.mockRejectedValueOnce(new Error('Guild settings failed'));
    fireEvent.click(screen.getByRole('button', { name: en.platform.apply }));
    await screen.findByText('Guild settings failed');
    expect(screen.queryByTestId('destination')).toBeNull();
    expect((screen.getByRole('checkbox', { name: 'Guild Two' }) as HTMLInputElement).checked).toBe(true);
    expect((input as HTMLInputElement).value).toBe('fixture-discord-token');
    expect(mock.api.mutateConfig).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: en.platform.apply }));
    await screen.findByTestId('destination');
    expect(mock.api.saveSettings).toHaveBeenCalledTimes(2);
    expect(mock.api.saveSettings).toHaveBeenLastCalledWith({ guilds: { 'g-two': { enabled: true } } }, 'discord');
  });
  it('cancelled selection writes neither credentials nor settings', async () => {
    await repairDiscord(); fireEvent.click(screen.getByRole('checkbox', { name: 'Guild One' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(mock.api.saveSettings).not.toHaveBeenCalled(); expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.queryByTestId('destination')).toBeNull();
  });
});
