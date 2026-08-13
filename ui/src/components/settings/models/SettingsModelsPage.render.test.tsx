// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/context/ToastProvider';
import i18n from '@/i18n';
import { modelsApi } from './modelsApi';
import { SettingsModelsPage } from './SettingsModelsPage';
import type { AgentBackend, AgentChain, AgentSupply, RuntimeDependency, Source } from './types';

const directAgent = (backend: AgentBackend): AgentSupply => ({
  backend,
  cli_present: true,
  mode: 'direct',
  menu_kind: backend === 'opencode' ? 'open' : 'fixed',
});

const runtime: RuntimeDependency = {
  contract_version: 5,
  manifest: { name: 'cliproxyapi', version: '1', source_sha: 'a'.repeat(40), assets: [] },
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
  contract_version: 5,
  backend: 'codex',
  model_id: 'gpt-5.6-sol',
  current: { source_id: 'src_relay', model_id: 'gpt-5.6-sol' },
  chain: [
    { source_id: 'src_head', model_id: 'gpt-5.6-sol', channel: 'hub', health: 'cooldown', runnable: false, reason: null, retry_at: '2099-01-01T00:00:00Z' },
    { source_id: 'src_relay', model_id: 'gpt-5.6-sol', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null },
  ],
  supply_state: 'ok',
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
  return render(
    <ToastProvider>
      <I18nextProvider i18n={i18n}>
        <SettingsModelsPage />
      </I18nextProvider>
    </ToastProvider>,
  );
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('SettingsModelsPage surface branches', () => {
  it('renders Frame 09 without tabs when every backend is direct and no source exists', async () => {
    renderPage([]);

    expect(await screen.findByText(/^Currently: direct$|^当前:直连$/i)).toBeTruthy();
    expect(screen.getAllByRole('button', { name: /^Switch to Gateway$|^切换到网关$/i })).toHaveLength(3);
    expect(screen.getByText(/^Switch to the gateway and you gain three things$|^切换到网关，你会多出三件事$/i)).toBeTruthy();
    expect(screen.queryByRole('tab')).toBeNull();
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
    expect(screen.queryByRole('button', { name: /^Switch to Gateway$|^切换到网关$/i })).toBeNull();
    expect(screen.queryByText(/backends are direct|个后端均为直连/i)).toBeNull();
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

    expect(await screen.findByText(/^Gateway status unavailable$|^网关状态未读到$/i)).toBeTruthy();
    expect(screen.queryByText(/^All 3 backends are direct$|^3 个后端均为直连$/i)).toBeNull();
  });

  it('keeps the stopped runtime action visible on the direct-only surface', async () => {
    const stopped = { ...runtime, status: { ...runtime.status, health: 'down' as const } };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([
      directAgent('claude'),
      directAgent('codex'),
      directAgent('opencode'),
    ]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(stopped);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByRole('button', { name: /Gateway stopped|网关已停止/i })).toBeTruthy();
    expect(screen.queryByText(/^All 3 backends are direct$|^3 个后端均为直连$/i)).toBeNull();
  });

  it('renders Frame 01 with tabs when retained sources remain under all-direct backends', async () => {
    renderPage([retainedSource]);

    expect(await screen.findByText('Retained source')).toBeTruthy();
    expect(screen.getAllByRole('tab')).toHaveLength(2);
    expect(screen.queryByText(/^Switch to the gateway and you gain three things$|^切换到网关，你会多出三件事$/i)).toBeNull();
  });

  it('lands the operational overview without waiting for event history', async () => {
    const pendingEvents = deferred<[]>();
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([
      directAgent('claude'),
      directAgent('codex'),
      directAgent('opencode'),
    ]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockReturnValue(pendingEvents.promise);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByText(/^Currently: direct$|^当前:直连$/i)).toBeTruthy();
    expect(screen.getAllByRole('button', { name: /^Switch to Gateway$|^切换到网关$/i })).toHaveLength(3);
    await act(async () => {
      pendingEvents.resolve([]);
      await pendingEvents.promise;
    });
  });

  it('counts a recoverable reroute once per backend in Frame 08', async () => {
    const head = { ...retainedSource, id: 'src_head', display_name: 'Paused source', state: { status: 'cooldown' as const, retry_at: '2099-01-01T00:00:00Z', detail_key: null } };
    const relay = { ...retainedSource, id: 'src_relay', display_name: 'Replacement source', state: { status: 'active' as const, retry_at: null, detail_key: null } };
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([head, relay]);
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([takeoverAgent]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(takeoverChain);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByText(/^1 takeover active$|^1 处接管中$/i)).toBeTruthy();
    expect(screen.getByText(/^Taken over$|^接管中$/i)).toBeTruthy();
    expect(screen.getByText(/Now: Replacement source \(takeover\)|当前 Replacement source（接管）/i)).toBeTruthy();
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

    expect(await screen.findByText(/^Gateway status unavailable$|^网关状态未读到$/i)).toBeTruthy();
    expect(screen.queryByText(/^Gateway stopped|^网关已停止/i)).toBeNull();
    expect(screen.queryByRole('button', { name: /Gateway stopped|网关已停止/i })).toBeNull();
  });

  it('lands the overview without waiting for a per-model chain read', async () => {
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
    vi.spyOn(modelsApi, 'getAgentChain').mockImplementation(() => new Promise(() => {}));

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

  it('cannot restore chains after the authoritative supply leaves hub mode', async () => {
    const head = { ...retainedSource, id: 'src_head', display_name: 'Paused source', state: { status: 'cooldown' as const, retry_at: '2099-01-01T00:00:00Z', detail_key: null } };
    const relay = { ...retainedSource, id: 'src_relay', display_name: 'Replacement source', state: { status: 'active' as const, retry_at: null, detail_key: null } };
    const direct = { ...takeoverAgent, mode: 'direct' as const, sources: null, routes: null, supply_status: null, model_supply: null };
    const pendingChain = deferred<AgentChain>();
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([head, relay]);
    const agentRead = vi.spyOn(modelsApi, 'listAgents')
      .mockResolvedValueOnce([takeoverAgent])
      .mockResolvedValueOnce([direct]);
    vi.spyOn(modelsApi, 'setAgentMode').mockResolvedValueOnce(direct);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    const chainRead = vi.spyOn(modelsApi, 'getAgentChain').mockImplementation(() => pendingChain.promise);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    await waitFor(() => expect(chainRead).toHaveBeenCalledOnce());
    await userEvent.click(screen.getByRole('button', { name: /^Switch to direct$|^切换到直连$/i }));
    await waitFor(() => expect(agentRead).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('button', { name: /^Switch to Gateway$|^切换到网关$/i })).toBeTruthy();

    await act(async () => {
      pendingChain.resolve(takeoverChain);
      await pendingChain.promise;
    });

    expect(screen.queryByRole('button', { name: /route chain|路由链/i })).toBeNull();
    expect(screen.queryByText(/^Taken over$|^接管中$/i)).toBeNull();
    expect(screen.queryByText(/Now: Replacement source \(takeover\)|当前 Replacement source（接管）/i)).toBeNull();
  });

  it('reconciles a lost Direct-mode response before rendering failure', async () => {
    const direct = { ...takeoverAgent, mode: 'direct' as const, sources: null, routes: null, supply_status: null, model_supply: null };
    const agentRead = vi.spyOn(modelsApi, 'listAgents')
      .mockResolvedValueOnce([takeoverAgent])
      .mockResolvedValueOnce([direct]);
    vi.spyOn(modelsApi, 'listSources').mockResolvedValue([retainedSource]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime);
    vi.spyOn(modelsApi, 'listEvents').mockResolvedValue([]);
    vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(takeoverChain);
    const setMode = vi.spyOn(modelsApi, 'setAgentMode').mockRejectedValueOnce(new TypeError('response lost'));

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    await userEvent.click(await screen.findByRole('button', { name: /^Switch to direct$|^切换到直连$/i }));

    expect(await screen.findByRole('button', { name: /^Switch to Gateway$|^切换到网关$/i })).toBeTruthy();
    expect(screen.queryByText(/did not go through|没切换成功/i)).toBeNull();
    expect(setMode).toHaveBeenCalledOnce();
    expect(agentRead).toHaveBeenCalledTimes(2);
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
    vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(takeoverChain);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByText(/Now: Replacement source \(takeover\)|当前 Replacement source（接管）/i)).toBeTruthy();
    await userEvent.click(screen.getByText('Paused source').closest('button') as HTMLButtonElement);
    await userEvent.click(await screen.findByRole('button', { name: /^Refetch$|^重新拉取$/i }));

    await waitFor(() => expect(agentRead).toHaveBeenCalledTimes(2));
    await userEvent.click(screen.getByRole('button', { name: /^Back to sources$|^返回来源$/i }));
    expect(await screen.findByText(/Could not read this backend's supply|没有读到后端列表/i)).toBeTruthy();
    expect(screen.queryByText(/Now: Replacement source \(takeover\)|当前 Replacement source（接管）/i)).toBeNull();
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
    vi.spyOn(modelsApi, 'getAgentChain').mockResolvedValue(takeoverChain);

    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    expect(await screen.findByText(/Now: Replacement source \(takeover\)|当前 Replacement source（接管）/i)).toBeTruthy();
    await userEvent.click(screen.getByText('Paused source').closest('button') as HTMLButtonElement);
    await userEvent.click(await screen.findByRole('button', { name: /^Refetch$|^重新拉取$/i }));

    await waitFor(() => expect(runtimeRead).toHaveBeenCalledTimes(2));
    await userEvent.click(screen.getByRole('button', { name: /^Back to sources$|^返回来源$/i }));
    expect(await screen.findByText(/^Gateway status unavailable$|^网关状态未读到$/i)).toBeTruthy();
    expect(screen.queryByText(/Now: Replacement source \(takeover\)|当前 Replacement source（接管）/i)).toBeNull();
    expect(screen.queryByText(/^Taken over$|^接管中$/i)).toBeNull();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });
});
