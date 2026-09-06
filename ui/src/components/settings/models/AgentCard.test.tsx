// @vitest-environment jsdom
import type { ComponentProps } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { AgentCard as RuntimeAgentCard } from './AgentCard';
import { COLLAPSED_MODEL_LIMIT, modelChainKey } from './modelRows';
import { readyRegion } from './regionRead';
import { freshRuntimeProjection } from './runtimeLifecycle';
import type { AgentSupply, RuntimeDependency, Source } from './types';

const runtime: RuntimeDependency = {
  contract_version: 9,
  manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1.0.0', source_sha: 'fixture', assets: [] },
  status: { installed_version: '1.0.0', verified: true, listening: null, health: 'ok', last_check: null },
};
type AgentCardProps = Omit<ComponentProps<typeof RuntimeAgentCard>, 'runtime' | 'onOpenModels'>
  & Partial<Pick<ComponentProps<typeof RuntimeAgentCard>, 'onOpenModels'>>;
const AgentCard = ({ onOpenModels = vi.fn(), ...props }: AgentCardProps) =>
  <RuntimeAgentCard {...props} onOpenModels={onOpenModels} runtime={freshRuntimeProjection(readyRegion(runtime))} />;

const localeInstance = (lng: 'en' | 'zh') => {
  const instance = createInstance();
  void instance.use(initReactI18next).init({
    lng,
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  return instance;
};

const source = (id: string, name: string): Source => ({
  id, last_discovered_at: null, kind: 'api_key', vendor: 'anthropic', display_name: name,
  protocol: 'anthropic', supply_channel: 'hub', billing: 'metered', state: { status: 'active', retry_at: null, detail_key: null },
  models: [{ id: 'claude-opus-4-6', origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null }],
});
const hubAgent: AgentSupply = {
  backend: 'claude', cli_present: true, mode: 'hub', menu_kind: 'fixed', selected_model_id: 'claude-opus-4-6', selected_model_explicit: true,
  sources: { order: ['src_a', 'src_b'], eligibility: [{ source_id: 'src_a', eligible: true }, { source_id: 'src_b', eligible: true }] },
  routes: { 'claude-opus-4-6': { hops: [{ source_id: 'src_a', model_id: 'claude-opus-4-6' }, { source_id: 'src_b', model_id: 'claude-opus-4-6' }] } },
  supply_status: 'degraded', model_supply: [{ route_origin: "manual" as const, model_id: 'claude-opus-4-6', chain_length: 2, has_runnable_hop: true }], named_agents: [{ name: 'claude', effective_model_id: 'claude-opus-4-6', supply_status: 'degraded' }], builtin_models: ['claude-opus-4-6'], menu: null,
};
const openCodeAgent: AgentSupply = {
  backend: 'opencode',
  cli_present: true,
  mode: 'hub',
  menu_kind: 'open',
  selected_model_id: null,
  selected_model_explicit: false,
  sources: { order: ['src_a'], eligibility: [{ source_id: 'src_a', eligible: true }] },
  routes: {},
  supply_status: null,
  model_supply: [],
  named_agents: [{
    name: 'opencode',
    effective_model_id: 'gpt-5.6-terra',
    supply_status: 'interrupted',
    route_reason: 'route_unconfigured',
  }],
  builtin_models: null,
  menu: { view: 'featured', checked: [] },
};

afterEach(cleanup);

describe('AgentCard origin help ownership', () => {
  const agents: AgentSupply[] = ['claude', 'codex'].map((backend) => ({
    ...hubAgent,
    backend: backend as AgentSupply['backend'],
    catalog_models: ['shared-model', 'second-model'].map((id) => ({
      id, display_name: null, origin: 'manual', models_dev_id: null, context_window: null, max_output_tokens: null,
      input_modalities: ['text'], output_modalities: ['text'], supports_tools: true, supports_reasoning: false,
      reasoning_efforts: [], locked: false, routeable: true,
    })),
    model_supply: ['shared-model', 'second-model'].map((model_id) => ({
      model_id, route_origin: 'automatic', chain_length: 1, has_runnable_hop: true,
    })),
  }));
  const setup = () => {
    const onOpenRoute = vi.fn();
    const collection = (nextAgents = agents) => <I18nextProvider i18n={i18n}><AgentCard
      agents={nextAgents} sources={[]} chains={{}} pendingBackends={new Set()}
      switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()}
      onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={onOpenRoute} onProbeSettled={vi.fn()}
    /></I18nextProvider>;
    const view = render(collection());
    const badge = (backend: string, model = 'shared-model') => within(
      document.querySelector<HTMLElement>(`[data-route-backend="${backend}"][data-route-model="${model}"]`)!,
    ).getByRole('button', { name: 'Automatic' });
    return { ...view, collection, badge, onOpenRoute };
  };
  const onlyHelpFor = (trigger: HTMLElement) => {
    const help = document.getElementById(trigger.getAttribute('aria-controls')!);
    expect(help).not.toBeNull();
    expect([...document.querySelectorAll('.model-hub-origin-help')]).toEqual([help]);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    return help!;
  };
  const mouse = (target: HTMLElement, type: 'pointerover' | 'pointerout') => {
    const event = new MouseEvent(type, { bubbles: true });
    Object.defineProperty(event, 'pointerType', { value: 'mouse' });
    fireEvent(target, event);
  };
  afterEach(() => { vi.useRealTimers(); });

  it.each(['focus', 'pin'] as const)('a new backend hover replaces the old %s even while its trigger stays focused', async (activation) => {
    const user = userEvent.setup();
    const { badge, onOpenRoute } = setup();
    const first = badge('claude');
    const second = badge('codex');
    if (activation === 'pin') await user.click(first);
    else act(() => first.focus());
    onlyHelpFor(first);
    await user.hover(second);
    expect(document.activeElement).toBe(first);
    await waitFor(() => onlyHelpFor(second));
    expect(first.getAttribute('aria-expanded')).toBe('false');
    // A late blur from the replaced trigger must not dismiss the new help.
    act(() => first.blur());
    onlyHelpFor(second);
    expect(onOpenRoute).not.toHaveBeenCalled();
  });

  it('focus and touch replace pinned help across rows, then unpin, Escape and outside dismiss normally', async () => {
    const user = userEvent.setup();
    const { badge, onOpenRoute } = setup();
    const first = badge('claude');
    const second = badge('claude', 'second-model');
    const third = badge('codex');
    await user.click(first);
    act(() => second.focus());
    await waitFor(() => onlyHelpFor(second));
    await user.pointer([{ keys: '[TouchA>]', target: third }, { keys: '[/TouchA]' }]);
    await waitFor(() => onlyHelpFor(third));
    await user.pointer([{ keys: '[TouchA>]', target: third }, { keys: '[/TouchA]' }]);
    await waitFor(() => expect(document.querySelector('.model-hub-origin-help')).toBeNull());
    act(() => first.focus());
    onlyHelpFor(first);
    await user.keyboard('{Escape}');
    expect(document.querySelector('.model-hub-origin-help')).toBeNull();
    await user.click(second);
    onlyHelpFor(second);
    await user.click(document.body);
    expect(document.querySelector('.model-hub-origin-help')).toBeNull();
    expect(onOpenRoute).not.toHaveBeenCalled();
  });

  it('old leave timers cannot close the newly active backend help', () => {
    vi.useFakeTimers();
    const { badge } = setup();
    const first = badge('claude');
    const second = badge('codex');
    mouse(first, 'pointerover');
    mouse(first, 'pointerout');
    mouse(second, 'pointerover');
    onlyHelpFor(second);
    // Even a queued leave event delivered after replacement is key-scoped.
    mouse(first, 'pointerout');
    act(() => vi.advanceTimersByTime(121));
    onlyHelpFor(second);
  });

  it('keeps the 120ms pointer bridge and content hover, then closes after leaving both', () => {
    vi.useFakeTimers();
    const { badge } = setup();
    const trigger = badge('claude');
    mouse(trigger, 'pointerover');
    const help = onlyHelpFor(trigger);
    mouse(trigger, 'pointerout');
    act(() => vi.advanceTimersByTime(119));
    onlyHelpFor(trigger);
    mouse(help, 'pointerover');
    act(() => vi.advanceTimersByTime(121));
    onlyHelpFor(trigger);
    mouse(help, 'pointerout');
    act(() => vi.advanceTimersByTime(120));
    expect(document.querySelector('.model-hub-origin-help')).toBeNull();
  });

  it('clears a removed active row without reviving its pin when it returns', async () => {
    const user = userEvent.setup();
    const { badge, rerender, collection } = setup();
    await user.click(badge('claude'));
    rerender(collection([{ ...agents[0], catalog_models: agents[0].catalog_models!.slice(1) }, agents[1]]));
    expect(document.querySelector('.model-hub-origin-help')).toBeNull();
    rerender(collection());
    expect(document.querySelector('.model-hub-origin-help')).toBeNull();
    await user.hover(badge('claude'));
    onlyHelpFor(badge('claude'));
    await user.hover(badge('codex'));
    await waitFor(() => onlyHelpFor(badge('codex')));
    rerender(collection([agents[1]]));
    onlyHelpFor(badge('codex'));
  });

  it('cancels pending help on collection unmount and keeps row opening independent', () => {
    vi.useFakeTimers();
    const { badge, unmount, onOpenRoute } = setup();
    const trigger = badge('claude');
    fireEvent.click(trigger);
    expect(onOpenRoute).not.toHaveBeenCalled();
    const row = trigger.closest<HTMLElement>('.model-hub-model-row')!;
    fireEvent.click(within(row).getByRole('button', { name: /route chain/ }));
    expect(onOpenRoute).toHaveBeenCalledWith(agents[0], 'shared-model', row.querySelector('.model-hub-model-open'));
    fireEvent.click(trigger);
    mouse(badge('codex'), 'pointerover');
    mouse(badge('codex'), 'pointerout');
    unmount();
    act(() => vi.advanceTimersByTime(121));
    expect(document.querySelector('.model-hub-origin-help')).toBeNull();
  });
});

describe('AgentCard', () => {
  it('renders an empty OpenCode selection as configurable rather than as missing backend supply', async () => {
    const onOpenModels = vi.fn();
    const retainedModelId = 'gpt-5.6-luna';
    const agentWithRetainedRoute = {
      ...openCodeAgent,
      routes: { [retainedModelId]: { hops: [{ source_id: 'src_a', model_id: 'gpt-5.6-luna' }] } },
      model_supply: [{ route_origin: "manual" as const, model_id: retainedModelId, chain_length: 1, has_runnable_hop: true }],
    };
    render(<I18nextProvider i18n={localeInstance('zh')}><AgentCard agents={[agentWithRetainedRoute]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenModels={onOpenModels} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);

    expect(screen.getByText('网关 · 未配置模型路由')).toBeTruthy();
    expect(screen.getByText('0 个模型')).toBeTruthy();
    // The card offers the catalog instead of reporting missing supply: an empty
    // list is something the user can fix from here, not a broken backend.
    expect(screen.getByText('这个后端的模型列表是空的')).toBeTruthy();
    expect(screen.queryByText(retainedModelId)).toBeNull();

    await userEvent.click(screen.getAllByRole('button', { name: '管理模型' })[0]);
    expect(onOpenModels).toHaveBeenCalledWith(agentWithRetainedRoute);
  });

  it('offers the same catalog action on every backend and lists the catalog in its own order', async () => {
    const onOpenModels = vi.fn();
    const catalogued = (backend: AgentSupply['backend'], ids: string[]): AgentSupply => ({
      ...hubAgent,
      backend,
      routes: {},
      model_supply: ids.map((modelId) => ({ route_origin: "manual" as const, model_id: modelId, chain_length: 1, has_runnable_hop: true })),
      catalog_models: ids.map((id) => ({
        id, display_name: null, origin: 'manual', models_dev_id: null, context_window: null, max_output_tokens: null,
        input_modalities: ['text'], output_modalities: ['text'], supports_tools: true, supports_reasoning: false,
        reasoning_efforts: [], locked: false, routeable: true,
        ...(backend === 'opencode' ? { native_protocol: 'openai_responses' as const } : {}),
      })),
    });
    const agents = [
      catalogued('claude', ['claude-two', 'claude-one']),
      catalogued('codex', ['codex-only']),
      catalogued('opencode', ['one']),
    ];
    render(<I18nextProvider i18n={i18n}><AgentCard agents={agents} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenModels={onOpenModels} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);

    // The OpenCode-only distinction is gone: the action is the backend-agnostic one.
    const actions = screen.getAllByRole('button', { name: 'Manage models' });
    expect(actions).toHaveLength(agents.length);
    await userEvent.click(actions[1]);
    expect(onOpenModels).toHaveBeenCalledWith(agents[1]);

    // Catalog order, not an alphabetized or legacy projection.
    const routeRows = screen.getAllByRole('button', { name: /route chain/i });
    expect(routeRows.map((row) => row.getAttribute('aria-label'))).toEqual([
      'Open claude-two route chain',
      'Open claude-one route chain',
      'Open codex-only route chain',
      'Open one route chain',
    ]);
  });

  it('labels a selected OpenCode model with an empty route as unconfigured', () => {
    const modelId = 'gpt-5.6-luna';
    render(<I18nextProvider i18n={localeInstance('zh')}><AgentCard agents={[{
      ...openCodeAgent,
      menu: { view: 'featured', checked: [modelId] },
      model_supply: [{ route_origin: null, model_id: modelId, chain_length: 0, has_runnable_hop: false }],
    }]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);

    expect(screen.getByText('1 个模型')).toBeTruthy();
    expect(screen.getByText(modelId)).toBeTruthy();
    expect(screen.getByText('未配置模型路由')).toBeTruthy();
    expect(screen.queryByText('没有可用供应商')).toBeNull();
  });

  it.each([
    ['en', 'Gateway · Supply unavailable for now'],
    ['zh', '网关 · 等待供应商恢复'],
  ] as const)('renders the waiting umbrella in %s', (lng, copy) => {
    render(<I18nextProvider i18n={localeInstance(lng)}><AgentCard agents={[{ ...hubAgent, named_agents: [{ name: 'claude', effective_model_id: 'claude-opus-4-6', supply_status: 'waiting' }] }]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);

    expect(screen.getByText(copy)).toBeTruthy();
  });

  it.each([
    ['en', 'Gateway · Healthy'],
    ['zh', '网关 · 正常'],
  ] as const)('summarizes the backend Agents instead of the unrelated default-Agent selection in %s', (lng, copy) => {
    render(<I18nextProvider i18n={localeInstance(lng)}><AgentCard agents={[{
      ...hubAgent,
      selected_model_id: null,
      selected_model_explicit: false,
      supply_status: null,
      named_agents: [{ name: 'claude', effective_model_id: 'claude-sonnet-5', supply_status: 'ok' }],
    }]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);

    expect(screen.getByText(copy)).toBeTruthy();
  });

  it.each([
    ['en', 'Gateway · No Agent uses this backend'],
    ['zh', '网关 · 暂无 Agent 使用'],
  ] as const)('states when no enabled Agent uses the backend in %s', (lng, copy) => {
    render(<I18nextProvider i18n={localeInstance(lng)}><AgentCard agents={[{ ...hubAgent, named_agents: [] }]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);

    expect(screen.getByText(copy)).toBeTruthy();
  });

  it.each([
    ['en', 'Backup → claude-opus-4-6 (Taken over)', 'Open claude-opus-4-6 route chain · Backup → claude-opus-4-6 (Taken over)'],
    ['zh', 'Backup → claude-opus-4-6（已自动切换）', '打开 claude-opus-4-6 的路由 · Backup → claude-opus-4-6（已自动切换）'],
  ] as const)('derives and announces takeover from the exact current hop in %s', (lng, mappingCopy, accessibleName) => {
    const key = modelChainKey('claude', 'claude-opus-4-6');
    render(<I18nextProvider i18n={localeInstance(lng)}><AgentCard agents={[hubAgent]} sources={[source('src_a', 'Primary'), source('src_b', 'Backup')]} chains={{ [key]: readyRegion({ manual_override: {hops:[{source_id:'src_a',model_id:'claude-opus-4-6'},{source_id:'src_b',model_id:'claude-opus-4-6'}]}, route_origin: "manual" as const, contract_version: 9, backend: 'claude', model_id: 'claude-opus-4-6', current: { source_id: 'src_b', model_id: 'claude-opus-4-6' }, chain: [{ source_id: 'src_a', model_id: 'claude-opus-4-6', channel: 'hub', health: 'cooldown', runnable: false, reason: null, retry_at: '2099-01-01T00:00:00Z' }, { source_id: 'src_b', model_id: 'claude-opus-4-6', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }], supply_state: 'ok' }) }} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);
    expect(screen.getByText(/Backup/)).toBeTruthy();
    expect(screen.getByTitle(mappingCopy)).toBeTruthy();
    expect(screen.getByRole('button', { name: accessibleName })).toBeTruthy();
  });

  it.each([
    ['en', 'Gateway', 'Backup source → routed-opus', 'Open claude-opus-4-6 route chain · Backup source → routed-opus'],
    ['zh', '网关', 'Backup source → routed-opus', '打开 claude-opus-4-6 的路由 · Backup source → routed-opus'],
  ] as const)('renders and announces the %s source-to-model mapping', (lng, modeCopy, mappingCopy, accessibleName) => {
    const key = modelChainKey('claude', 'claude-opus-4-6');
    render(
      <I18nextProvider i18n={localeInstance(lng)}>
        <AgentCard
          agents={[hubAgent]}
          sources={[source('src_b', 'Backup source')]}
          chains={{
            [key]: readyRegion({ manual_override: {hops:[{source_id:'src_b',model_id:'routed-opus'}]}, route_origin: "manual" as const,
              contract_version: 9,
              backend: 'claude',
              model_id: 'claude-opus-4-6',
              current: { source_id: 'src_b', model_id: 'routed-opus' },
              chain: [{ source_id: 'src_b', model_id: 'routed-opus', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }],
              supply_state: 'ok',
            }),
          }}
          pendingBackends={new Set()}
          switchFailures={new Set()}
          connectingBackend={null}
          onConnectHub={vi.fn()}
          onSwitchDirect={vi.fn()}
          onOpenOrder={vi.fn()}
          onOpenRoute={vi.fn()}
          onProbeSettled={vi.fn()}
        />
      </I18nextProvider>,
    );

    const routeButton = screen.getByRole('button', { name: accessibleName });
    const mapping = screen.getByTitle(mappingCopy);
    expect(routeButton.parentElement?.contains(mapping)).toBe(true);
    expect(mapping.parentElement?.textContent).toContain(lng === 'zh' ? '手动' : 'Manual');
    expect(modeCopy).toBeTruthy();
    expect(mapping.textContent).toBe(mappingCopy);
  });

  it('does not call a later current hop takeover unless the head is unavailable for cooldown', () => {
    const key = modelChainKey('claude', 'claude-opus-4-6');
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[hubAgent]} sources={[source('src_a', 'Primary'), source('src_b', 'Backup')]} chains={{ [key]: readyRegion({ manual_override: {hops:[{source_id:'src_a',model_id:'claude-opus-4-6'},{source_id:'src_b',model_id:'claude-opus-4-6'}]}, route_origin: "manual" as const, contract_version: 9, backend: 'claude', model_id: 'claude-opus-4-6', current: { source_id: 'src_b', model_id: 'claude-opus-4-6' }, chain: [{ source_id: 'src_a', model_id: 'claude-opus-4-6', channel: 'native_cli', health: 'healthy', runnable: false, reason: 'native_cli_unavailable', retry_at: null }, { source_id: 'src_b', model_id: 'claude-opus-4-6', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }], supply_state: 'ok' }) }} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);
    expect(screen.getByText(/Backup/)).toBeTruthy();
    expect(screen.queryByText(/takeover/i)).toBeNull();
  });

  it('hides current and takeover projections while the runtime is stopped', () => {
    const key = modelChainKey('claude', 'claude-opus-4-6');
    const stopped = { ...runtime, status: { ...runtime.status, health: 'down' as const } };
    render(<I18nextProvider i18n={i18n}><RuntimeAgentCard runtime={freshRuntimeProjection(readyRegion(stopped))} agents={[hubAgent]} sources={[source('src_a', 'Primary'), source('src_b', 'Backup')]} chains={{ [key]: readyRegion({ manual_override: {hops:[{source_id:'src_a',model_id:'claude-opus-4-6'},{source_id:'src_b',model_id:'claude-opus-4-6'}]}, route_origin: "manual" as const, contract_version: 9, backend: 'claude', model_id: 'claude-opus-4-6', current: { source_id: 'src_b', model_id: 'claude-opus-4-6' }, chain: [{ source_id: 'src_a', model_id: 'claude-opus-4-6', channel: 'hub', health: 'cooldown', runnable: false, reason: null, retry_at: '2099-01-01T00:00:00Z' }, { source_id: 'src_b', model_id: 'claude-opus-4-6', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }], supply_state: 'ok' }) }} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenModels={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);

    expect(screen.queryByText(/Backup/)).toBeNull();
    expect(screen.queryByText(/takeover/i)).toBeNull();
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole('button', { name: 'Manual' })).toBeTruthy();
  });

  it('hides gateway-owned catalog and supply actions in direct mode', () => {
    const onOpenModels = vi.fn();
    const key = modelChainKey('claude', 'claude-opus-4-6');
    const directAgent: AgentSupply = { ...hubAgent, mode: 'direct', sources: null, routes: null, supply_status: null, model_supply: null };
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[directAgent]} sources={[source('src_a', 'Primary'), source('src_b', 'Backup')]} chains={{ [key]: readyRegion({ manual_override: {hops:[{source_id:'src_a',model_id:'claude-opus-4-6'},{source_id:'src_b',model_id:'claude-opus-4-6'}]}, route_origin: "manual" as const, contract_version: 9, backend: 'claude', model_id: 'claude-opus-4-6', current: { source_id: 'src_b', model_id: 'claude-opus-4-6' }, chain: [{ source_id: 'src_a', model_id: 'claude-opus-4-6', channel: 'hub', health: 'cooldown', runnable: false, reason: null, retry_at: '2099-01-01T00:00:00Z' }, { source_id: 'src_b', model_id: 'claude-opus-4-6', channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }], supply_state: 'ok' }) }} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenModels={onOpenModels} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);
    expect(screen.queryByText(/Adjust priority/i)).toBeNull();
    expect(screen.queryByRole('button', { name: /route chain/i })).toBeNull();
    expect(screen.queryByText(/Backup/)).toBeNull();
    expect(screen.queryByText(/takeover/i)).toBeNull();

    expect(screen.getByRole('button', { name: 'Switch to gateway' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Manage models' })).toBeNull();
    expect(onOpenModels).not.toHaveBeenCalled();
  });

  it.each([
    ['en', 'Default routing', 'Switch to direct', 'Switch to gateway'],
    ['zh', '默认路由', '切到直连', '切换到模型网关'],
  ] as const)('uses explicit gateway action labels and icons in %s', async (lng, orderCopy, directCopy, gatewayCopy) => {
    const directAgent: AgentSupply = {
      ...hubAgent,
      backend: 'codex',
      mode: 'direct',
      sources: null,
      routes: null,
      supply_status: null,
      model_supply: null,
      named_agents: [],
    };
    render(
      <I18nextProvider i18n={localeInstance(lng)}>
        <AgentCard
          agents={[hubAgent, directAgent]}
          sources={[]}
          chains={{}}
          pendingBackends={new Set()}
          switchFailures={new Set()}
          connectingBackend={null}
          onConnectHub={vi.fn()}
          onSwitchDirect={vi.fn()}
          onOpenOrder={vi.fn()}
          onOpenRoute={vi.fn()}
          onProbeSettled={vi.fn()}
        />
      </I18nextProvider>,
    );

    const order = screen.getByRole('button', { name: orderCopy });
    const gateway = screen.getByRole('button', { name: gatewayCopy });
    await userEvent.click(screen.getByRole('button', { name: /Runtime mode:|运行模式[:：]/i }));
    const modeGroup = await screen.findByRole('group', { name: /Runtime mode|运行模式/i });
    const direct = within(modeGroup).getByRole('button', { name: new RegExp(directCopy, 'i') });
    expect(within(modeGroup).queryByRole('menuitem')).toBeNull();
    expect(order.querySelector('svg')).toBeTruthy();
    expect(direct.querySelector('.lucide-power')).toBeTruthy();
    expect(gateway.querySelector('svg')).toBeTruthy();
    expect(order.parentElement?.parentElement?.className).toContain('sm:flex-wrap');
    expect(order.parentElement?.className).toContain('items-center');
    expect(order.parentElement?.className).toContain('min-w-0');
    expect(order.parentElement?.parentElement?.parentElement?.className).toContain('min-h-[52px]');
    expect(gateway.className).toContain('bg-primary');
  });

  it('renders the AgentSupply collapse projection and rereads chains on expand and collapse', async () => {
    const onProbeSettled = vi.fn();
    const models = Array.from({ length: COLLAPSED_MODEL_LIMIT + 2 }, (_, index) => `model-${index + 1}`);
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[{
      ...hubAgent,
      builtin_models: models,
      model_supply: models.map((modelId) => ({ route_origin: "manual" as const, model_id: modelId, chain_length: 1, has_runnable_hop: true })),
      routes: {},
    }]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={onProbeSettled} /></I18nextProvider>);

    expect(screen.getAllByRole('button', { name: /route chain/i })).toHaveLength(COLLAPSED_MODEL_LIMIT);
    expect(screen.queryByText(models.at(-1) as string)).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /more model/i }));
    expect(screen.getByText(models.at(-1) as string)).toBeTruthy();
    expect(screen.getByRole('button', { name: /^Collapse$|^收起$/i })).toBeTruthy();
    expect(onProbeSettled).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByRole('button', { name: /^Collapse$|^收起$/i }));
    expect(screen.queryByText(models.at(-1) as string)).toBeNull();
    expect(onProbeSettled).toHaveBeenCalledTimes(2);
    expect(onProbeSettled).toHaveBeenNthCalledWith(1, expect.objectContaining({ backend: 'claude' }));
    expect(onProbeSettled).toHaveBeenNthCalledWith(2, expect.objectContaining({ backend: 'claude' }));
  });

  it('folds a paused route beyond the six-row limit until the group is expanded', async () => {
    const models = Array.from({ length: COLLAPSED_MODEL_LIMIT + 2 }, (_, index) => `model-${index + 1}`);
    const pausedModel = models.at(-1) as string;
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[{
      ...hubAgent,
      builtin_models: models,
      routes: {},
      model_supply: models.map((modelId) => ({ route_origin: "manual" as const,
        model_id: modelId,
        chain_length: 1,
        has_runnable_hop: modelId !== pausedModel,
      })),
    }]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);

    expect(screen.queryByText(pausedModel)).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /more model/i }));
    expect(screen.getByText(pausedModel)).toBeTruthy();
    expect(screen.getByText(/^Supply paused$|^等待供应商恢复$/i)).toBeTruthy();
  });

  it('reveals a newly saved model that lands beyond the collapsed limit', async () => {
    const models = Array.from({ length: COLLAPSED_MODEL_LIMIT }, (_, index) => `model-${index + 1}`);
    const addedModel = 'newly-added-model';
    const catalogued = (ids: string[]): AgentSupply => ({
      ...hubAgent,
      routes: {},
      model_supply: ids.map((modelId) => ({ route_origin: null, model_id: modelId, chain_length: 0, has_runnable_hop: false })),
      catalog_models: ids.map((id) => ({
        id, display_name: null, origin: 'manual', models_dev_id: null, context_window: null, max_output_tokens: null,
        input_modalities: ['text'], output_modalities: ['text'], supports_tools: true, supports_reasoning: false,
        reasoning_efforts: [], locked: false, routeable: true,
      })),
    });
    const props = {
      sources: [], chains: {}, pendingBackends: new Set<string>(), switchFailures: new Set<string>(), connectingBackend: null,
      onConnectHub: vi.fn(), onSwitchDirect: vi.fn(), onOpenOrder: vi.fn(), onOpenRoute: vi.fn(), onProbeSettled: vi.fn(),
    };
    const { rerender } = render(
      <I18nextProvider i18n={i18n}><AgentCard agents={[catalogued(models)]} {...props} /></I18nextProvider>,
    );

    expect(screen.queryByText(addedModel)).toBeNull();
    rerender(
      <I18nextProvider i18n={i18n}><AgentCard agents={[catalogued([...models, addedModel])]} {...props} /></I18nextProvider>,
    );

    expect(screen.getByText(addedModel)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: /^Collapse$|^收起$/i }));
    expect(screen.queryByText(addedModel)).toBeNull();
  });

  it('keeps a chain reread reachable when a short group is unresolved', async () => {
    const onProbeSettled = vi.fn();
    const key = modelChainKey('claude', 'claude-opus-4-6');
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[hubAgent]} sources={[]} chains={{ [key]: { kind: 'unread', retryable: true } }} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={onProbeSettled} /></I18nextProvider>);

    await userEvent.click(screen.getByRole('button', { name: /^Retry$|^重试$/i }));
    expect(onProbeSettled).toHaveBeenCalledOnce();
    expect(onProbeSettled).toHaveBeenCalledWith(expect.objectContaining({ backend: 'claude' }));
  });

  it('opens Frame 02 with the exact backend and model context', async () => {
    const onOpenRoute = vi.fn();
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[hubAgent]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={vi.fn()} onOpenOrder={vi.fn()} onOpenRoute={onOpenRoute} onProbeSettled={vi.fn()} /></I18nextProvider>);

    await userEvent.click(screen.getByRole('button', { name: /Open claude-opus-4-6 route chain/i }));

    expect(onOpenRoute).toHaveBeenCalledWith(hubAgent, 'claude-opus-4-6', expect.any(HTMLElement));
  });

  it('replaces the gateway status slot and action with an in-place retry after leaving fails', async () => {
    const onSwitchDirect = vi.fn();
    render(<I18nextProvider i18n={i18n}><AgentCard agents={[hubAgent]} sources={[]} chains={{}} pendingBackends={new Set()} switchFailures={new Set(['claude'])} connectingBackend={null} onConnectHub={vi.fn()} onSwitchDirect={onSwitchDirect} onOpenOrder={vi.fn()} onOpenRoute={vi.fn()} onProbeSettled={vi.fn()} /></I18nextProvider>);

    expect(screen.getByText(/did not go through/i)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: /Runtime mode:|运行模式[:：]/i }));
    await userEvent.click(await screen.findByRole('button', { name: /retry|重试/i }));
    expect(onSwitchDirect).toHaveBeenCalledWith(hubAgent);
  });
});
