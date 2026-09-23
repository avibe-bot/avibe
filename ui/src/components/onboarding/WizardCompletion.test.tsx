// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Wizard } from '../Wizard';
import { CONTRACT_VERSION, type AgentBackend, type AgentSupply, type NamedAgentSupply, type RuntimeDependency, type Source } from '../settings/models/types';
import en from '../../i18n/en.json';

const mock = vi.hoisted(() => ({ control: vi.fn(), toast: vi.fn(), permission: vi.fn(), manageAccess: true, apiFetch: vi.fn(), api: {
  saveSettings: vi.fn(), discordAuthTest: vi.fn(), discordGuilds: vi.fn(), slackManifest: vi.fn(), slackAuthTest: vi.fn(), getConfig: vi.fn(), detectCli: vi.fn(), getBackendRuntime: vi.fn(), getBackendConnection: vi.fn(),
  readModelHubAgentCatalogForModelPicker: vi.fn(), listVibeAgents: vi.fn(), getVibeAgent: vi.fn(), setDefaultVibeAgent: vi.fn(), mutateConfig: vi.fn(),
},
  // The endpoints the second screen reads, and — through the one Agent-supply authority
  // the shell shares with it — the endpoints the entry gate judges. Only the endpoints:
  // the collection authority, the lifecycle helper and the take-over dialog underneath
  // them are the real ones, which is what makes the journey below the journey rather
  // than a direct mount of the third screen.
  models: { listSources: vi.fn(), listAgents: vi.fn(), refreshAgentPresence: vi.fn(), scanMigration: vi.fn(), getRuntimeStatus: vi.fn(), getAgentChain: vi.fn(), previewAgentChain: vi.fn(), putAgentChain: vi.fn() },
}));
vi.mock('../../context/ApiContext', async (importOriginal) => ({ ...await importOriginal<typeof import('../../context/ApiContext')>(), useApi: () => mock.api }));
vi.mock('@/lib/apiFetch', async (importOriginal) => ({ ...await importOriginal<typeof import('@/lib/apiFetch')>(), apiFetch: mock.apiFetch }));
vi.mock('../settings/models/modelsApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../settings/models/modelsApi')>();
  return { ...actual, modelsApi: { ...actual.modelsApi, ...mock.models } };
});
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
// The second screen's own prerequisites: one usable Hub source and a running runtime is
// what makes its primary say "continue" rather than "add a source" or "connect".
const HUB_SOURCE: Source = {
  id: 'src_openai', vendor: 'openai', display_name: 'OpenAI', kind: 'api_key', protocol: 'openai_chat',
  supply_channel: 'hub', billing: 'metered', state: { status: 'active' }, last_discovered_at: null,
  models: [
    { id: 'gpt-5', origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null },
    { id: 'gpt-4.1', origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null },
  ],
};
const RUNTIME_OK: RuntimeDependency = {
  contract_version: CONTRACT_VERSION,
  manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1.0.0', source_sha: 'sha', assets: [] },
  status: { verified: true, health: 'ok' },
};
let fresh: () => Promise<Response>;
let served: Record<string, unknown>;
/** The listing `setDefaultVibeAgent` mutates, so a later uncached read can confirm it. */
let listing: { ok: true; default_agent_name: string | null; agents: unknown[] };
/** Serve one config to both the cached projection and the uncached prerequisite GET. */
function serveConfig(config: Record<string, unknown>) { served = config; mock.api.getConfig.mockResolvedValue(config); fresh = async () => jsonResponse(config); }
/** Change only the uncached GET, so the cached projection keeps answering the old one. */
function serveFreshOnly(next: () => Promise<Response>) { fresh = next; }
/** A config write that actually lands, because the readback is what ends setup now:
 *  completion is believed only when a later uncached read says so, so a fixture whose
 *  writes never reached storage would refuse every completion for the wrong reason.
 *  A case that replaces `mutateConfig` to model its own server delegates back here. */
function persistConfig(mutations: { path: string[]; value: unknown }[]) {
  const next = { ...served };
  for (const mutation of mutations) if (mutation.path.length === 1) next[mutation.path[0]] = mutation.value;
  serveConfig(next);
}
/** One Agent the Hub can route, in the shape the supply projection reports it. */
const route = (name: string, overrides: Partial<NamedAgentSupply> = {}): NamedAgentSupply =>
  ({ name, effective_model_id: 'openai/gpt-5.6-sol', supply_status: 'ok', ...overrides });
const hubSupply = (backend: AgentBackend, named: NamedAgentSupply[], overrides: Partial<AgentSupply> = {}): AgentSupply =>
  ({
    backend, cli_present: true, mode: 'hub', menu_kind: 'fixed', named_agents: named,
    sources: {
      order: [HUB_SOURCE.id],
      eligibility: [{ source_id: HUB_SOURCE.id, eligible: true }],
    },
    ...overrides,
  });
/** The Agent listing and the Hub's routing table, kept agreeing the way a server that
 *  routes the Agents it knows keeps them. The gate exists for the cases where they do
 *  NOT agree, and those set one of the two projections again afterwards. */
function serveAgents(defaultName: string | null, agents: Array<{ name: string; backend: AgentBackend } & Record<string, unknown>>) {
  listing = {
    ok: true,
    default_agent_name: defaultName,
    agents: agents.map((agent) => ({
      id: `${agent.backend}-${agent.name}`, display_name: agent.name, description: null,
      model: null, reasoning_effort: null, archived_at: null, updated_at: '',
      enabled: true, archived: false, source: 'builtin', ...agent,
    })),
  };
  mock.api.listVibeAgents.mockImplementation(async () => listing);
  mock.api.getVibeAgent.mockImplementation(async (name: string, params?: { cache?: boolean }) => {
    expect(params?.cache).toBe(false);
    const agent = listing.agents.find((row) => (row as { name: string }).name === name) as
      | ({ name: string; backend: string; source?: string; metadata?: Record<string, unknown> } & Record<string, unknown>)
      | undefined;
    if (!agent) return { ok: false, agent: null, default_agent_name: listing.default_agent_name };
    const metadata = agent.metadata ?? (agent.source === 'user' ? {} : { builtin_default: true });
    return {
      ok: true,
      default_agent_name: listing.default_agent_name,
      agent: { ...agent, system_prompt: null, created_at: '', metadata },
    };
  });
  const byBackend = new Map<AgentBackend, NamedAgentSupply[]>();
  for (const agent of agents) byBackend.set(agent.backend, [...byBackend.get(agent.backend) ?? [], route(agent.name)]);
  mock.models.listAgents.mockResolvedValue([...byBackend].map(([backend, named]) => hubSupply(backend, named)));
}
/** The baseline candidate: an ordinary shipped Claude Agent on a routed backend. */
const CLAUDE_AGENT = { id: 'cl-1', name: 'claude-agent', backend: 'claude' as const, enabled: true, archived: false, source: 'builtin' };
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
  serveAgents('missing-codex', [CLAUDE_AGENT]);
  mock.api.setDefaultVibeAgent.mockImplementation(async (name: string) => {
    listing = { ...listing, default_agent_name: name };
    return { ok: true };
  });
  mock.api.mutateConfig.mockImplementation(async (mutations) => { persistConfig(mutations); return {}; });
  // `/api/control` answers `{ok, action, status}`; the bootstrap only accepts a start it
  // can see acknowledged, so the fixture has to answer in the server's shape.
  mock.control.mockImplementation(async () => { running = true; return { ok: true, action: 'start' }; });
  mock.models.listSources.mockResolvedValue([HUB_SOURCE]);
  mock.models.refreshAgentPresence.mockResolvedValue([]);
  mock.models.scanMigration.mockResolvedValue({ items: [] });
  mock.models.getRuntimeStatus.mockResolvedValue(RUNTIME_OK);
});
afterEach(async () => { cleanup(); await i18n.changeLanguage('en'); });
/** The registered journey, not a direct mount of the last screen: the second screen is
 *  in the sequence, so every completion case below reaches the third one through it. */
async function arriveAtProviders() {
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Get started' }));
  await waitFor(() => expect(primaryAction().textContent).toContain(en.onboarding.providers.actionContinue));
  await waitFor(() => expect(primaryAction().hasAttribute('disabled')).toBe(false));
}
/** `stopped` is about the state the completion has to find, not the state the journey
 *  needed: arriving at the second screen legitimately starts the Hub, so a case that
 *  owns the completion's own start has to take the runtime back down after the handoff
 *  and forget the bootstrap's calls. Otherwise it would be asserting on both. */
async function setup(options: { stopped?: boolean } = {}) {
  await arriveAtProviders();
  fireEvent.click(primaryAction());
  const enter = await screen.findByRole('button', { name: 'Enter workspace' });
  await waitFor(() => expect(enter.hasAttribute('disabled')).toBe(false));
  if (options.stopped) { running = false; mock.control.mockClear(); }
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
    const enter = await setup({ stopped: true }); fireEvent.click(enter);
    await screen.findByTestId('destination');
    expect(mock.control).toHaveBeenCalledExactlyOnceWith('start');
    expect(mock.control.mock.invocationCallOrder[0]).toBeLessThan(mock.api.mutateConfig.mock.invocationCallOrder[0]);
  });
  it('preserves an existing usable default', async () => {
    serveAgents('mine', [{ name: 'mine', backend: 'claude' }]);
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
  });
  it.each(['start', 'legacy IM'])('keeps failed completion recoverable (%s)', async (failure) => {
    if (failure !== 'start') mock.api.mutateConfig.mockRejectedValue(new Error("Config 'slack.bot_token' must be provided"));
    const enter = await setup({ stopped: failure === 'start' });
    if (failure === 'start') mock.control.mockRejectedValue(new Error('Start unavailable'));
    fireEvent.click(enter);
    await screen.findByRole('alert');
    expect(screen.queryByTestId('destination')).toBeNull();
    if (failure === 'start') expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    mock.control.mockImplementation(async () => { running = true; return { ok: true, action: 'start' }; });
    mock.api.mutateConfig.mockImplementation(async (mutations) => { persistConfig(mutations); return {}; });
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

// AUTH-SETUP-121: the journey is Model Hub's, so the gate a completion has to pass is
// a route the Hub can actually serve for ONE named Agent. The retracted Direct detour
// used to live here; a completion-time credential form could only ever have repaired a
// backend this journey does not adopt, so its cases are replaced by the join itself.
describe('the correlated entry gate', () => {
  /** Every assistant installed, enabled, applied and in custody — so that what a case
   *  changes afterwards is the only thing keeping the gate shut. */
  const allConnected = (overrides: (backend: AgentBackend) => Record<string, unknown> = () => ({})) =>
    mock.api.getBackendConnection.mockImplementation((backend: AgentBackend) => Promise.resolve({
      ok: true, backend, enabled: true, installed: true, auth: 'api_key',
      application: 'applied', ready: true, entry_eligible: true, ...overrides(backend),
    }));
  const refusal = async (copy: string) => {
    expect((await screen.findByRole('alert')).textContent).toContain(copy);
    expectNoForwardWrite();
  };

  it('refuses three half-answers that belong to different assistants', async () => {
    // A detected Claude the Hub cannot route, plus a routed Codex whose configured
    // binary detectCli does not find. Every condition is satisfied somewhere and none
    // of them meet. `connection.installed` is not the detector.
    allConnected();
    mock.api.listVibeAgents.mockResolvedValue({
      ok: true, default_agent_name: null,
      agents: [CLAUDE_AGENT, { name: 'codex-agent', backend: 'codex', enabled: true, archived: false, source: 'builtin' }],
    });
    mock.models.listAgents.mockResolvedValue([hubSupply('claude', []), hubSupply('codex', [route('codex-agent')])]);
    const enter = await setup();
    mock.api.detectCli.mockImplementation(async (binary: string) => ({ found: binary !== 'codex', path: binary }));
    fireEvent.click(enter);
    await refusal(en.onboarding.connection.modelUnavailable);
  });

  it('admits a backend whose connection.installed is false once detectCli and presence agree', async () => {
    allConnected(() => ({ installed: false }));
    fireEvent.click(await setup());
    await screen.findByTestId('destination');
  });

  it('will not let one Agent\'s route speak for another with the same backend', async () => {
    allConnected();
    mock.models.listAgents.mockResolvedValue([hubSupply('claude', [route('someone-else')])]);
    fireEvent.click(await setup());
    await refusal(en.onboarding.connection.modelUnavailable);
  });

  // What the server says about THIS name, in the four shapes that are not a run.
  it.each([
    ['a route whose sources are still coming up', route('claude-agent', { supply_status: 'waiting' })],
    ['a route whose sources broke', route('claude-agent', { supply_status: 'interrupted' })],
    ['a backend that never routed this name', route('claude-agent', { route_reason: 'route_unconfigured' })],
    ['a name with no model behind it', route('claude-agent', { effective_model_id: null, supply_status: null })],
  ])('refuses %s', async (_label, named) => {
    allConnected();
    mock.models.listAgents.mockResolvedValue([hubSupply('claude', [named])]);
    fireEvent.click(await setup());
    await refusal(en.onboarding.connection.modelUnavailable);
  });

  it('admits a degraded route, which is a route that still runs', async () => {
    allConnected();
    mock.models.listAgents.mockResolvedValue([hubSupply('claude', [route('claude-agent', { supply_status: 'degraded' })])]);
    fireEvent.click(await setup()); await screen.findByTestId('destination');
  });

  it('refuses while the Hub that would serve the route is down', async () => {
    // Hub MODE is not Hub readiness: the routes are configured and nothing can run.
    // Judged at completion, not at the providers arrival that needs the engine up.
    const enter = await setup();
    mock.models.getRuntimeStatus.mockResolvedValue({ ...RUNTIME_OK, status: { verified: true, health: 'down' } });
    fireEvent.click(enter);
    await refusal(en.onboarding.connection.modelUnavailable);
  });

  it('refuses a backend whose CLI the refreshed presence read cannot see', async () => {
    const enter = await setup();
    mock.models.listAgents.mockResolvedValue([hubSupply('claude', [route('claude-agent')], { cli_present: false })]);
    fireEvent.click(enter);
    await waitFor(() => expect(mock.models.refreshAgentPresence).toHaveBeenCalled());
    await refusal(en.onboarding.connection.modelUnavailable);
  });

  it('detects the persisted CLI path before entry, not the backend name', async () => {
    serveConfig(baseConfig({
      agents: {
        claude: { enabled: true, cli_path: '/opt/custom/claude-bin' },
        codex: { enabled: true },
        opencode: { enabled: true },
      },
    }));
    const enter = await setup();
    mock.api.detectCli.mockClear();
    fireEvent.click(enter);
    await screen.findByTestId('destination');
    expect(mock.api.detectCli).toHaveBeenCalledWith('/opt/custom/claude-bin');
    expect(mock.api.detectCli.mock.invocationCallOrder[0]).toBeLessThan(mock.api.mutateConfig.mock.invocationCallOrder[0]);
  });

  it('cannot enter when detectCli does not find the configured binary, even with installed and cli_present', async () => {
    serveConfig(baseConfig({
      agents: {
        claude: { enabled: true, cli_path: '/opt/custom/claude-bin' },
        codex: { enabled: true },
        opencode: { enabled: true },
      },
    }));
    const enter = await setup();
    mock.api.detectCli.mockImplementation(async (binary: string) => ({
      found: binary !== '/opt/custom/claude-bin',
      path: binary,
    }));
    fireEvent.click(enter);
    await refusal(en.onboarding.connection.modelUnavailable);
  });

  it('cannot enter when the shared presence refresh fails', async () => {
    const enter = await setup();
    mock.models.refreshAgentPresence.mockRejectedValue(new Error('presence unread'));
    fireEvent.click(enter);
    await refusal(en.onboarding.connection.modelUnavailable);
  });
  it('stops when a fresh read does not confirm the default the server accepted', async () => {
    // The next screen runs on whatever the server thinks the default is, so the write's
    // own `ok` is not what setup believes.
    mock.api.setDefaultVibeAgent.mockImplementation(async () => ({ ok: true }));
    fireEvent.click(await setup());
    expect((await screen.findByRole('alert')).textContent).toContain(en.onboarding.connection.entryFailed);
    expect(mock.api.setDefaultVibeAgent).toHaveBeenCalledExactlyOnceWith('claude-agent');
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.queryByTestId('destination')).toBeNull();
  });

  // A confirmed `stopped` is the one application state an explicit start may answer.
  // These are the states that are not a confirmation, so nothing is started for them.
  it.each(['draining', 'failed', 'unknown'] as const)('refuses an application that is %s without asking for a start', async (application) => {
    const enter = await setup();
    allConnected(() => ({ application }));
    fireEvent.click(enter);
    await refusal(en.onboarding.connection.modelUnavailable);
  });

  it('prefers the assistant\'s own Agent once the saved default cannot run', async () => {
    // `aa-custom` sorts first and is a perfectly usable candidate, so picking `claude`
    // is the preference stated rather than whatever the list happened to hand over.
    allConnected();
    serveAgents('gone', [
      { name: 'aa-custom', backend: 'claude', source: 'user' },
      { name: 'claude', backend: 'claude', source: 'file', metadata: { builtin_default: true } },
    ]);
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.setDefaultVibeAgent).toHaveBeenCalledExactlyOnceWith('claude');
    expect(mock.api.getVibeAgent.mock.calls.every(([, params]) => params?.cache === false)).toBe(true);
  });

  it('does not treat a same-named Agent as the assistant when its metadata cannot be read', async () => {
    // `claude` sorts after `aa-custom`. Inferring builtin from source would pick it;
    // a failed full read must not, so the first runnable candidate is the one entered.
    allConnected();
    serveAgents('gone', [
      { name: 'aa-custom', backend: 'claude', source: 'user' },
      { name: 'claude', backend: 'claude', source: 'builtin' },
    ]);
    mock.api.getVibeAgent.mockImplementation(async (name: string, params?: { cache?: boolean }) => {
      expect(params?.cache).toBe(false);
      if (name === 'claude') return { ok: false, agent: null, default_agent_name: 'gone' };
      const agent = listing.agents.find((row) => (row as { name: string }).name === name);
      if (!agent) return { ok: false, agent: null, default_agent_name: 'gone' };
      return { ok: true, default_agent_name: 'gone', agent: { ...agent, system_prompt: null, created_at: '', metadata: {} } };
    });
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.setDefaultVibeAgent).toHaveBeenCalledExactlyOnceWith('aa-custom');
  });

  it('keeps a usable custom default that no card represents', async () => {
    allConnected();
    serveAgents('mine', [
      { name: 'mine', backend: 'claude', source: 'user' },
      { name: 'claude', backend: 'claude', source: 'file', metadata: { builtin_default: true } },
    ]);
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
  });

  it('starts a confirmed-stopped controller even when Hub IPC is down, then completes on a fresh read', async () => {
    const enter = await setup({ stopped: true });
    mock.models.refreshAgentPresence.mockImplementation(async () => {
      if (!running) throw new Error('engine_down: controller socket absent');
      return [];
    });
    mock.models.listAgents.mockImplementation(async () => {
      if (!running) throw new Error('engine_down: controller socket absent');
      return [hubSupply('claude', [route('claude-agent')])];
    });
    mock.models.getRuntimeStatus.mockImplementation(async () => {
      if (!running) throw new Error('engine_down: controller socket absent');
      return RUNTIME_OK;
    });
    fireEvent.click(enter);
    fireEvent.click(enter);
    await screen.findByTestId('destination');
    expect(mock.control).toHaveBeenCalledExactlyOnceWith('start');
    expect(mock.control.mock.invocationCallOrder[0]).toBeLessThan(mock.api.mutateConfig.mock.invocationCallOrder[0]);
  });

  it('starts a stopped controller with no assistant or entry_eligible prerequisite', async () => {
    const enter = await setup({ stopped: true });
    mock.api.listVibeAgents.mockResolvedValue({ ok: true, agents: [], default_agent_name: null });
    mock.api.getBackendConnection.mockImplementation((backend) => Promise.resolve({
      ok: true, backend, enabled: true, installed: true,
      ready: running, entry_eligible: false, application: running ? 'applied' : 'stopped',
    }));
    mock.models.refreshAgentPresence.mockImplementation(async () => {
      if (!running) throw new Error('engine_down: controller socket absent');
      return [];
    });
    mock.models.listAgents.mockImplementation(async () => {
      if (!running) throw new Error('engine_down: controller socket absent');
      return [hubSupply('claude', [route('claude-agent')])];
    });
    mock.models.getRuntimeStatus.mockImplementation(async () => {
      if (!running) throw new Error('engine_down: controller socket absent');
      return RUNTIME_OK;
    });
    mock.control.mockImplementation(async () => {
      running = true;
      serveAgents('missing-codex', [CLAUDE_AGENT]);
      mock.api.getBackendConnection.mockImplementation((backend) => Promise.resolve({
        ok: true, backend, enabled: true, installed: true,
        ready: backend === 'claude', entry_eligible: backend === 'claude', application: 'applied',
      }));
      return { ok: true, action: 'start' };
    });
    fireEvent.click(enter);
    await screen.findByTestId('destination');
    expect(mock.control).toHaveBeenCalledExactlyOnceWith('start');
  });

  it('does not complete a recovery that is still unready, and does not start twice', async () => {
    const enter = await setup({ stopped: true });
    mock.models.refreshAgentPresence.mockImplementation(async () => {
      if (!running) throw new Error('engine_down: controller socket absent');
      return [];
    });
    mock.models.listAgents.mockImplementation(async () => {
      if (!running) throw new Error('engine_down: controller socket absent');
      return [hubSupply('claude', [route('claude-agent')])];
    });
    mock.models.getRuntimeStatus.mockImplementation(async () => {
      if (!running) throw new Error('engine_down: controller socket absent');
      return { ...RUNTIME_OK, status: { verified: true, health: 'down' } };
    });
    fireEvent.click(enter);
    expect((await screen.findByRole('alert')).textContent).toContain(en.onboarding.connection.modelUnavailable);
    expect(mock.control).toHaveBeenCalledExactlyOnceWith('start');
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.queryByTestId('destination')).toBeNull();
  });

  it('never starts an unknown application even when Hub IPC is down', async () => {
    const enter = await setup();
    mock.api.getBackendConnection.mockImplementation((backend) => Promise.resolve({
      ok: true, backend, enabled: true, installed: true, ready: false,
      entry_eligible: true, application: 'unknown',
    }));
    mock.models.refreshAgentPresence.mockRejectedValue(new Error('engine_down: controller socket absent'));
    mock.models.listAgents.mockRejectedValue(new Error('engine_down: controller socket absent'));
    mock.models.getRuntimeStatus.mockRejectedValue(new Error('engine_down: controller socket absent'));
    fireEvent.click(enter);
    expect((await screen.findByRole('alert')).textContent).toContain(en.onboarding.connection.applyPending);
    expect(mock.control).not.toHaveBeenCalled();
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.queryByTestId('destination')).toBeNull();
  });

  it('writes completion last and enters only on a fresh read that says so', async () => {
    const enter = await setup();
    fireEvent.click(enter); await screen.findByTestId('destination');
    expect(mock.api.setDefaultVibeAgent.mock.invocationCallOrder[0]).toBeLessThan(mock.api.mutateConfig.mock.invocationCallOrder[0]);
    // The last read of the journey is uncached: a cached answer from before the write
    // cannot settle anything about it.
    const last = mock.apiFetch.mock.calls.at(-1)!;
    expect(String(last[0])).toBe('/api/config');
    expect((last[1] as RequestInit | undefined)?.cache).toBe('no-store');
  });

  it('refuses to enter on an accepted write the config does not show', async () => {
    mock.api.mutateConfig.mockResolvedValue({ setup_completed: true });
    fireEvent.click(await setup());
    expect((await screen.findByRole('alert')).textContent).toContain(en.onboarding.flow.completionUnconfirmed);
    expect(screen.queryByTestId('destination')).toBeNull();
    expect(mock.api.mutateConfig).toHaveBeenCalledOnce();
  });

  it('reconciles a completion write whose answer was lost by reading, not by writing again', async () => {
    // The reply never came back; the write did land. Resubmitting would be a guess,
    // and the config is the only thing that can say which happened.
    mock.api.mutateConfig.mockImplementation(async (mutations) => { persistConfig(mutations); throw new Error('socket closed'); });
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.mutateConfig).toHaveBeenCalledOnce();
  });
});

/** A saved platform whose required credential is gone: the shape that used to stop
 *  completion. Shared with the navigation cases below. */
function incompleteSlack() {
  const config = baseConfig({ platforms: { primary: 'slack', enabled: ['slack'] }, platform_catalog: [{ id: 'slack', config_key: 'slack', credential_fields: ['bot_token', 'app_token'] }], slack: { bot_token: '', app_token: '', has_bot_token: false, has_app_token: true, proxy_url: '' }, agent: { default_cwd: '/original' } }) as ReturnType<typeof baseConfig> & { slack: Record<string, unknown>; platform_catalog: Record<string, unknown>[] };
  serveConfig(config);
  mock.api.slackManifest.mockResolvedValue({ ok: true, manifest: '{}' });
  return config;
}

// AUTH-SETUP-120: a saved platform is no longer a gate. The Wizard finishes on the
// config it was given and writes nothing on the platform's behalf.
describe('saved messaging no longer gates the workspace', () => {
  it('completes with an enabled platform whose saved credential is gone', async () => {
    incompleteSlack();
    fireEvent.click(await setup({ stopped: true }));
    expect(await screen.findByTestId('destination')).toBeTruthy();
    expect(mock.api.mutateConfig.mock.lastCall![0]).toEqual([{ kind: 'set', path: ['setup_completed'], value: true }]);
    expect(mock.api.slackManifest).not.toHaveBeenCalled(); expect(mock.api.slackAuthTest).not.toHaveBeenCalled();
    expect(mock.control).toHaveBeenCalledExactlyOnceWith('start');
  });
  it('honors redacted credential markers and the WeChat runnable exception', async () => {
    const config = incompleteSlack();
    serveConfig({ ...config, platforms: { primary: 'slack', enabled: ['slack', 'wechat'] }, slack: { has_bot_token: true, has_app_token: true }, wechat: {}, platform_catalog: [...config.platform_catalog, { id: 'wechat', credential_fields: ['bot_token'] }] });
    fireEvent.click(await setup()); await screen.findByTestId('destination');
    expect(mock.api.slackManifest).not.toHaveBeenCalled(); expect(mock.api.slackAuthTest).not.toHaveBeenCalled();
  });
});

// e-Da: the block holds two different kinds of string. `flowError` is the sentence a
// person reads, which is always a bundle key; the disclosure below it is whatever the
// server or the transport said. Reading them apart is what makes "the raw detail is not
// the alert" an assertion rather than a coincidence of concatenation.
const flowError = () => document.querySelector('.onboarding-flow-error p')?.textContent ?? null;
const flowErrorDetail = () => {
  const disclosure = document.querySelector('.onboarding-flow-error details');
  if (!disclosure) return null;
  return (disclosure.textContent ?? '').slice((disclosure.querySelector('summary')?.textContent ?? '').length);
};
const primaryAction = () => document.querySelector('.onboarding-primary-action') as HTMLButtonElement;
const assistantsRoot = () => document.querySelector('[data-setup-screen-root="assistants"]') as HTMLElement;
const assistantCard = (name: string) => within(within(assistantsRoot()).getByLabelText(name));
/** The card's route button is named for the assistant it belongs to: the label on it is
    the shared route's first model, which is not knowable before the read lands. */
const routeButton = (name: string) => en.onboarding.setup.defaultModelNamed.replace('{{name}}', name);
const backAction = () => document.querySelector('.onboarding-back-action') as HTMLButtonElement;
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
// browser-to-API journey: that is AUTH-SETUP-123, recorded as partial and owned by
// integrated acceptance, because no harness without a real browser can claim it.
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
  // The fourth column is what the producer said, and every producer that says anything
  // here says it in English whatever language the setup is being read in — so it belongs
  // under the disclosure, never in the sentence. `null` is the honest case where the
  // server offered nothing quotable: then there is no disclosure to open at all.
  it.each([
    ['a body that never says what it had to', async () => jsonResponse({ ...baseConfig(), runtime: undefined }), en.onboarding.connection.readFailed, null],
    ['a body that is not JSON at all', async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected token <'); } }) as unknown as Response, en.onboarding.connection.readFailed, null],
    ['an HTTP failure that explains itself', async () => jsonResponse({ error: 'config unavailable' }, { ok: false, status: 503 }), en.onboarding.connection.readFailedStatus.replace('{{status}}', '503'), 'config unavailable'],
    ['an HTTP failure that does not', async () => jsonResponse({}, { ok: false, status: 500 }), en.onboarding.connection.readFailedStatus.replace('{{status}}', '500'), null],
    ['a transport that never answered', async () => { throw new Error('network down'); }, en.onboarding.connection.readFailed, 'Error: network down'],
  ])('leaves the prerequisite unknown and offers Retry for %s', async (_label, respond, explanation, detail) => {
    const enter = await setup();
    serveFreshOnly(respond);
    fireEvent.click(enter);
    await waitFor(() => expect(flowError()).toBe(explanation));
    // Unknown is not "off": the gateway copy would be a claim nobody could confirm.
    expect(flowError()).not.toBe(en.onboarding.flow.gatewayRequired);
    // What the producer said is kept, and kept out of the sentence.
    expect(flowErrorDetail()).toBe(detail);
    if (detail) expect(flowError()).not.toContain(detail);
    expectNoForwardWrite();
    expect(backAction().hasAttribute('disabled')).toBe(false);
    expect(primaryAction().textContent).toContain(en.common.retry);
  });
  it('a Retry that reads a healthy config restores ordinary action without completing setup', async () => {
    const enter = await setup();
    serveFreshOnly(async () => { throw new Error('network down'); });
    fireEvent.click(enter);
    await waitFor(() => expect(flowError()).toBe(en.onboarding.connection.readFailed));
    expect(flowErrorDetail()).toBe('Error: network down');
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
    const readsBefore = mock.apiFetch.mock.calls.length;
    serveFreshOnly(() => new Promise<Response>((resolve) => { releaseEntry = resolve; }));
    fireEvent.click(enter);
    await waitFor(() => expect(mock.apiFetch.mock.calls.length).toBe(readsBefore + 1));
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
    const enter = await setup({ stopped: true });
    // The gateway goes off while the Agent listing is in flight. A start is a write
    // against persisted intent, so the listing wait is not permission to start: the
    // next read of the prerequisite has to refuse first.
    mock.api.listVibeAgents.mockImplementation(async () => {
      serveFreshOnly(async () => jsonResponse(baseConfig({ model_hub: { enabled: false } })));
      return { ok: true, default_agent_name: 'missing-codex', agents: [{ name: 'claude-agent', backend: 'claude', enabled: true }] };
    });
    fireEvent.click(enter);
    await waitFor(() => expect(flowError()).toBe(en.onboarding.flow.gatewayRequired));
    expect(mock.control).not.toHaveBeenCalled();
    expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
    expect(screen.queryByTestId('destination')).toBeNull();
  });

  it('rechecks persisted gateway intent after readiness waits before recovery starts', async () => {
    const enter = await setup({ stopped: true });
    const original = mock.api.getBackendConnection.getMockImplementation()!;
    let withdraw = false;
    mock.api.getBackendConnection.mockImplementation(async (backend) => {
      const value = await original(backend);
      if (!withdraw) {
        withdraw = true;
        serveFreshOnly(async () => jsonResponse(baseConfig({ model_hub: { enabled: false } })));
      }
      return value;
    });
    fireEvent.click(enter);
    await waitFor(() => expect(flowError()).toBe(en.onboarding.flow.gatewayRequired));
    expect(mock.control).not.toHaveBeenCalled();
    expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
    expect(mock.api.mutateConfig).not.toHaveBeenCalled();
  });
  // The admission the boundary grants is good until the next answer, not for the rest
  // of the operation. Both cases below read an enabled prerequisite, then let a real
  // second read — the language change that re-runs the shell's own load — arrive while
  // the completion is parked on an await it had already been authorised to make.
  const overtaken = [
    ['the gateway went off', async () => jsonResponse(baseConfig({ model_hub: { enabled: false } })), en.onboarding.flow.gatewayRequired],
    ['the prerequisite became unreadable', async () => { throw new Error('network down'); }, en.onboarding.connection.readFailed],
  ] as const;
  it.each(overtaken)('stops before the start it was authorised to make once %s', async (_label, respond, explanation) => {
    const enter = await setup({ stopped: true });
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
});

// XpVU: a screen that is waiting on its own answer holds the shared pair while it
// waits — without pretending separate work is running.
describe('journey navigation ownership', () => {
  it('an ordinary not-yet-ready primary still lets the journey go back', async () => {
    await arriveAtProviders();
    // Only now: the second screen's own admission needs a connection answer, so hanging
    // it from the start would hold the journey before it ever reached the third screen.
    mock.api.getBackendConnection.mockImplementation(() => new Promise(() => {}));
    fireEvent.click(primaryAction());
    await screen.findByRole('button', { name: 'Enter workspace' });
    expect(primaryAction().hasAttribute('disabled')).toBe(true);
    expect(backAction().hasAttribute('disabled')).toBe(false);
    fireEvent.click(backAction());
    await waitFor(() => expect(document.querySelector('[data-setup-screen]')?.getAttribute('data-setup-screen')).toBe('providers'));
  });
});

// C2/D11: the journey is three screens, and the middle one is where the controller is
// made answerable. These cases own that edge — when it happens, how often, and what a
// failure leaves on screen — from the assembled Wizard rather than from the sequence
// module, which cannot say anything about when a React tree calls it.
describe('the registered journey', () => {
  const screenId = () => document.querySelector('[data-setup-screen]')?.getAttribute('data-setup-screen');
  const seeds = () => mock.apiFetch.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === 'POST');
  const gateway = () => document.querySelector('.setup-gateway')!;

  it('establishes the controller on arrival at the second screen, never on mount', async () => {
    mount();
    const start = await screen.findByRole('button', { name: 'Get started' });
    // Nothing is seeded or started for somebody who has not asked to go anywhere yet.
    expect(seeds()).toHaveLength(0);
    expect(mock.models.getRuntimeStatus).not.toHaveBeenCalled();

    fireEvent.click(start);
    await waitFor(() => expect(screenId()).toBe('providers'));
    await waitFor(() => expect(mock.models.getRuntimeStatus).toHaveBeenCalledOnce());
    expect(seeds()).toHaveLength(1);
    expect(seeds()[0][1]).toMatchObject({ method: 'POST', body: '{}' });
    // The readback is the next call and is uncached: the seed cleared no projection,
    // so a cached read here would describe the world before the write.
    const seedIndex = mock.apiFetch.mock.calls.indexOf(seeds()[0]);
    expect((mock.apiFetch.mock.calls[seedIndex + 1]?.[1] as RequestInit | undefined)?.cache).toBe('no-store');
    expect(mock.api.getBackendConnection).toHaveBeenCalledWith('claude');
    await waitFor(() => expect(primaryAction().textContent).toContain(en.onboarding.providers.actionContinue));
    await waitFor(() => expect(primaryAction().hasAttribute('disabled')).toBe(false));
  });

  it('keeps the second screen on the way back without re-establishing anything', async () => {
    await arriveAtProviders();
    fireEvent.click(primaryAction());
    const enter = await screen.findByRole('button', { name: 'Enter workspace' });
    await waitFor(() => expect(enter.hasAttribute('disabled')).toBe(false));

    const sourcesBeforeReturn = mock.models.listSources.mock.calls.length;
    const runtimeBeforeReturn = mock.models.getRuntimeStatus.mock.calls.length;
    fireEvent.click(backAction());
    await waitFor(() => expect(screenId()).toBe('providers'));
    // Establishing once is not observing once: coming back must show the machine as
    // it is now. What does not repeat is the write that made the controller answerable.
    await waitFor(() => expect(primaryAction().textContent).toContain(en.onboarding.providers.actionContinue));
    expect(seeds()).toHaveLength(1);
    expect(mock.models.getRuntimeStatus.mock.calls.length).toBeGreaterThan(runtimeBeforeReturn);
    expect(mock.models.listSources.mock.calls.length).toBeGreaterThan(sourcesBeforeReturn);
    expect(mock.control).not.toHaveBeenCalled();
    await waitFor(() => expect(primaryAction().hasAttribute('disabled')).toBe(false));

    fireEvent.click(primaryAction());
    await waitFor(() => expect(screenId()).toBe('assistants'));
    expect(seeds()).toHaveLength(1);
    const again = await screen.findByRole('button', { name: 'Enter workspace' });
    await waitFor(() => expect(again.hasAttribute('disabled')).toBe(false));
    fireEvent.click(again);
    expect(await screen.findByTestId('destination')).toBeTruthy();
  });

  it('makes current source supply usable once its own controller bootstrap succeeds', async () => {
    // Isolated against a real Wizard: the first supply read can fail while the
    // controller is still stopped, and after D11 brings IPC up the screen has to
    // take one current read — not stay on Retry for a world that no longer exists.
    running = false;
    const observedRunning: boolean[] = [];
    mock.models.listSources.mockImplementation(async () => {
      observedRunning.push(running);
      if (!running) throw new Error('controller unavailable');
      return [HUB_SOURCE];
    });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Get started' }));
    await waitFor(() => expect(mock.models.getRuntimeStatus).toHaveBeenCalled());
    expect(running).toBe(true);
    await waitFor(() => expect(primaryAction().textContent).toContain(en.onboarding.providers.actionContinue));
    expect(observedRunning).toContain(true);
    expect(primaryAction().hasAttribute('disabled')).toBe(false);
  });

  it('holds the journey on a failed establishment, and its own Retry gives it back', async () => {
    mock.models.getRuntimeStatus.mockRejectedValueOnce(new Error('runtime unreadable'));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Get started' }));
    await waitFor(() => expect(gateway().getAttribute('data-state')).toBe('failed'));
    // The shell's read failed, not an attempt this screen made — so the card names no
    // step, and the one Retry on screen is the card's rather than the footer's.
    expect(gateway().hasAttribute('data-failed-step')).toBe(false);
    expect(flowError()).toBeNull();
    expect(primaryAction().textContent).toContain(en.onboarding.providers.actionContinue);
    expect(primaryAction().hasAttribute('disabled')).toBe(true);
    const retry = gateway().querySelector('.setup-gateway-retry') as HTMLButtonElement;
    expect(retry.textContent).toContain(en.common.retry);
    expect(screenId()).toBe('providers');
    expectNoForwardWrite();

    // An explicit retry is the one thing that may seed again: the first attempt's write
    // was acknowledged, and only a person asking re-runs the sequence that made it.
    fireEvent.click(retry);
    await waitFor(() => expect(gateway().getAttribute('data-state')).toBe('running'));
    expect(seeds()).toHaveLength(2);
    await waitFor(() => expect(primaryAction().hasAttribute('disabled')).toBe(false));
    fireEvent.click(primaryAction());
    const enter = await screen.findByRole('button', { name: 'Enter workspace' });
    await waitFor(() => expect(enter.hasAttribute('disabled')).toBe(false));
    fireEvent.click(enter);
    expect(await screen.findByTestId('destination')).toBeTruthy();
  });
});

describe('setup route editor on the registered journey', () => {
  const hops = [
    { source_id: 'src_openai', model_id: 'gpt-5' },
    { source_id: 'src_openai', model_id: 'gpt-4.1' },
  ];
  const chainOf = (order = hops) => ({
    contract_version: 10,
    backend: 'claude' as const,
    model_id: 'opus-5',
    manual_override: { hops: order },
    route_origin: 'manual' as const,
    current: order[0],
    chain: order.map((hop) => ({
      ...hop,
      channel: 'hub' as const,
      health: 'healthy' as const,
      runnable: true,
      reason: null,
      retry_at: null,
    })),
    supply_state: 'ok' as const,
  });

  it('reorders the exact saved chain, keeps the draft through add-source, then enters', async () => {
    serveAgents('claude-agent', [{ ...CLAUDE_AGENT, model: 'opus-5' }]);
    mock.models.listAgents.mockResolvedValue([
      hubSupply('claude', [route('claude-agent', { effective_model_id: 'opus-5' })]),
    ]);
    mock.api.getBackendConnection.mockImplementation((backend) => Promise.resolve({
      ok: true, backend, enabled: true, installed: true, auth: 'none',
      application: 'applied', ready: backend === 'claude', entry_eligible: backend === 'claude',
      supply_mode: backend === 'claude' ? 'hub' : undefined,
    }));
    mock.models.getAgentChain.mockResolvedValue(chainOf());
    mock.models.previewAgentChain.mockImplementation(async (_backend, _model, body) =>
      chainOf(body.manual_override?.hops ?? hops));
    mock.models.putAgentChain.mockImplementation(async (_backend, _model, body) => {
      const next = chainOf(body.hops);
      mock.models.getAgentChain.mockResolvedValue(next);
      return { chain: next };
    });

    await setup();
    fireEvent.click(await assistantCard('Claude Code').findByRole('button', { name: routeButton('Claude Code') }));
    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.route.moveDownNamed.replace('{{name}}', 'OpenAI · gpt-5') }));
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.route.addSource }));
    await waitFor(() => expect(document.querySelector('[data-setup-screen]')?.getAttribute('data-setup-screen')).toBe('providers'));
    expect(mock.models.putAgentChain).not.toHaveBeenCalled();

    await waitFor(() => expect(primaryAction().textContent).toContain(en.onboarding.providers.actionContinue));
    await waitFor(() => expect(primaryAction().hasAttribute('disabled')).toBe(false));
    fireEvent.click(primaryAction());
    await screen.findByRole('button', { name: 'Enter workspace' });
    fireEvent.click(assistantCard('Claude Code').getByRole('button', { name: routeButton('Claude Code') }));
    expect((await screen.findByText(en.onboarding.route.preferred)).closest('.setup-add-row')?.textContent).toContain('gpt-4.1');

    fireEvent.click(screen.getByRole('button', { name: en.onboarding.route.done }));
    await waitFor(() => expect(mock.models.putAgentChain).toHaveBeenCalledWith('claude', 'opus-5', { hops: [hops[1], hops[0]] }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    fireEvent.click(assistantCard('Claude Code').getByRole('button', { name: routeButton('Claude Code') }));
    expect((await screen.findByText(en.onboarding.route.preferred)).closest('.setup-add-row')?.textContent).toContain('gpt-4.1');
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.route.done }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Enter workspace' }));
    expect(await screen.findByTestId('destination')).toBeTruthy();
    expect(mock.api.mutateConfig).toHaveBeenCalledExactlyOnceWith([{ kind: 'set', path: ['setup_completed'], value: true }]);
    expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
  });

  it('selects the first exact hop on an empty route, then enters', async () => {
    const user = userEvent.setup();
    let routed = false;
    serveAgents('claude-agent', [{ ...CLAUDE_AGENT, model: 'opus-5' }]);
    mock.models.listAgents.mockResolvedValue([
      hubSupply('claude', [route('claude-agent', { effective_model_id: 'opus-5' })]),
    ]);
    mock.api.getBackendConnection.mockImplementation((backend) => Promise.resolve({
      ok: true, backend, enabled: true, installed: true, auth: 'none',
      application: 'applied', ready: backend === 'claude' && routed, entry_eligible: backend === 'claude' && routed,
      supply_mode: backend === 'claude' ? 'hub' : undefined,
    }));
    mock.models.getAgentChain.mockResolvedValue(chainOf([]));
    mock.models.previewAgentChain.mockImplementation(async (_backend, _model, body) =>
      chainOf(body.manual_override?.hops ?? []));
    mock.models.putAgentChain.mockImplementation(async (_backend, _model, body) => {
      routed = true;
      const next = chainOf(body.hops);
      mock.models.getAgentChain.mockResolvedValue(next);
      return { chain: next };
    });

    await arriveAtProviders();
    fireEvent.click(primaryAction());
    const enter = await screen.findByRole('button', { name: 'Enter workspace' });
    await waitFor(() => expect(enter.hasAttribute('disabled')).toBe(true));
    // Nothing is routed yet, so the card offers the route itself rather than a model.
    fireEvent.click(await assistantCard('Claude Code').findByRole('button', { name: en.onboarding.setup.configureRoute }));
    await user.click(await screen.findByRole('button', { name: en.settings.models.routeDialog.addHop }));
    await user.click(screen.getByRole('option', { name: /gpt-5/ }));
    await user.click(screen.getByRole('button', { name: en.settings.models.routeDialog.add.confirm }));
    await user.click(screen.getByRole('button', { name: en.onboarding.route.done }));
    await waitFor(() => expect(mock.models.putAgentChain).toHaveBeenCalledWith('claude', 'opus-5', { hops: [hops[0]] }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(enter.hasAttribute('disabled')).toBe(false));

    fireEvent.click(enter);
    expect(await screen.findByTestId('destination')).toBeTruthy();
    expect(mock.api.mutateConfig).toHaveBeenCalledExactlyOnceWith([{ kind: 'set', path: ['setup_completed'], value: true }]);
    expect(mock.api.setDefaultVibeAgent).not.toHaveBeenCalled();
  });

  it('saves the Codex card onto Codex and refreshes Enter after the real PUT', async () => {
    const user = userEvent.setup();
    const saved: Record<string, typeof hops> = { claude: [], codex: [] };
    serveAgents('claude-agent', [
      { ...CLAUDE_AGENT, model: 'opus-5' },
      { name: 'codex', backend: 'codex', model: 'gpt-5' },
    ]);
    mock.models.listAgents.mockResolvedValue([
      hubSupply('claude', [route('claude-agent', { effective_model_id: 'opus-5' })]),
      hubSupply('codex', [route('codex', { effective_model_id: 'gpt-5' })]),
    ]);
    mock.api.getBackendConnection.mockImplementation((backend) => Promise.resolve({
      ok: true, backend, enabled: true, installed: true, auth: 'none',
      application: 'applied',
      ready: Boolean(saved[backend]?.length),
      entry_eligible: Boolean(saved[backend]?.length),
      supply_mode: 'hub',
    }));
    mock.models.getAgentChain.mockImplementation(async (backend: AgentBackend, model: string) => ({
      ...chainOf(saved[backend] ?? []),
      backend,
      model_id: model,
    }));
    mock.models.previewAgentChain.mockImplementation(async (backend, model, body) => ({
      ...chainOf(body.manual_override?.hops ?? []),
      backend,
      model_id: model,
    }));
    mock.models.putAgentChain.mockImplementation(async (backend, model, body) => {
      saved[backend] = body.hops;
      return { chain: { ...chainOf(body.hops), backend, model_id: model } };
    });

    await arriveAtProviders();
    fireEvent.click(primaryAction());
    const enter = await screen.findByRole('button', { name: 'Enter workspace' });
    await waitFor(() => expect(enter.hasAttribute('disabled')).toBe(true));
    fireEvent.click(await assistantCard('Codex').findByRole('button', { name: en.onboarding.setup.configureRoute }));
    await user.click(await screen.findByRole('button', { name: en.settings.models.routeDialog.addHop }));
    await user.click(screen.getByRole('option', { name: /gpt-5/ }));
    await user.click(screen.getByRole('button', { name: en.settings.models.routeDialog.add.confirm }));
    await user.click(screen.getByRole('button', { name: en.onboarding.route.done }));
    await waitFor(() => expect(mock.models.putAgentChain).toHaveBeenCalledWith('codex', 'gpt-5', { hops: [hops[0]] }));
    expect(mock.models.putAgentChain).not.toHaveBeenCalledWith('claude', expect.anything(), expect.anything());
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(enter.hasAttribute('disabled')).toBe(false));
    fireEvent.click(enter);
    expect(await screen.findByTestId('destination')).toBeTruthy();
  });
});
