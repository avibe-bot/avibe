// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/context/ToastProvider';
import i18n from '@/i18n';
import { MANAGE_COMMIT_ACTIONS } from './manage';
import type { ModelsSurfaceKind } from './modelHubSurfaceState';
import { modelsApi } from './modelsApi';
import { SOURCE_MUTATION_REPORT_PROJECTIONS } from './mutationSettlement';
import { SettingsModelsPage } from './SettingsModelsPage';
import { CONTRACT_VERSION, type AgentBackend, type AgentChain, type AgentSupply, type BackendModel, type RuntimeDependency, type Source, type UsageSummary } from './types';

const directAgent = (backend: AgentBackend): AgentSupply => ({
  backend,
  cli_present: true,
  mode: 'direct',
  menu_kind: backend === 'opencode' ? 'open' : 'fixed',
});

const runtime: RuntimeDependency = {
  contract_version: 7,
  manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1', source_sha: 'a'.repeat(40), assets: [] },
  status: { installed_version: '1', verified: true, health: 'ok' },
};

const retainedSource: Source = {
  id: 'src_retained',
  last_discovered_at: null,
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: 'Retained source',
  protocol: 'anthropic',
  base_url: null,
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'standby', retry_at: null, detail_key: null },
  masked_credential: null,
  models: [],
};

const nativeSubscription: Source = {
  ...retainedSource,
  id: 'src_native_subscription',
  kind: 'subscription',
  supply_channel: 'native_cli',
  display_name: 'Claude native login',
};

/** The row every re-auth journey starts from: a subscription that stopped. */
const blockedSubscription: Source = {
  ...nativeSubscription,
  state: { status: 'needs_action', retry_at: null, detail_key: 'models.source.needs_action.oauth_expired' },
};

const takeoverAgent: AgentSupply = {
  backend: 'codex',
  cli_present: true,
  mode: 'hub',
  menu_kind: 'fixed',
  selected_model_id: 'gpt-5.6-sol',
  selected_model_explicit: true,
  sources: { order: ['src_head', 'src_relay'], eligibility: [] },
  routes: { 'gpt-5.6-sol': { hops: [{ source_id: 'src_head', model_id: 'gpt-5.6-sol' }, { source_id: 'src_relay', model_id: 'gpt-5.6-sol' }] } },
  supply_status: 'degraded',
  model_supply: [{ model_id: 'gpt-5.6-sol', chain_length: 2, has_runnable_hop: true }],
  builtin_models: ['gpt-5.6-sol'],
  named_agents: [],
  menu: null,
};

const takeoverChain: AgentChain = {
  contract_version: 7,
  backend: 'codex',
  model_id: 'gpt-5.6-sol',
  current: { source_id: 'src_relay', model_id: 'gpt-5.6-sol' },
  chain: [
    { source_id: 'src_head', model_id: 'gpt-5.6-sol', channel: 'hub', health: 'cooldown', runnable: false, reason: null, retry_at: '2099-01-01T00:00:00Z' },
    { source_id: 'src_relay', model_id: 'gpt-5.6-sol', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null },
  ],
  supply_state: 'ok',
};

const takeoverMappingTitle = /Replacement source → gpt-5\.6-sol \((?:Taken over|已自动切换)\)/i;
const headMappingTitle = /^Paused source → gpt-5\.6-sol$/i;

const usageSummary: UsageSummary = {
  window_days: 30,
  from_day: '2026-07-20',
  to_day: '2026-08-18',
  totals: { requests: 12, token_reports: 12, input_tokens: 148230, cached_input_tokens: 96010, output_tokens: 4120 },
  sources: [{
    source_id: 'src_retained',
    label: 'Retained source',
    last_metered_at: '2026-08-18T03:14:00+00:00',
    requests: 12,
    token_reports: 12,
    input_tokens: 148230,
    cached_input_tokens: 96010,
    output_tokens: 4120,
    models: [{ model_id: 'claude-opus-4-6', label: 'claude-opus-4-6', requests: 12, token_reports: 12, input_tokens: 148230, cached_input_tokens: 96010, output_tokens: 4120 }],
  }],
  days: [{ day: '2026-08-18', requests: 12, token_reports: 12, input_tokens: 148230, cached_input_tokens: 96010, output_tokens: 4120 }],
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

const renderPage = (sources: Source[]) => {
  vi.spyOn(modelsApi, 'listSources').mockResolvedValue(sources);
  vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([
    directAgent('claude'),
    directAgent('codex'),
    directAgent('opencode'),
  ]);
  vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
  vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
  vi.spyOn(modelsApi, 'getUsageSummary').mockResolvedValue(usageSummary);
  return render(
    <ToastProvider>
      <I18nextProvider i18n={i18n}>
        <SettingsModelsPage />
      </I18nextProvider>
    </ToastProvider>,
  );
};

const switchFirstGatewayAgentToDirect = async () => {
  await userEvent.click((await screen.findAllByRole('button', { name: /Runtime mode:|运行模式[:：]/i }))[0]);
  const modeGroup = await screen.findByRole('group', { name: /Runtime mode|运行模式/i });
  await userEvent.click(within(modeGroup).getByRole('button', { name: /Switch to direct|切到直连|Retry|重试/i }));
};

const closeSourceDetails = async () => {
  await userEvent.click(screen.getByRole('button', { name: /Close provider details|关闭供应商详情/i }));
};

beforeEach(() => {
  vi.spyOn(modelsApi, 'refreshAgentPresence').mockReturnValue(new Promise(() => {}));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('SettingsModelsPage surface branches', () => {
  it('acknowledges a catalog save and reveals a new model beyond the collapsed limit', async () => {
    const ids = Array.from({ length: 6 }, (_, index) => `catalog-model-${index + 1}`);
    const addedId = 'catalog-model-added';
    const model = (id: string): BackendModel => ({
      id, display_name: null, origin: 'manual', models_dev_id: null, context_window: null, max_output_tokens: null,
      input_modalities: ['text'], output_modalities: ['text'], supports_tools: true, supports_reasoning: false,
      reasoning_efforts: [], locked: false, routeable: true,
    });
    const agent = (modelIds: string[]): AgentSupply => ({
      ...takeoverAgent,
      selected_model_id: modelIds[0] ?? null,
      routes: {},
      model_supply: modelIds.map((modelId) => ({ model_id: modelId, chain_length: 0, has_runnable_hop: false })),
      builtin_models: modelIds,
      catalog_models: modelIds.map(model),
    });
    const initial = agent(ids);
    const saved = agent([...ids, addedId]);
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValueOnce([initial]).mockResolvedValue([saved]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'getAgentSources').mockResolvedValue(initial);
    vi.spyOn(modelsApi, 'putAgentModels').mockResolvedValue(saved);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    await userEvent.click(await screen.findByRole('button', { name: /^Manage models$|^管理模型$/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Add model$|^添加模型$/i }));
    await userEvent.type(screen.getByLabelText(/^Backend model ID$|^后端模型 ID$/i), addedId);
    await userEvent.click(screen.getByRole('button', { name: /^Add model$|^添加模型$/i }));
    await userEvent.click(screen.getByRole('button', { name: /^Save$|^保存$/i }));

    expect(await screen.findByText(/^Model list saved\.$|^模型列表已保存。$/i)).toBeTruthy();
    expect(screen.getByText(addedId)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: /^Collapse$|^收起$/i }));
    expect(screen.queryByText(addedId)).toBeNull();
  });

  it('coalesces the OAuth success landing with its trailing stale-row notification', () => {
    const page = readFileSync(join(process.cwd(), 'src/components/settings/models/SettingsModelsPage.tsx'), 'utf8');
    const start = page.indexOf('const subscriptionAdded');
    const callback = page.slice(start, page.indexOf('const closeSubscription', start));

    expect(callback).toMatch(/if \(!source\) \{[\s\S]*?subscriptionSuccessReconcileRef\.current/);
    expect(callback).toMatch(/sourceEntityAuthority\.landLatest\(source\)[\s\S]*?subscriptionSuccessReconcileRef\.current = true;[\s\S]*?void refresh\(\)/);
    expect((callback.match(/void refresh\(\)/g) ?? []).length).toBe(2);
  });

  it('issues exactly one surface refresh for a successful subscription create', async () => {
    const created = {
      ...nativeSubscription,
      id: 'src_created_subscription',
      display_name: 'Created subscription',
    };
    const terminal = {
      flow_id: 'flow_created_subscription',
      client_nonce: 'ofn_created_subscription',
      vendor: 'anthropic',
      channel: 'native_cli' as const,
      state: 'success' as const,
      presentation: { expects: 'none' as const },
      expires_at: '2099-01-01T00:00:00Z',
    };
    vi.spyOn(modelsApi, 'startOAuth').mockResolvedValue(terminal);
    const status = vi.spyOn(modelsApi, 'getOAuthStatus').mockResolvedValue({
      flow: terminal,
      created: { source: created, added_to: [], adopted_by: [] },
      repaired: null,
    });
    renderPage([retainedSource]);

    await screen.findByText('Retained source');
    const listSources = vi.mocked(modelsApi.listSources);
    const refreshesBeforeCreate = listSources.mock.calls.length;
    listSources.mockResolvedValue([retainedSource, created]);
    const user = userEvent.setup();
    const trigger = screen.getByRole('button', { name: /Add subscription|添加订阅/i });
    await user.click(trigger);
    await user.click(await screen.findByRole('menuitem', { name: /Claude subscription|Claude 订阅/i }));
    await user.click(screen.getByRole('button', { name: /Sign in|去登录/i }));

    await waitFor(() => expect(status).toHaveBeenCalledWith(terminal.flow_id));
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(screen.queryByRole('dialog', { name: 'Created subscription' })).toBeNull();
    await waitFor(() => expect(listSources).toHaveBeenCalledTimes(refreshesBeforeCreate + 1));
    await act(async () => Promise.resolve());
    expect(listSources).toHaveBeenCalledTimes(refreshesBeforeCreate + 1);
    const detail = await screen.findByRole('dialog', { name: 'Created subscription' }, { timeout: 2500 });
    await user.click(within(detail).getByRole('button', { name: /Close provider details|关闭供应商详情/i }));
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('returns focus to Add API key after opening and closing the created provider', async () => {
    const created: Source = {
      ...retainedSource,
      id: 'src_created_api_key',
      vendor: 'custom',
      display_name: 'Created API key',
      protocol: 'openai_chat',
      base_url: 'https://relay.example/v1',
    };
    vi.spyOn(modelsApi, 'observeApiKeySource').mockResolvedValue({
      contract_version: CONTRACT_VERSION,
      outcome: 'observed',
      reachable: true,
      authenticated: 'authenticated',
      protocol: 'openai_chat',
      discovery: 'succeeded',
      models: ['model-a'],
    });
    vi.spyOn(modelsApi, 'createApiKeySource').mockResolvedValue({
      source: created,
      added_to: [],
      adopted_by: [],
    });
    renderPage([retainedSource]);

    await screen.findByText('Retained source');
    vi.mocked(modelsApi.listSources).mockResolvedValue([retainedSource, created]);
    const user = userEvent.setup();
    const trigger = screen.getByRole('button', { name: /Add API key|添加 API Key/i });
    await user.click(trigger);
    await user.type(screen.getByRole('textbox', { name: /^Base URL$/i }), 'https://relay.example/v1');
    await user.type(screen.getByLabelText(/^API key$/i), 'secret-key');
    await user.click(screen.getByRole('button', { name: /^Add$|^添加$/i }));

    const detail = await screen.findByRole('dialog', { name: 'Created API key' });
    await user.click(within(detail).getByRole('button', { name: /Close provider details|关闭供应商详情/i }));
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('renders Frame 09 as the sources tab when every backend is direct and no source exists', async () => {
    renderPage([]);

    expect(await screen.findByText(/^Currently: direct$|^当前:直连$/i)).toBeTruthy();
    expect(screen.getAllByRole('button', { name: /^Switch to Gateway$|^切换到模型网关$/i })).toHaveLength(3);
    expect(screen.getByText(/^Switch to the gateway and you gain three things$|^切换到模型网关，你会多出三件事$/i)).toBeTruthy();
    // Frame 09 is what the `sources` tab shows here — not what the Hub shows
    // instead of its tabs. It is still Frame 09's body: none of the gateway
    // overview leaks in beside it.
    expect(screen.getAllByRole('tab')).toHaveLength(3);
    expect(screen.queryByText(/^Recent switches$|^最近切换$/i)).toBeNull();
  });

  it('renders the no-backend state when every authoritative CLI is absent', async () => {
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([
      { ...directAgent('claude'), cli_present: false },
      { ...directAgent('codex'), cli_present: false },
      { ...directAgent('opencode'), cli_present: false },
    ]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByText(/No agent backend was found|没有找到 Agent 后端/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^Switch to Gateway$|^切换到模型网关$/i })).toBeNull();
    expect(screen.queryByText(/backends are direct|个后端均为直连/i)).toBeNull();
  });

  it('publishes a newly discovered backend into the already-open page', async () => {
    const unavailable = [
      { ...directAgent('claude'), cli_present: false },
      { ...directAgent('codex'), cli_present: false },
      { ...directAgent('opencode'), cli_present: false },
    ];
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    const available = [
      unavailable[0],
      directAgent('codex'),
      unavailable[2],
    ];
    vi.spyOn(modelsApi, 'listAgents')
      .mockResolvedValueOnce(unavailable)
      .mockResolvedValue(available);
    vi.mocked(modelsApi.refreshAgentPresence).mockResolvedValue(available);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByRole('button', { name: /^Switch to Gateway$|^切换到模型网关$/i })).toBeTruthy();
    expect(screen.queryByText(/No agent backend was found|没有找到 Agent 后端/i)).toBeNull();
    expect(modelsApi.refreshAgentPresence).toHaveBeenCalledOnce();
  });

  it('retries deep backend detection without remounting the page', async () => {
    const unavailable = [
      { ...directAgent('claude'), cli_present: false },
      { ...directAgent('codex'), cli_present: false },
      { ...directAgent('opencode'), cli_present: false },
    ];
    const available = [
      unavailable[0],
      directAgent('codex'),
      unavailable[2],
    ];
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'listAgents')
      .mockResolvedValueOnce(unavailable)
      .mockResolvedValueOnce(unavailable)
      .mockResolvedValue(available);
    vi.mocked(modelsApi.refreshAgentPresence)
      .mockResolvedValueOnce(unavailable)
      .mockResolvedValueOnce(available);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByText(/No agent backend was found|没有找到 Agent 后端/i)).toBeTruthy();
    const retry = await screen.findByRole('button', { name: /Detect Agent backends again|重新检测 Agent 后端/i });
    await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(false));
    const user = userEvent.setup();
    await user.click(retry);

    expect(await screen.findByRole('button', { name: /^Switch to Gateway$|^切换到模型网关$/i })).toBeTruthy();
    expect(modelsApi.refreshAgentPresence).toHaveBeenCalledTimes(2);
  });

  it('keeps an unread runtime visible on the direct-only surface', async () => {
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([
      directAgent('claude'),
      directAgent('codex'),
      directAgent('opencode'),
    ]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockRejectedValue(new TypeError('status unread'));
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByText(/^Gateway status unavailable$|^模型网关状态不可用$/i)).toBeTruthy();
    expect(screen.queryByText(/^All 3 backends are direct$|^3 个后端均为直连$/i)).toBeNull();
  });

  it('gates all internal configuration behind the stopped runtime switch', async () => {
    const stopped = { ...runtime, status: { ...runtime.status, health: 'down' as const } };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([
      directAgent('claude'),
      directAgent('codex'),
      directAgent('opencode'),
    ]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(stopped);
    const start = vi.spyOn(modelsApi, 'startRuntime').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    const toggle = await screen.findByRole('switch', { name: /Turn model gateway on|开启模型网关/i });
    expect(toggle.getAttribute('aria-checked')).toBe('false');
    expect(await screen.findByText(/^Model gateway is off$|^模型网关已关闭$/i)).toBeTruthy();
    expect(screen.queryAllByRole('tab')).toHaveLength(0);
    expect(screen.queryByText(/^All 3 backends are direct$|^3 个后端均为直连$/i)).toBeNull();

    await userEvent.click(toggle);

    await waitFor(() => expect(start).toHaveBeenCalledOnce());
    expect(await screen.findAllByRole('tab')).toHaveLength(3);
  });

  it('keeps routing controls available while an enabled gateway process is unavailable', async () => {
    const unavailable = {
      ...runtime,
      enabled: true,
      status: { ...runtime.status, health: 'down' as const },
    };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([retainedSource]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([takeoverAgent]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(unavailable);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    const toggle = await screen.findByRole('switch', { name: /Switch codex to Direct|请先将 codex 切换为直连/i });
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    expect((toggle as HTMLButtonElement).disabled).toBe(true);
    expect(await screen.findByText('Retained source')).toBeTruthy();
    expect(screen.getByRole('button', { name: /Runtime mode:|运行模式[:：]/i })).toBeTruthy();
    expect(screen.queryAllByRole('tab')).toHaveLength(3);
  });

  it('still allows persisted enablement to be turned off on an unsupported host', async () => {
    const unsupported = {
      ...runtime,
      enabled: true,
      manifest: { ...runtime.manifest, resolution: 'unsupported' as const },
      status: { ...runtime.status, health: 'not_installed' as const },
    };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([
      directAgent('claude'),
      directAgent('codex'),
      directAgent('opencode'),
    ]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(unsupported);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    const stopped = { ...unsupported, enabled: false };
    const stop = vi.spyOn(modelsApi, 'stopRuntime').mockResolvedValue(stopped);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    const toggle = await screen.findByRole('switch', { name: /Turn model gateway off|关闭模型网关/i });
    expect((toggle as HTMLButtonElement).disabled).toBe(false);
    await userEvent.click(toggle);
    await waitFor(() => expect(stop).toHaveBeenCalledOnce());
  });

  it('stops an unused running gateway and hides its retained configuration', async () => {
    renderPage([retainedSource]);
    const stopped = { ...runtime, status: { ...runtime.status, health: 'not_started' as const } };
    const stop = vi.spyOn(modelsApi, 'stopRuntime').mockResolvedValue(stopped);

    await screen.findByText('Retained source');
    await userEvent.click(screen.getByRole('switch', { name: /Turn model gateway off|关闭模型网关/i }));

    await waitFor(() => expect(stop).toHaveBeenCalledOnce());
    expect(await screen.findByText(/^Model gateway is off$|^模型网关已关闭$/i)).toBeTruthy();
    expect(screen.queryByText('Retained source')).toBeNull();
    expect(screen.queryAllByRole('tab')).toHaveLength(0);
  });

  it('does not turn the runtime off while a backend still uses the gateway', async () => {
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([retainedSource]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([takeoverAgent]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    const stop = vi.spyOn(modelsApi, 'stopRuntime').mockResolvedValue(runtime);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    const toggle = await screen.findByRole('switch', { name: /Switch codex to Direct|请先将 codex 切换为直连/i });
    expect((toggle as HTMLButtonElement).disabled).toBe(true);
    await userEvent.click(toggle);
    expect(stop).not.toHaveBeenCalled();
    expect(await screen.findByText('Retained source')).toBeTruthy();
  });

  it('renders Frame 01 with tabs when retained sources remain under all-direct backends', async () => {
    renderPage([retainedSource]);

    expect(await screen.findByText('Retained source')).toBeTruthy();
    expect(screen.getAllByRole('tab')).toHaveLength(3);
    expect(screen.queryByText(/^Switch to the gateway and you gain three things$|^切换到模型网关，你会多出三件事$/i)).toBeNull();
  });

  it('keeps tier-editor Escape local to the provider dialog', async () => {
    const editableSource: Source = {
      ...retainedSource,
      models: [{ id: 'model-a', display_name: null, origin: 'manual', reasoning_efforts: ['high'] }],
    };
    renderPage([editableSource]);
    const user = userEvent.setup();

    const sourceOpener = (await screen.findByText('Retained source')).closest('button') as HTMLButtonElement;
    await user.click(sourceOpener);
    const sourceDialog = await screen.findByRole('dialog', { name: 'Retained source' });
    await user.click(within(sourceDialog).getByRole('button', { name: /high/i }));
    const tierInput = within(sourceDialog).getByPlaceholderText(/Enter to add|回车添加/i);
    await user.type(tierInput, 'draft');
    await user.keyboard('{Escape}');

    expect(screen.getByRole('dialog', { name: 'Retained source' })).toBeTruthy();
    expect(within(sourceDialog).queryByPlaceholderText(/Enter to add|回车添加/i)).toBeNull();

    await user.click(within(sourceDialog).getByRole('button', { name: /^Add model$|^添加模型$/i }));
    let manualDraft = sourceDialog.querySelector('[data-manual-model-draft]');
    const modelIdInput = within(manualDraft as HTMLElement).getByPlaceholderText(/^Model ID$|^模型 ID$/i);
    await user.type(modelIdInput, 'draft-model');
    await user.keyboard('{Escape}');

    expect(screen.getByRole('dialog', { name: 'Retained source' })).toBeTruthy();
    expect(sourceDialog.querySelector('[data-manual-model-draft]')).toBeNull();

    await user.click(within(sourceDialog).getByRole('button', { name: /^Add model$|^添加模型$/i }));
    manualDraft = sourceDialog.querySelector('[data-manual-model-draft]');
    const draftTierInput = within(manualDraft as HTMLElement).getByPlaceholderText(/Enter to add|回车添加/i);
    await user.type(draftTierInput, 'draft');
    await user.keyboard('{Escape}');

    expect(screen.getByRole('dialog', { name: 'Retained source' })).toBeTruthy();
    expect((draftTierInput as HTMLInputElement).value).toBe('');

    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Retained source' })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(sourceOpener));
  });

  it('moves recent switches into the Logs tab and removes the Advanced placeholder', async () => {
    renderPage([retainedSource]);

    await screen.findByText('Retained source');
    const events = vi.mocked(modelsApi.listEvents);
    expect(events).not.toHaveBeenCalled();
    expect(screen.queryByText(/^Recent switches$|^最近切换$/i)).toBeNull();
    expect(screen.queryByText(/Cross-vendor auto-substitution|跨厂商自动顶替/i)).toBeNull();

    await userEvent.click(await screen.findByRole('tab', { name: /^Logs$|^日志$/i }));

    expect(await screen.findByText(/^Recent switches$|^最近切换$/i)).toBeTruthy();
    expect(screen.queryByText('Retained source')).toBeNull();
    await waitFor(() => expect(events).toHaveBeenCalledTimes(1));
  });

  it('opens the subscription vendor picker and OAuth flow from the Sources card', async () => {
    renderPage([retainedSource]);

    await screen.findByText('Retained source');
    await userEvent.click(screen.getByRole('button', { name: /Add subscription|添加订阅/i }));
    const picker = await screen.findByRole('menu');
    expect(within(picker).getByText(/Native recommended|推荐由 Agent 管理/i)).toBeTruthy();
    expect(within(picker).getByText(/Gateway recommended|推荐由模型网关管理/i)).toBeTruthy();
    expect(within(picker).queryByText(/Claude Pro \/ Max|ChatGPT Plus \/ Pro/i)).toBeNull();
    await userEvent.click(within(picker).getByRole('menuitem', { name: /Claude subscription|Claude 订阅/i }));

    expect(screen.queryByRole('menu')).toBeNull();
    expect((await screen.findByRole('dialog')).textContent).toMatch(/Add Claude subscription|添加 anthropic 订阅/i);
  });

  it('supports roving keyboard navigation in the subscription vendor picker', async () => {
    renderPage([retainedSource]);

    const user = userEvent.setup();
    await screen.findByText('Retained source');
    const trigger = screen.getByRole('button', { name: /Add subscription|添加订阅/i });
    await user.click(trigger);
    const picker = await screen.findByRole('menu');
    const claude = within(picker).getByRole('menuitem', { name: /Claude subscription|Claude 订阅/i });
    const chatgpt = within(picker).getByRole('menuitem', { name: /ChatGPT subscription|ChatGPT 订阅/i });

    await waitFor(() => expect(document.activeElement).toBe(claude));
    expect(claude.getAttribute('tabindex')).toBe('0');
    expect(chatgpt.getAttribute('tabindex')).toBe('-1');
    await user.keyboard('{ArrowDown}');
    expect(document.activeElement).toBe(chatgpt);
    await user.keyboard('{Home}');
    expect(document.activeElement).toBe(claude);
    await user.keyboard('{End}');
    expect(document.activeElement).toBe(chatgpt);
    await user.keyboard('{ArrowUp}');
    expect(document.activeElement).toBe(claude);
  });

  it('restores the Add subscription trigger when the vendor picker is dismissed', async () => {
    renderPage([retainedSource]);

    const user = userEvent.setup();
    await screen.findByText('Retained source');
    const trigger = screen.getByRole('button', { name: /Add subscription|添加订阅/i });
    await user.click(trigger);
    await screen.findByRole('menu');
    await user.keyboard('{Escape}');

    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('restores the Add subscription trigger when an outside press dismisses the vendor picker', async () => {
    renderPage([retainedSource]);

    const user = userEvent.setup();
    await screen.findByText('Retained source');
    const trigger = screen.getByRole('button', { name: /Add subscription|添加订阅/i });
    await user.click(trigger);
    await screen.findByRole('menu');
    await user.click(document.body);

    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('opens an occupied native vendor with the gateway option selected', async () => {
    renderPage([nativeSubscription]);

    const user = userEvent.setup();
    await screen.findByText('Claude native login');
    await user.click(screen.getByRole('button', { name: /Add subscription|添加订阅/i }));
    await user.click(await screen.findByRole('menuitem', { name: /Claude subscription|Claude 订阅/i }));

    const native = await screen.findByRole('radio', { name: /Native|原生/i });
    const hub = screen.getByRole('radio', { name: /Gateway|网关/i });
    expect(native.getAttribute('aria-disabled')).toBe('true');
    expect(hub.getAttribute('aria-checked')).toBe('true');
    await waitFor(() => expect(document.activeElement).toBe(hub));
  });

  it('returns focus to Add subscription when the OAuth flow is cancelled', async () => {
    renderPage([retainedSource]);

    await screen.findByText('Retained source');
    const trigger = screen.getByRole('button', { name: /Add subscription|添加订阅/i });
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole('menuitem', { name: /Claude subscription|Claude 订阅/i }));
    const cancelButtons = screen.getAllByRole('button', { name: /^Cancel$|^取消$/i });
    await userEvent.click(cancelButtons[cancelButtons.length - 1]);

    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  // The wiring §4.5 was missing, end to end. `repair.ts` had decided the remedy
  // and `OAuthConnectDialog` had run the re-login journey since it shipped, but no
  // mount connected them, so a stopped subscription rendered its cause and
  // stopped. The proof is the request: from the row a user can reach, one tap and
  // one confirm reach `POST …/reauth` for THAT source.
  it('reaches the re-auth request from a stopped subscription row', async () => {
    const started = {
      flow_id: 'flow_reauth',
      intent: 'reauth' as const,
      vendor: 'anthropic',
      channel: 'native_cli' as const,
      state: 'starting' as const,
      presentation: { expects: 'none' as const },
    };
    const reauth = vi.spyOn(modelsApi, 'reauthSource').mockResolvedValue(started);
    vi.spyOn(modelsApi, 'getOAuthStatus').mockResolvedValue(started);
    renderPage([blockedSubscription]);

    await userEvent.click(await screen.findByRole('button', { name: /Claude native login/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Sign in$|^重新登录$/i }));
    expect(reauth).not.toHaveBeenCalled();
    await userEvent.click(await screen.findByRole('button', { name: /^Start sign-in$|^开始登录$/i }));

    await waitFor(() => expect(reauth).toHaveBeenCalledWith(blockedSubscription.id));
  });

  // The confirm IS this journey's gesture — the dialog it opens POSTs as it mounts,
  // so nothing after it can be granted a tab. Asserted where the user feels it: the
  // provider page lands in the tab, instead of behind a blocked popup and a link
  // the user has to notice.
  it('lands the provider page in the tab the re-auth confirmation opened', async () => {
    const authUrl = 'https://provider.example/authorize?code=1';
    const started = {
      flow_id: 'flow_reauth',
      intent: 'reauth' as const,
      vendor: 'anthropic',
      channel: 'native_cli' as const,
      state: 'awaiting_action' as const,
      presentation: { expects: 'paste_callback_url' as const, auth_url: authUrl },
    };
    vi.spyOn(modelsApi, 'reauthSource').mockResolvedValue(started);
    vi.spyOn(modelsApi, 'getOAuthStatus').mockResolvedValue(started);
    const tab = { closed: false, opener: {} as unknown, location: { href: '' } };
    const open = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
    renderPage([blockedSubscription]);

    await userEvent.click(await screen.findByRole('button', { name: /Claude native login/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Sign in$|^重新登录$/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Start sign-in$|^开始登录$/i }));

    expect(open).toHaveBeenCalledWith('about:blank', '_blank');
    expect(tab.opener).toBeNull();
    await waitFor(() => expect(tab.location.href).toBe(authUrl));
  });

  // A failed re-auth has already spent the irreversible half — the sibling sources
  // are already marked. Sending the user back to the row to agree to that cost a
  // second time, for the only gesture that can undo it, is the one journey where a
  // second confirmation is worse than none.
  it('retries a failed re-auth in place, without asking to confirm again', async () => {
    const started = {
      flow_id: 'flow_reauth',
      intent: 'reauth' as const,
      vendor: 'anthropic',
      channel: 'native_cli' as const,
      state: 'starting' as const,
      presentation: { expects: 'none' as const },
    };
    const reauth = vi
      .spyOn(modelsApi, 'reauthSource')
      .mockRejectedValueOnce(new Error('start failed'))
      .mockResolvedValue(started);
    vi.spyOn(modelsApi, 'getOAuthStatus').mockResolvedValue(started);
    renderPage([blockedSubscription]);

    await userEvent.click(await screen.findByRole('button', { name: /Claude native login/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Sign in$|^重新登录$/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Start sign-in$|^开始登录$/i }));

    const retry = await screen.findByRole('button', { name: /^Retry$|^重试$/i });
    expect(screen.queryByRole('button', { name: /^Start sign-in$|^开始登录$/i })).toBeNull();
    await userEvent.click(retry);

    await waitFor(() => expect(reauth).toHaveBeenCalledTimes(2));
  });

  it('lands the operational overview without reading hidden event history', async () => {
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([
      directAgent('claude'),
      directAgent('codex'),
      directAgent('opencode'),
    ]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    const events = vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByText(/^Currently: direct$|^当前:直连$/i)).toBeTruthy();
    expect(screen.getAllByRole('button', { name: /^Switch to Gateway$|^切换到模型网关$/i })).toHaveLength(3);
    expect(events).not.toHaveBeenCalled();
  });

  it('counts a recoverable reroute once per backend in Frame 08', async () => {
    const head = { ...retainedSource, id: 'src_head', display_name: 'Paused source', state: { status: 'cooldown' as const, retry_at: '2099-01-01T00:00:00Z', detail_key: null } };
    const relay = { ...retainedSource, id: 'src_relay', display_name: 'Replacement source', state: { status: 'active' as const, retry_at: null, detail_key: null } };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([head, relay]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([takeoverAgent]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'getAgentChains').mockResolvedValue([takeoverChain]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByText(/^1 takeover active$|^1 处已自动切换$/i)).toBeTruthy();
    expect(screen.getByText(/^Taken over$|^已自动切换$/i)).toBeTruthy();
    expect(screen.getByTitle(takeoverMappingTitle)).toBeTruthy();
  });

  it('[MH-OVERVIEW-001] reads overview chains once per backend and keeps exact reads for the route dialog', async () => {
    const secondModel = 'gpt-5.6-terra';
    const agent: AgentSupply = {
      ...takeoverAgent,
      builtin_models: [takeoverChain.model_id, secondModel],
      model_supply: [takeoverChain.model_id, secondModel].map((model_id) => ({
        model_id,
        chain_length: 2,
        has_runnable_hop: true,
      })),
      routes: {
        ...takeoverAgent.routes,
        [secondModel]: { hops: takeoverAgent.routes![takeoverChain.model_id].hops },
      },
    };
    const secondChain: AgentChain = { ...takeoverChain, model_id: secondModel };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([agent]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    const overviewRead = vi.spyOn(modelsApi, 'getAgentChains').mockResolvedValue([
      takeoverChain,
      secondChain,
    ]);
    const exactRead = vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(takeoverChain);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    await waitFor(() => expect(overviewRead).toHaveBeenCalledOnce());
    expect(overviewRead).toHaveBeenCalledWith('codex');
    expect(exactRead).not.toHaveBeenCalled();

    await userEvent.click(await screen.findByRole('button', {
      name: /Open gpt-5\.6-sol route chain|打开 gpt-5\.6-sol 的路由链/i,
    }));
    await waitFor(() => expect(exactRead).toHaveBeenCalledOnce());
    expect(exactRead).toHaveBeenCalledWith('codex', 'gpt-5.6-sol');
    expect(screen.getByText(/Later Source order changes reorder its hops to match\.|以后调整供应商优先级时，其中的供应商会按新顺序重排。/i)).toBeTruthy();
  });

  it('keeps a failed event read distinct from an empty history and retries it', async () => {
    const hubAgent: AgentSupply = {
      ...directAgent('claude'),
      mode: 'hub',
      sources: { order: [], eligibility: [] },
      routes: {},
      model_supply: [],
      builtin_models: [],
      named_agents: [],
    };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([retainedSource]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([hubAgent]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    const events = vi.spyOn(modelsApi, 'listEvents')
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce([]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    await userEvent.click(await screen.findByRole('tab', { name: /^Logs$|^日志$/i }));
    expect((await screen.findAllByText(/Couldn't refresh, please retry|刷新失败，请重试/i)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/^No switches yet$|^暂无切换记录$/i)).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /^Retry$|^重试$/i }));
    await waitFor(() => expect(events).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/^No switches yet$|^暂无切换记录$/i)).toBeTruthy();
  });

  it('keeps an unread runtime status distinct from an authoritative stopped state', async () => {
    const hubAgent: AgentSupply = {
      ...directAgent('claude'),
      mode: 'hub',
      sources: { order: [], eligibility: [] },
      routes: {},
      model_supply: [],
      builtin_models: [],
      named_agents: [],
    };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([retainedSource]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([hubAgent]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockRejectedValue(new TypeError('status unread'));
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByText(/^Gateway status unavailable$|^模型网关状态不可用$/i)).toBeTruthy();
    expect(screen.queryByText(/^Gateway stopped|^网关已停止/i)).toBeNull();
    expect(screen.queryByRole('button', { name: /Gateway stopped|网关已停止/i })).toBeNull();
  });

  it('lands the overview without waiting for its backend chain collection', async () => {
    const hubAgent: AgentSupply = {
      ...takeoverAgent,
      backend: 'claude',
      selected_model_id: 'claude-opus-4-6',
      sources: { order: [retainedSource.id], eligibility: [{ source_id: retainedSource.id, eligible: true }] },
      routes: { 'claude-opus-4-6': { hops: [{ source_id: retainedSource.id, model_id: 'claude-opus-4-6' }] } },
      model_supply: [{ model_id: 'claude-opus-4-6', chain_length: 1, has_runnable_hop: true }],
      builtin_models: ['claude-opus-4-6'],
    };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([retainedSource]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([hubAgent]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'getAgentChains').mockImplementation(() => new Promise(() => {}));

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByText('Retained source')).toBeTruthy();
    expect(screen.getByText(/^Claude Code$/i)).toBeTruthy();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  it.each(MANAGE_COMMIT_ACTIONS)(
    '[MH-SRC-DELETE-001] keeps the page-owned $action impact readable until every referenced projection lands',
    async (action) => {
      const modelId = 'claude-opus-4-6';
      const updatedSource = { ...retainedSource, display_name: 'Updated source' };
      const hubAgent: AgentSupply = {
        ...takeoverAgent,
        backend: 'claude',
        selected_model_id: modelId,
        sources: {
          order: [retainedSource.id],
          eligibility: [{ source_id: retainedSource.id, eligible: true }],
        },
        routes: { [modelId]: { hops: [{ source_id: retainedSource.id, model_id: modelId }] } },
        model_supply: [{ model_id: modelId, chain_length: 1, has_runnable_hop: true }],
        builtin_models: [modelId],
      };
      const affectedChain: AgentChain = {
        contract_version: 7,
        backend: 'claude',
        model_id: modelId,
        current: { source_id: retainedSource.id, model_id: modelId },
        chain: [{
          source_id: retainedSource.id,
          model_id: modelId,
          channel: 'hub',
          health: 'healthy',
          runnable: true,
          reason: null,
          retry_at: null,
        }],
        supply_state: 'ok',
      };
      const impact = {
        removed_hops: [{
          backend: 'claude' as const,
          menu_model: modelId,
          position: 1,
          source_id: retainedSource.id,
          model_id: modelId,
        }],
        interrupted: [{ backend: 'claude' as const, model_id: modelId, agents: ['Release bot'] }],
      };
      const sourceRead = vi.spyOn(modelsApi, 'listSources').mockResolvedValue([retainedSource]);
      vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([hubAgent]);
      vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
      vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
      const overviewRead = vi.spyOn(modelsApi, 'getAgentChains').mockResolvedValue([affectedChain]);
      const chainRead = vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(affectedChain);
      if (action === 'edit') {
        vi.spyOn(modelsApi, 'patchSource').mockResolvedValueOnce({ source: updatedSource, ...impact });
      } else {
        vi.spyOn(modelsApi, 'deleteSource').mockResolvedValueOnce(impact);
      }

      render(
        <ToastProvider>
          <I18nextProvider i18n={i18n}>
            <SettingsModelsPage />
          </I18nextProvider>
        </ToastProvider>,
      );
      await userEvent.click((await screen.findByText('Retained source')).closest('button') as HTMLButtonElement);
      const sourceDialog = await screen.findByRole('dialog', { name: 'Retained source' });
      expect(within(sourceDialog).getByRole('textbox', { name: /Search model IDs|搜索模型 ID/i })).toBeTruthy();
      await waitFor(() => expect(overviewRead).toHaveBeenCalledOnce());

      await userEvent.click(screen.getByRole('button', { name: /Manage Retained source|管理 Retained source/i }));
      if (action === 'edit') {
        await userEvent.click(screen.getByRole('menuitem', { name: /^Edit source$|^编辑供应商$/i }));
        const name = screen.getByLabelText(/^Display name$|^显示名称$/i);
        await userEvent.clear(name);
        await userEvent.type(name, updatedSource.display_name);
        await userEvent.click(screen.getByRole('button', { name: /^Save$|^保存$/i }));
      } else {
        await userEvent.click(screen.getByRole('menuitem', { name: /^Remove source$|^移除供应商$/i }));
        await userEvent.click(screen.getByRole('button', { name: /^Remove source$|^移除供应商$/i }));
      }

      const report = await screen.findByRole('dialog', {
        name: action === 'edit' ? /source was updated|供应商已更新/i : /source was removed|供应商已移除/i,
      });
      expect(report.dataset.reportProjections?.split(' ')).toEqual(
        Object.keys(SOURCE_MUTATION_REPORT_PROJECTIONS),
      );
      expect(report.textContent).toContain(modelId);
      expect(report.textContent).toContain('Release bot');

      const chainLanding = deferred<AgentChain>();
      chainRead.mockImplementationOnce(() => chainLanding.promise);
      sourceRead.mockResolvedValue(action === 'edit' ? [updatedSource] : []);
      const done = within(report).getAllByRole('button', { name: /^Done$|^完成$/i })
        .find((button) => button.classList.contains('model-hub-guard-action'));
      await userEvent.click(done!);

      if (action === 'edit') {
        await waitFor(() => expect(document.querySelector('.model-hub-source-title')?.textContent)
          .toBe(updatedSource.display_name));
      } else {
        await waitFor(() => expect(document.querySelector('.model-hub-source-title')).toBeNull());
      }
      expect(screen.getByRole('dialog', {
        name: action === 'edit' ? /source was updated|供应商已更新/i : /source was removed|供应商已移除/i,
      })).toBeTruthy();
      expect(chainRead).toHaveBeenLastCalledWith('claude', modelId);

      await act(async () => {
        chainLanding.resolve(affectedChain);
        await chainLanding.promise;
      });
      await waitFor(() => expect(screen.queryByRole('dialog', {
        name: action === 'edit' ? /source was updated|供应商已更新/i : /source was removed|供应商已移除/i,
      })).toBeNull());
    },
  );

  it('cannot restore chains after the authoritative supply leaves hub mode', async () => {
    const head = { ...retainedSource, id: 'src_head', display_name: 'Paused source', state: { status: 'cooldown' as const, retry_at: '2099-01-01T00:00:00Z', detail_key: null } };
    const relay = { ...retainedSource, id: 'src_relay', display_name: 'Replacement source', state: { status: 'active' as const, retry_at: null, detail_key: null } };
    const direct = { ...takeoverAgent, mode: 'direct' as const, sources: null, routes: null, supply_status: null, model_supply: null };
    const pendingChains = deferred<AgentChain[]>();
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([head, relay]);
    const agentRead = vi.spyOn(modelsApi, 'listAgents')
      .mockResolvedValueOnce([takeoverAgent])
      .mockResolvedValueOnce([direct]);
    vi.spyOn(modelsApi, 'setAgentMode').mockResolvedValueOnce(direct);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    const chainRead = vi.spyOn(modelsApi, 'getAgentChains').mockImplementation(() => pendingChains.promise);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    await waitFor(() => expect(chainRead).toHaveBeenCalledOnce());
    await switchFirstGatewayAgentToDirect();
    await waitFor(() => expect(agentRead).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('button', { name: /^Switch to Gateway$|^切换到模型网关$/i })).toBeTruthy();

    await act(async () => {
      pendingChains.resolve([takeoverChain]);
      await pendingChains.promise;
    });

    expect(screen.queryByRole('button', { name: /route chain|路由链/i })).toBeNull();
    expect(screen.queryByText(/^Taken over$|^已自动切换$/i)).toBeNull();
    expect(screen.queryByTitle(takeoverMappingTitle)).toBeNull();
  });

  it('cannot let a pre-save chain read overwrite the committed route echo', async () => {
    const head = { ...retainedSource, id: 'src_head', display_name: 'Paused source', state: { status: 'active' as const, retry_at: null, detail_key: null } };
    const relay = { ...retainedSource, id: 'src_relay', display_name: 'Replacement source', state: { status: 'active' as const, retry_at: null, detail_key: null } };
    const pendingOldChain = deferred<AgentChain>();
    const pendingReconciliation = deferred<AgentSupply[]>();
    const committedChain: AgentChain = {
      ...takeoverChain,
      current: { source_id: 'src_head', model_id: 'gpt-5.6-sol' },
      chain: [{ ...takeoverChain.chain[0], health: 'healthy', runnable: true, retry_at: null }],
    };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([head, relay]);
    vi.spyOn(modelsApi, 'listAgents')
      .mockResolvedValueOnce([takeoverAgent])
      .mockReturnValueOnce(pendingReconciliation.promise);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'getAgentChains').mockImplementation(() => pendingOldChain.promise.then((chain) => [chain]));
    vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(takeoverChain);
    vi.spyOn(modelsApi, 'putAgentChain').mockResolvedValue({
      chain: committedChain,
      removed_hops: [],
      interrupted: [],
    });

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    await userEvent.click(await screen.findByRole('button', { name: /Open gpt-5\.6-sol route chain|打开 gpt-5\.6-sol 的路由链/i }));
    const removeButtons = await screen.findAllByRole('button', { name: /^Remove hop$|^移除这个路由项$/i });
    await userEvent.click(removeButtons[1]);
    await userEvent.click(screen.getByRole('button', { name: /^Save$|^保存$/i }));
    await userEvent.click((await screen.findByText(/^Done$|^完成$/i)).closest('button') as HTMLButtonElement);
    expect(await screen.findByTitle(headMappingTitle)).toBeTruthy();

    await act(async () => {
      pendingOldChain.resolve(takeoverChain);
      await pendingOldChain.promise;
    });

    expect(screen.getByTitle(headMappingTitle)).toBeTruthy();
    expect(screen.queryByTitle(takeoverMappingTitle)).toBeNull();
  });

  it('installs a removed backend before the committed route closes and applies PF-1', async () => {
    const head = { ...retainedSource, id: 'src_head', display_name: 'Paused source', state: { status: 'active' as const, retry_at: null, detail_key: null } };
    const relay = { ...retainedSource, id: 'src_relay', display_name: 'Replacement source', state: { status: 'active' as const, retry_at: null, detail_key: null } };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([head, relay]);
    vi.spyOn(modelsApi, 'listAgents')
      .mockResolvedValueOnce([takeoverAgent])
      .mockResolvedValueOnce([]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'getAgentChains').mockResolvedValue([takeoverChain]);
    vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(takeoverChain);
    vi.spyOn(modelsApi, 'putAgentChain').mockResolvedValue({
      chain: { ...takeoverChain, chain: [takeoverChain.chain[0]], current: takeoverChain.chain[0] },
      removed_hops: [],
      interrupted: [],
    });

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    await userEvent.click(await screen.findByRole('button', { name: /Open gpt-5\.6-sol route chain|打开 gpt-5\.6-sol 的路由链/i }));
    await userEvent.click((await screen.findAllByRole('button', { name: /^Remove hop$|^移除这个路由项$/i }))[1]);
    await userEvent.click(screen.getByRole('button', { name: /^Save$|^保存$/i }));

    await waitFor(() =>
      expect(document.querySelector('[data-agent-backend="codex"]')).toBeNull(),
    );
    await userEvent.click((await screen.findByText(/^Done$|^完成$/i)).closest('button') as HTMLButtonElement);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    const firstRegisteredDestination = document.querySelector(
      '.model-hub-shell-info',
    );
    await waitFor(() =>
      expect(document.activeElement).toBe(firstRegisteredDestination),
    );
    expect(document.activeElement?.isConnected).toBe(true);
    expect(document.activeElement?.closest('[data-agent-backend="codex"]')).toBeNull();
  });

  it('keeps the source projection intact after a lost Direct-mode response', async () => {
    const direct = { ...takeoverAgent, mode: 'direct' as const, sources: null, routes: null, supply_status: null, model_supply: null };
    const staleSource = {
      ...retainedSource,
      id: 'src_head',
      display_name: 'Paused source',
      adopted_by: [{ backend: 'codex' as const, menu_model: 'gpt-5.6-sol' }],
    };
    const agentRead = vi.spyOn(modelsApi, 'listAgents')
      .mockResolvedValueOnce([takeoverAgent])
      .mockResolvedValueOnce([direct]);
    const sourceRead = vi.spyOn(modelsApi, 'listSources').mockResolvedValue([staleSource]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'getAgentChains').mockResolvedValue([takeoverChain]);
    const setMode = vi.spyOn(modelsApi, 'setAgentMode').mockRejectedValueOnce(new TypeError('response lost'));

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    await switchFirstGatewayAgentToDirect();

    expect(await screen.findByRole('button', { name: /^Switch to Gateway$|^切换到模型网关$/i })).toBeTruthy();
    expect(screen.queryByText(/did not go through|没切换成功/i)).toBeNull();
    expect(setMode).toHaveBeenCalledOnce();
    expect(agentRead).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(sourceRead).toHaveBeenCalledOnce());
    expect(screen.queryByText(/Could not read the source list · the gateway itself is fine|供应商列表暂时不可用 · 模型网关本身正常/i)).toBeNull();
    expect(screen.queryByText(/Supplying Codex|正在使用 Codex/i)).toBeNull();
    expect(screen.getByText(/Available · not currently supplying|可用 · 当前未使用/i)).toBeTruthy();
  });

  it('does not compete for source inventory during Direct-mode recovery', () => {
    const page = readFileSync(join(process.cwd(), 'src/components/settings/models/SettingsModelsPage.tsx'), 'utf8');
    const recovery = page.slice(page.indexOf('const switchToDirect'), page.indexOf('const loadOlderEvents'));
    expect(recovery).not.toMatch(/sourceCollectionReads\.read\(/);
    expect(recovery).not.toMatch(/void refresh\(\)/);
  });

  it('keeps retained supply rows but clears derived chain claims when a later supply read fails', async () => {
    const head = { ...retainedSource, id: 'src_head', display_name: 'Paused source', state: { status: 'cooldown' as const, retry_at: '2099-01-01T00:00:00Z', detail_key: null } };
    const relay = { ...retainedSource, id: 'src_relay', display_name: 'Replacement source', state: { status: 'active' as const, retry_at: null, detail_key: null } };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([head, relay]);
    const agentRead = vi.spyOn(modelsApi, 'listAgents')
      .mockResolvedValueOnce([takeoverAgent])
      .mockRejectedValueOnce(new TypeError('supply unread'));
    vi.spyOn(modelsApi, 'refreshSource').mockResolvedValueOnce({ source: head, discovered: head.models.length });
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'getAgentChains').mockResolvedValue([takeoverChain]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByTitle(takeoverMappingTitle)).toBeTruthy();
    await userEvent.click(screen.getByText('Paused source').closest('button') as HTMLButtonElement);
    await userEvent.click(await screen.findByRole('button', { name: /^Refetch$|^重新拉取$/i }));

    await waitFor(() => expect(agentRead).toHaveBeenCalledTimes(2));
    await closeSourceDetails();
    expect(await screen.findByText(/Could not read this backend's supply|没有读到后端列表/i)).toBeTruthy();
    expect(screen.queryByTitle(takeoverMappingTitle)).toBeNull();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  it('keeps retained regions but clears live chain claims when a later runtime read fails', async () => {
    const head = { ...retainedSource, id: 'src_head', display_name: 'Paused source', state: { status: 'cooldown' as const, retry_at: '2099-01-01T00:00:00Z', detail_key: null } };
    const relay = { ...retainedSource, id: 'src_relay', display_name: 'Replacement source', state: { status: 'active' as const, retry_at: null, detail_key: null } };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([head, relay]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([takeoverAgent]);
    const runtimeRead = vi.spyOn(modelsApi, 'getRuntimeStatus')
      .mockResolvedValueOnce(runtime)
      .mockRejectedValueOnce(new TypeError('runtime unread'));
    vi.spyOn(modelsApi, 'refreshSource').mockResolvedValueOnce({ source: head, discovered: head.models.length });
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'getAgentChains').mockResolvedValue([takeoverChain]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByTitle(takeoverMappingTitle)).toBeTruthy();
    await userEvent.click(screen.getByText('Paused source').closest('button') as HTMLButtonElement);
    await userEvent.click(await screen.findByRole('button', { name: /^Refetch$|^重新拉取$/i }));

    await waitFor(() => expect(runtimeRead).toHaveBeenCalledTimes(2));
    await closeSourceDetails();
    expect(await screen.findByText(/^Gateway status unavailable$|^模型网关状态不可用$/i)).toBeTruthy();
    expect(screen.queryByTitle(takeoverMappingTitle)).toBeNull();
    expect(screen.queryByText(/^Taken over$|^已自动切换$/i)).toBeNull();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });
});

// How the usage report is READ. What it says once read is `UsageTab.test.tsx`'s
// subject; the property here is that the read is the tab's own — off the first
// paint, live on every open, and spanning whatever the control was left on.
describe('SettingsModelsPage usage region', () => {
  const openUsage = () => userEvent.click(screen.getByRole('tab', { name: /^Usage$|^用量$/ }));

  // The ledger outlives the Sources it metered — 62 days of retention, and a
  // vanished Source still named by its id — so the route to it cannot be a branch
  // of currently having one. Deleting the last source may not delete the only way
  // to read what it cost, and a user reading the report may not be thrown off it
  // by their own deletion.
  it('MH-USAGE-022: reaches the report from every landing the Hub can draw', async () => {
    // Keyed by the landing itself: a `Record<ModelsSurfaceKind, …>` cannot omit
    // one, so a landing added later fails to compile here rather than quietly
    // shipping without a route. The loop stays inside one case because a row's
    // evidence has to be a case the catalog can resolve by name.
    const landings: Record<ModelsSurfaceKind, Source[]> = {
      direct_empty: [],
      gateway: [retainedSource],
    };

    for (const [landing, sources] of Object.entries(landings)) {
      cleanup();
      vi.restoreAllMocks();
      renderPage(sources);

      await waitFor(() => expect(screen.getAllByRole('tab'), landing).toHaveLength(3));
      await openUsage();
      await waitFor(() => expect(vi.mocked(modelsApi.getUsageSummary), landing).toHaveBeenCalledWith(30));
    }
  });

  it('MH-USAGE-020: leaves the report unread until its tab is opened, then reads the default span', async () => {
    renderPage([retainedSource]);
    await screen.findByText('Retained source');
    const read = vi.mocked(modelsApi.getUsageSummary);

    // The landing is what decides routing. A report nobody is looking at may
    // not be part of the read that draws it.
    expect(read).not.toHaveBeenCalled();
    await openUsage();
    await waitFor(() => expect(read).toHaveBeenCalledWith(30));
  });

  it('re-reads with the span the control was moved to', async () => {
    renderPage([retainedSource]);
    await screen.findByText('Retained source');
    await openUsage();
    const read = vi.mocked(modelsApi.getUsageSummary);
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1));

    await userEvent.click(screen.getByRole('radio', { name: /^7d$|^7 天$/ }));
    await waitFor(() => expect(read).toHaveBeenLastCalledWith(7));
  });

  it('re-reads on every open, because the figure is live', async () => {
    renderPage([retainedSource]);
    await screen.findByText('Retained source');
    const read = vi.mocked(modelsApi.getUsageSummary);
    await openUsage();
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1));

    await userEvent.click(screen.getByRole('tab', { name: /^Sources & gateway$|^供应商与路由$/ }));
    await openUsage();
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  });

  it('retries the read the tab failed on, at the same span', async () => {
    renderPage([retainedSource]);
    const read = vi.mocked(modelsApi.getUsageSummary);
    read.mockRejectedValueOnce(new TypeError('usage unread'));
    await screen.findByText('Retained source');
    await openUsage();

    await userEvent.click(await screen.findByRole('button', { name: /^Retry$|^重试$/ }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(read.mock.calls.map(([days]) => days)).toEqual([30, 30]);
  });
});
