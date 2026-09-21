// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Wizard } from '../Wizard';
import en from '../../i18n/en.json';

const mock = vi.hoisted(() => ({ supply: vi.fn(), control: vi.fn(), toast: vi.fn(), permission: vi.fn(), manageAccess: true, apiFetch: vi.fn(), api: {
  saveSettings: vi.fn(), discordAuthTest: vi.fn(), discordGuilds: vi.fn(), slackManifest: vi.fn(), slackAuthTest: vi.fn(), getConfig: vi.fn(), detectCli: vi.fn(), getBackendRuntime: vi.fn(), getBackendConnection: vi.fn(),
  getOpencodeProviders: vi.fn(), readOpencodeOptionsForModelPicker: vi.fn(), readModelHubAgentCatalogForModelPicker: vi.fn(), updateVibeAgent: vi.fn(), getVibeAgent: vi.fn(), listVibeAgents: vi.fn(), setDefaultVibeAgent: vi.fn(), mutateConfig: vi.fn(),
} }));
vi.mock('../../context/ApiContext', async (importOriginal) => ({ ...await importOriginal<typeof import('../../context/ApiContext')>(), useApi: () => mock.api }));
vi.mock('@/lib/apiFetch', async (importOriginal) => ({ ...await importOriginal<typeof import('@/lib/apiFetch')>(), apiFetch: mock.apiFetch }));
vi.mock('../settings/models/modelsApi', () => ({ modelsApi: { getAgentSources: mock.supply } }));
vi.mock('../../context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => ({ capabilities: { can_manage_agents: true, can_manage_access_members: mock.manageAccess } }) }));
vi.mock('../../context/StatusContext', () => ({ useStatus: () => ({ control: mock.control }) }));
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: mock.toast }) }));
vi.mock('../settings/models/useModelHubCapability', () => ({ useModelHubCapability: () => false }));
vi.mock('../settings/shared/useOpencodePermission', () => ({ useOpencodePermission: () => ({ permissionAllowed: false, statusLoaded: true, setupPermission: mock.permission }) }));
const i18n = createInstance();
await i18n.init({ lng: 'en', fallbackLng: 'en', resources: { en: { translation: en } } });
function Destination() { const location = useLocation(); return <div data-testid="destination">{JSON.stringify(location.state)}</div>; }
function mount() { return render(<MemoryRouter initialEntries={['/setup']}><I18nextProvider i18n={i18n}><Routes><Route path="/setup" element={<Wizard />} /><Route path="/" element={<Destination />} /></Routes></I18nextProvider></MemoryRouter>); }
// `api.getConfig()` answers from a 30-second cache, and the shell's prerequisite reads
// deliberately do not. Both are driven from here so a case can make them disagree and
// prove which one the flow actually acted on.
const jsonResponse = (body: unknown, init: { ok?: boolean; status?: number } = {}) => ({ ok: init.ok ?? true, status: init.status ?? 200, json: async () => body }) as unknown as Response;
const baseConfig = (overrides: Record<string, unknown> = {}) => ({
  version: 'v2', setup_completed: false, runtime: {}, capabilities: { model_hub: { enabled: true } }, model_hub: { enabled: true },
  agents: { claude: { enabled: true }, codex: { enabled: true }, opencode: { enabled: true } }, platforms: { primary: 'slack', enabled: [] }, ...overrides,
});
let fresh: () => Promise<Response>;
/** Serve one config to both the cached projection and the uncached prerequisite GET. */
function serveConfig(config: Record<string, unknown>) { mock.api.getConfig.mockResolvedValue(config); fresh = async () => jsonResponse(config); }
/** Change only the uncached GET, so the cached projection keeps answering the old one. */
function serveFreshOnly(next: () => Promise<Response>) { fresh = next; }
let running: boolean;
beforeEach(() => {
  vi.resetAllMocks(); running = true; mock.manageAccess = true;
  mock.api.saveSettings.mockResolvedValue({ guild_allowlist: [] });
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  Element.prototype.scrollIntoView = vi.fn();
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
  mock.api.readModelHubAgentCatalogForModelPicker.mockResolvedValue(null);
  mock.apiFetch.mockImplementation(async (input: RequestInfo | URL) => {
    if (String(input) !== '/api/config') throw new Error(`unexpected apiFetch ${String(input)}`);
    return fresh();
  });
  serveConfig(baseConfig());
  mock.api.detectCli.mockResolvedValue({ found: true, path: '/测试/bin/assistant' });
  mock.api.getBackendRuntime.mockResolvedValue({ installed: true, has_update: false });
  mock.api.getBackendConnection.mockImplementation((backend) => Promise.resolve({ ok: true, backend, enabled: true, installed: true, auth: backend === 'claude' ? 'api_key' : 'none', application: running ? 'applied' : 'stopped', ready: backend === 'claude' && running, entry_eligible: backend === 'claude', permission_required: backend === 'opencode' }));
  mock.api.listVibeAgents.mockResolvedValue({ ok: true, default_agent_name: 'missing-codex', agents: [{ name: 'claude-agent', backend: 'claude', enabled: true }] });
  mock.api.setDefaultVibeAgent.mockResolvedValue({ ok: true });
  mock.api.mutateConfig.mockResolvedValue({ setup_completed: true });
  mock.control.mockImplementation(async () => { running = true; return { ok: true }; });
  // Hub-enabled config routes the OpenCode completion read through the supply
  // projection; Direct mode keeps every case authored before the gate on its path.
  mock.supply.mockResolvedValue({ backend: 'opencode', mode: 'direct', sources: { order: [], eligibility: [] }, routes: {}, builtin_models: [], catalog_models: [], named_agents: [], menu: null, model_supply: [], supply_status: 'unavailable' });
});
afterEach(async () => { cleanup(); await i18n.changeLanguage('en'); });
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
  // Hub-enabled config routes the OpenCode completion read through the supply
  // projection; Direct mode keeps every case authored before the gate on its path.
  mock.supply.mockResolvedValue({ backend: 'opencode', mode: 'direct', sources: { order: [], eligibility: [] }, routes: {}, builtin_models: [], catalog_models: [], named_agents: [], menu: null, model_supply: [], supply_status: 'unavailable' });
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
  serveConfig(baseConfig({ capabilities: { model_hub: { enabled: true } } }));
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

/** A saved platform whose required credential is gone: the shape that stops completion
 *  and hands the slot a repair form. Shared with the navigation-ownership cases below. */
function incompleteSlack() {
  const config = baseConfig({ platforms: { primary: 'slack', enabled: ['slack'] }, platform_catalog: [{ id: 'slack', config_key: 'slack', credential_fields: ['bot_token', 'app_token'] }], slack: { bot_token: '', app_token: '', has_bot_token: false, has_app_token: true, proxy_url: '' }, agent: { default_cwd: '/original' } }) as ReturnType<typeof baseConfig> & { slack: Record<string, unknown>; platform_catalog: Record<string, unknown>[] };
  serveConfig(config);
  mock.api.slackManifest.mockResolvedValue({ ok: true, manifest: '{}' });
  return config;
}

// AUTH-SETUP-120: the actual Wizard consumes legacy-IM recovery and narrow mutations.
describe('saved messaging recovery', () => {
  it('mounts only after explicit repair, saves changed credentials only, then rechecks and completes', async () => {
    const config = incompleteSlack(); running = false;
    fireEvent.click(await setup());
    await screen.findByRole('region', { name: en.onboarding.connection.platformRepair });
    expect(mock.control).not.toHaveBeenCalled(); expect(mock.api.slackManifest).not.toHaveBeenCalled();
    mock.api.mutateConfig.mockImplementation(async (mutations) => {
      if (mutations[0].path[0] === 'slack') serveConfig({ ...config, slack: { ...config.slack, has_bot_token: true }, agent: { default_cwd: '/concurrent' } });
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
    serveConfig({ ...config, platforms: { primary: 'slack', enabled: ['slack', 'wechat'] }, slack: { has_bot_token: true, has_app_token: true }, wechat: {}, platform_catalog: [...config.platform_catalog, { id: 'wechat', credential_fields: ['bot_token'] }] });
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.slackManifest).not.toHaveBeenCalled(); expect(mock.api.slackAuthTest).not.toHaveBeenCalled();
  });
});


// AUTH-SETUP-120: Discord's existing form emits credential and auxiliary settings.
describe('saved Discord recovery', () => {
  async function repairDiscord() {
    const config = baseConfig({ agents: { claude: { enabled: true } }, platforms: { primary: 'discord', enabled: ['discord'] }, platform_catalog: [{ id: 'discord', config_key: 'discord', credential_fields: ['bot_token'] }], discord: { bot_token: '', has_bot_token: false } });
    serveConfig(config);
    mock.api.discordAuthTest.mockResolvedValue({ ok: true });
    mock.api.discordGuilds.mockResolvedValue({ ok: true, guilds: [{ id: 'g-one', name: 'Guild One' }, { id: 'g-two', name: 'Guild Two' }] });
    mock.api.mutateConfig.mockImplementation(async (changes) => {
      if (changes[0].path[0] === 'discord') serveConfig({ ...config, discord: { bot_token: '', has_bot_token: true } });
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

const flowError = () => document.querySelector('.onboarding-flow-error')?.textContent ?? null;
const primaryAction = () => document.querySelector('.onboarding-primary-action') as HTMLButtonElement;
const backAction = () => document.querySelector('.onboarding-back-action') as HTMLButtonElement;
// The footer's spinner carries Tailwind's `motion-safe:` variant, so the token in the
// DOM is the whole `motion-safe:animate-spin`. A bare `.animate-spin` matches nothing
// here whatever the shell renders, which would make every "not busy" check vacuous.
const spinners = () => primaryAction().querySelectorAll('[class~="motion-safe:animate-spin"]').length;
/** Nothing that ends setup, and nothing that leaves the screen. */
function expectNoForwardWrite() {
  expect(mock.api.mutateConfig).not.toHaveBeenCalled();
  expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
  expect(mock.control).not.toHaveBeenCalled();
  expect(screen.queryByTestId('destination')).toBeNull();
}

// XpVZ/XpVc: the prerequisite is persisted state somebody else can change while this
// flow is open, so every case here starts from a config the initial load accepted.
//
// AUTH-SETUP-122, browser half: these cases own the admission rule — what the shell does
// with an answer it could not validate — against controlled responses. The producer half
// (real authentication refusal, the full envelope shape, CSRF-gated persistence, readback)
// is the scenario test the catalog entry names. The two do not compose into the assembled
// browser-to-API journey; that stays with integrated acceptance.
describe('fresh prerequisite boundary', () => {
  it.each([
    ['the saved intent', { model_hub: { enabled: false } }],
    ['the capability', { capabilities: { model_hub: { enabled: false } } }],
  ])('refuses completion when a fresh read says %s is off, while the cached projection still says on', async (_label, off) => {
    const enter = await setup();
    serveFreshOnly(async () => jsonResponse(baseConfig(off)));
    fireEvent.click(enter);
    await waitFor(() => expect(flowError()).toBe(en.onboarding.flow.gatewayRequired));
    // The bypass is the point: `api.getConfig()` would still have answered "enabled".
    expect(await mock.api.getConfig()).toMatchObject({ capabilities: { model_hub: { enabled: true } }, model_hub: { enabled: true } });
    expectNoForwardWrite();
    expect(backAction().hasAttribute('disabled')).toBe(false);
    expect(primaryAction().textContent).toContain(en.common.retry);
  });
  it.each([
    ['a body that never says what it had to', async () => jsonResponse({ ...baseConfig(), runtime: undefined }), en.onboarding.connection.readFailed],
    ['a body that is not JSON at all', async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected token <'); } }) as unknown as Response, en.onboarding.connection.readFailed],
    ['an HTTP failure that explains itself', async () => jsonResponse({ error: 'config unavailable' }, { ok: false, status: 503 }), 'config unavailable'],
    ['an HTTP failure that does not', async () => jsonResponse({}, { ok: false, status: 500 }), en.onboarding.connection.readFailedStatus.replace('{{status}}', '500')],
    ['a transport that never answered', async () => { throw new Error('network down'); }, 'Error: network down'],
  ])('leaves the prerequisite unknown and offers Retry for %s', async (_label, respond, explanation) => {
    const enter = await setup();
    serveFreshOnly(respond);
    fireEvent.click(enter);
    await waitFor(() => expect(flowError()).toBe(explanation));
    // Unknown is not "off": the gateway copy would be a claim nobody could confirm.
    expect(flowError()).not.toBe(en.onboarding.flow.gatewayRequired);
    expectNoForwardWrite();
    expect(backAction().hasAttribute('disabled')).toBe(false);
    expect(primaryAction().textContent).toContain(en.common.retry);
  });
  it('a Retry that reads a healthy config restores ordinary action without completing setup', async () => {
    const enter = await setup();
    serveFreshOnly(async () => { throw new Error('network down'); });
    fireEvent.click(enter);
    await waitFor(() => expect(flowError()).toBe('Error: network down'));
    serveConfig(baseConfig());
    fireEvent.click(screen.getByRole('button', { name: en.common.retry }));
    await waitFor(() => expect(flowError()).toBeNull());
    // Recovering the read is not consent to finish.
    expectNoForwardWrite();
    const enterAgain = await screen.findByRole('button', { name: 'Enter workspace' });
    await waitFor(() => expect(enterAgain.hasAttribute('disabled')).toBe(false));
    fireEvent.click(enterAgain);
    expect(await screen.findByTestId('destination')).toBeTruthy();
    expect(mock.api.mutateConfig).toHaveBeenCalledExactlyOnceWith([{ kind: 'set', path: ['setup_completed'], value: true }]);
    expect(enter).toBe(enterAgain);
  });
  it('an older enabled read settling last cannot revive a newer disabled answer or finish setup', async () => {
    const enter = await setup();
    let releaseEntry!: (value: Response) => void;
    serveFreshOnly(() => new Promise<Response>((resolve) => { releaseEntry = resolve; }));
    fireEvent.click(enter);
    await waitFor(() => expect(mock.apiFetch).toHaveBeenCalledTimes(2));
    // A language change re-runs the shell's load, which is how a second config read
    // legitimately starts while the completion boundary's first one is still open.
    serveFreshOnly(async () => jsonResponse(baseConfig({ model_hub: { enabled: false } })));
    await act(async () => { await i18n.changeLanguage('zh'); });
    await waitFor(() => expect(flowError()).toBe(en.onboarding.flow.gatewayRequired));
    await act(async () => { releaseEntry(jsonResponse(baseConfig())); });
    expect(flowError()).toBe(en.onboarding.flow.gatewayRequired);
    expectNoForwardWrite();
  });
  it('rereads the prerequisite after the awaited readiness work and before the final writes', async () => {
    running = false;
    const enter = await setup();
    // The gateway goes off while the start and the Agent listing are in flight.
    mock.api.listVibeAgents.mockImplementation(async () => {
      serveFreshOnly(async () => jsonResponse(baseConfig({ model_hub: { enabled: false } })));
      return { ok: true, default_agent_name: 'missing-codex', agents: [{ name: 'claude-agent', backend: 'claude', enabled: true }] };
    });
    fireEvent.click(enter);
    await waitFor(() => expect(flowError()).toBe(en.onboarding.flow.gatewayRequired));
    expect(mock.control).toHaveBeenCalledExactlyOnceWith('start');
    expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.queryByTestId('destination')).toBeNull();
  });
  // The admission the boundary grants is good until the next answer, not for the rest
  // of the operation. Both cases below read an enabled prerequisite, then let a real
  // second read — the language change that re-runs the shell's own load — arrive while
  // the completion is parked on an await it had already been authorised to make.
  const overtaken = [
    ['the gateway went off', async () => jsonResponse(baseConfig({ model_hub: { enabled: false } })), en.onboarding.flow.gatewayRequired],
    ['the prerequisite became unreadable', async () => { throw new Error('network down'); }, 'Error: network down'],
  ] as const;
  it.each(overtaken)('stops before the start it was authorised to make once %s', async (_label, respond, explanation) => {
    running = false;
    const enter = await setup();
    const answer = mock.api.getBackendConnection.getMockImplementation()!;
    let release!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    let reads = 0;
    mock.api.getBackendConnection.mockImplementation(async (backend) => { reads++; await held; return answer(backend); });
    fireEvent.click(enter);
    // The readiness reads the completion makes before it may ask for a start.
    await waitFor(() => expect(reads).toBeGreaterThanOrEqual(3));
    serveFreshOnly(respond);
    await act(async () => { await i18n.changeLanguage('zh'); });
    await waitFor(() => expect(flowError()).toBe(explanation));
    await act(async () => { release(); });
    await waitFor(() => expect(primaryAction().textContent).toContain(en.common.retry));
    expectNoForwardWrite();
    // And the stop is a stop, not a dead end: a Retry that reads a healthy config
    // hands the journey back, and the next explicit completion goes through.
    serveConfig(baseConfig());
    fireEvent.click(screen.getByRole('button', { name: en.common.retry }));
    await waitFor(() => expect(flowError()).toBeNull());
    const again = await screen.findByRole('button', { name: 'Enter workspace' });
    await waitFor(() => expect(again.hasAttribute('disabled')).toBe(false));
    fireEvent.click(again);
    expect(await screen.findByTestId('destination')).toBeTruthy();
    expect(mock.control).toHaveBeenCalledExactlyOnceWith('start');
  });
  it.each(overtaken)('stops before finishing setup when %s during the default write', async (_label, respond, explanation) => {
    const enter = await setup();
    let release!: (result: { ok: boolean }) => void;
    mock.api.setDefaultVibeAgent.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    fireEvent.click(enter);
    await waitFor(() => expect(mock.api.setDefaultVibeAgent).toHaveBeenCalledOnce());
    serveFreshOnly(respond);
    await act(async () => { await i18n.changeLanguage('zh'); });
    await waitFor(() => expect(flowError()).toBe(explanation));
    // The default was already written and stays written; what does not follow from it
    // is the completion it was a step towards.
    await act(async () => { release({ ok: true }); });
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.queryByTestId('destination')).toBeNull();
    expect(primaryAction().textContent).toContain(en.common.retry);
  });
  it('an obsolete completion cannot renew its own admission by reading again', async () => {
    const enter = await setup();
    let release!: (agents: unknown) => void;
    mock.api.listVibeAgents.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    fireEvent.click(enter);
    await waitFor(() => expect(mock.api.listVibeAgents).toHaveBeenCalledOnce());
    const readsBefore = mock.apiFetch.mock.calls.length;
    // The shell's own load takes the region while the listing is parked. The healthy
    // answer it leaves behind is exactly what a re-read would find, so a completion
    // that resumed here would sail through its final check and finish setup.
    await act(async () => { await i18n.changeLanguage('zh'); });
    await waitFor(() => expect(mock.apiFetch.mock.calls.length).toBeGreaterThan(readsBefore));
    const settled = mock.apiFetch.mock.calls.length;
    await act(async () => { release({ ok: true, default_agent_name: 'missing-codex', agents: [{ name: 'claude-agent', backend: 'claude', enabled: true }] }); });
    expect(mock.apiFetch).toHaveBeenCalledTimes(settled);
    expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.queryByTestId('destination')).toBeNull();
  });
  it('a completion whose setup surface is gone stops instead of writing into nothing', async () => {
    const enter = await setup();
    let release!: (result: { ok: boolean }) => void;
    mock.api.setDefaultVibeAgent.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    fireEvent.click(enter);
    await waitFor(() => expect(mock.api.setDefaultVibeAgent).toHaveBeenCalledOnce());
    // The other way the admission ends: the person left the setup route, so there is
    // no shell holding the answer this operation was acting under and no journey for
    // it to finish. What was already written stays written; nothing more follows.
    cleanup();
    await act(async () => { release({ ok: true }); });
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
  });
  it('a recovery completion callback passes the same boundary', async () => {
    directConnection('anthropic');
    fireEvent.click(await setup());
    const picker = await screen.findByRole('combobox', { name: en.onboarding.connection.modelSelect });
    await waitFor(() => expect(picker.hasAttribute('disabled')).toBe(false));
    fireEvent.click(picker);
    fireEvent.click(await screen.findByText('anthropic/selected-model', { exact: true }));
    serveFreshOnly(async () => jsonResponse(baseConfig({ model_hub: { enabled: false } })));
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.modelApply }));
    await waitFor(() => expect(flowError()).toBe(en.onboarding.flow.gatewayRequired));
    expect(mock.api.updateVibeAgent).toHaveBeenCalledOnce();
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.queryByTestId('destination')).toBeNull();
  });
});

// XpVU: a recovery owns the journey while it is open, and says so by holding the
// shared pair — not by pretending work is running.
describe('recovery navigation ownership', () => {
  it('holds both controls without a busy spinner, and gives them back on cancel', async () => {
    directConnection('anthropic');
    fireEvent.click(await setup());
    await screen.findByRole('region', { name: en.onboarding.connection.modelTitle });
    expect(backAction().hasAttribute('disabled')).toBe(true);
    expect(primaryAction().hasAttribute('disabled')).toBe(true);
    expect(spinners()).toBe(0);
    expect(primaryAction().textContent).toContain('Enter workspace');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(backAction().hasAttribute('disabled')).toBe(false));
  });
  // The same claim for the other recovery the slot carries. A saved platform's repair
  // is offered, not started: until somebody asks for it nothing runs, so the pair is
  // held rather than busy, and refusing it returns the journey untouched.
  it('a platform repair holds both controls without a busy spinner, and gives them back on cancel', async () => {
    incompleteSlack(); running = false;
    fireEvent.click(await setup());
    await screen.findByRole('region', { name: en.onboarding.connection.platformRepair });
    expect(backAction().hasAttribute('disabled')).toBe(true);
    expect(primaryAction().hasAttribute('disabled')).toBe(true);
    expect(spinners()).toBe(0);
    expect(mock.api.slackManifest).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(backAction().hasAttribute('disabled')).toBe(false));
    expect(screen.queryByRole('region', { name: en.onboarding.connection.platformRepair })).toBeNull();
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.queryByTestId('destination')).toBeNull();
  });
  it('keeps the journey put while a deferred platform repair is unsettled, survives its failure, and completes on a later success', async () => {
    const config = incompleteSlack(); running = false;
    let releaseApply!: (ok: boolean) => void;
    mock.api.mutateConfig.mockImplementation(async (mutations: { path: string[] }[]) => {
      if (mutations[0].path[0] !== 'slack') return {};
      if (!await new Promise<boolean>((resolve) => { releaseApply = resolve; })) throw new Error('Platform apply failed');
      serveConfig({ ...config, slack: { ...config.slack, has_bot_token: true } });
      return {};
    });
    fireEvent.click(await setup());
    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.connection.platformRepair }));
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(en.slackConfig.step2Title) }));
    const input = await screen.findByPlaceholderText(en.slackConfig.botTokenPlaceholder);
    fireEvent.change(input, { target: { value: 'xoxb-deferred' } });
    fireEvent.click(screen.getByRole('button', { name: en.platform.apply }));
    await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledOnce());
    // Unsettled: the write is out and the answer is not in, so the journey neither
    // leaves nor arrives on its own.
    fireEvent.click(backAction());
    expect(document.querySelector('[data-setup-screen]')?.getAttribute('data-setup-screen')).toBe('assistants');
    expect(screen.queryByTestId('destination')).toBeNull();

    // A refused write is not a finished recovery: the form stays, with its draft.
    await act(async () => { releaseApply(false); });
    await screen.findByText('Platform apply failed');
    await screen.findByRole('region', { name: en.onboarding.connection.platformRepair });
    expect(screen.queryByTestId('destination')).toBeNull();
    expect((input as HTMLInputElement).value).toBe('xoxb-deferred');
    expect(backAction().hasAttribute('disabled')).toBe(true);

    // And an explicit second attempt, settling for real, is what finishes setup.
    fireEvent.click(screen.getByRole('button', { name: en.platform.apply }));
    await waitFor(() => expect(mock.api.mutateConfig).toHaveBeenCalledTimes(2));
    await act(async () => { releaseApply(true); });
    expect(await screen.findByTestId('destination')).toBeTruthy();
    expect(mock.api.mutateConfig.mock.lastCall![0]).toEqual([{ kind: 'set', path: ['setup_completed'], value: true }]);
  });
  it('keeps the journey put while a deferred repair is unsettled, then completes on its own settlement', async () => {
    directConnection('anthropic');
    const save = mock.api.updateVibeAgent.getMockImplementation()!;
    let releaseSave!: () => void;
    mock.api.updateVibeAgent.mockImplementation(async (name: string, patch: unknown) => {
      await new Promise<void>((resolve) => { releaseSave = resolve; });
      return save(name, patch);
    });
    fireEvent.click(await setup());
    const picker = await screen.findByRole('combobox', { name: en.onboarding.connection.modelSelect });
    await waitFor(() => expect(picker.hasAttribute('disabled')).toBe(false));
    fireEvent.click(picker);
    fireEvent.click(await screen.findByText('anthropic/selected-model', { exact: true }));
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.connection.modelApply }));
    await waitFor(() => expect(mock.api.updateVibeAgent).toHaveBeenCalledOnce());
    fireEvent.click(backAction());
    expect(document.querySelector('[data-setup-screen]')?.getAttribute('data-setup-screen')).toBe('assistants');
    expect(screen.queryByTestId('destination')).toBeNull();
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    await act(async () => { releaseSave(); });
    expect(await screen.findByTestId('destination')).toBeTruthy();
  });
  it('an ordinary not-yet-ready primary still lets the journey go back', async () => {
    mock.api.getBackendConnection.mockImplementation(() => new Promise(() => {}));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Get started' }));
    await screen.findByRole('button', { name: 'Enter workspace' });
    expect(primaryAction().hasAttribute('disabled')).toBe(true);
    expect(backAction().hasAttribute('disabled')).toBe(false);
    fireEvent.click(backAction());
    await waitFor(() => expect(document.querySelector('[data-setup-screen]')?.getAttribute('data-setup-screen')).toBe('intro'));
  });
});
