// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentDetection } from '../steps/AgentDetection';
import en from '../../i18n/en.json';
import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import { ApiCallError } from '../settings/models/modelsApi';
import type { AgentSupply, BackendModelCandidates, ModelCandidate, Source } from '../settings/models/types';
import { inlineCandidates } from './builtinModelOffer';

const mock = vi.hoisted(() => ({ api: {
  detectCli: vi.fn(), installAgent: vi.fn(), getConfig: vi.fn(), getBackendRuntime: vi.fn(), getBackendConnection: vi.fn(),
  mutateConfig: vi.fn(), listVibeAgents: vi.fn(), getVibeAgent: vi.fn(), updateVibeAgent: vi.fn(),
}, showToast: vi.fn(), models: {
  getAgentChains: vi.fn(), listSources: vi.fn(), getAgentModelCandidates: vi.fn(), putAgentModels: vi.fn(),
} }));
vi.mock('../../context/ApiContext', () => ({ useApi: () => mock.api }));
vi.mock('../settings/models/modelsApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../settings/models/modelsApi')>();
  return { ...actual, modelsApi: { ...actual.modelsApi, ...mock.models } };
});
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: mock.showToast }) }));
vi.mock('../settings/shared/useOpencodePermission', () => ({ useOpencodePermission: () => ({ permissionAllowed: true, statusLoaded: true }) }));

const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const wrap = (node: React.ReactNode) => (
  <MemoryRouter><I18nextProvider i18n={i18n}><RouteSurfaceActiveContext.Provider value>{node}</RouteSurfaceActiveContext.Provider></I18nextProvider></MemoryRouter>
);
const card = () => within(screen.getByLabelText('Vibey'));
const enter = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Enter workspace' });

/** No CLI on this machine: the built-in assistant is the only one that can run. */
const data = (extra: Record<string, unknown> = {}) => ({
  __onboardingDetected: true,
  capabilities: { model_hub: { enabled: true } },
  agents: Object.fromEntries(['claude', 'codex', 'opencode'].map((name) => [name, { enabled: true, cli_path: name, status: 'missing' }])),
  ...extra,
});
const source: Source = {
  id: 'src_openai', vendor: 'openai', display_name: 'OpenAI', kind: 'api_key', protocol: 'openai', supply_channel: 'hub',
  billing: 'metered', state: { status: 'active' }, models: [], last_discovered_at: null,
};
const candidate = (id: string, supplied = true): ModelCandidate => ({
  id, display_name: null, reasoning_efforts: [], origin: 'provider',
  suppliers: supplied ? [{ source_id: 'src_openai', source_name: 'OpenAI', model_id: id }] : [],
});
const candidates = (): BackendModelCandidates => ({
  builtin: [],
  providers: [candidate('gpt-5.6-unsupplied', false), candidate('gpt-5.6-sol'), candidate('gpt-5.6-mini'), candidate('gpt-5.6-nano'), candidate('gpt-5.6-pro')],
  in_list: [],
});
/** Vibey's supply: its catalog, and the model its built-in Agent is pinned to. */
const vibeySupply = (models: string[] = [], pinned: string | null = models[0] ?? null): AgentSupply => ({
  backend: 'vibey', cli_present: false, mode: 'hub', menu_kind: 'fixed',
  named_agents: [{ name: 'vibey', effective_model_id: pinned, supply_status: pinned ? 'ok' : null }],
  catalog_models: models.map((id) => ({ id, reasoning_efforts: [] } as never)),
});
let effort: string | null = null;
const vibeyAgent = (model: string | null) => ({
  id: 'vibey-vibey', name: 'vibey', display_name: 'vibey', description: null, backend: 'vibey' as const, model,
  reasoning_effort: effort, enabled: true, archived: false, archived_at: null, source: 'builtin', updated_at: '',
  system_prompt: null, created_at: '', metadata: { builtin_default: true, lock_delete: true },
});
const chain = (modelId: string) => ({
  contract_version: 11, backend: 'vibey', model_id: modelId,
  manual_override: null, route_origin: 'automatic', current: { source_id: 'src_openai', model_id: modelId },
  chain: [{ source_id: 'src_openai', model_id: modelId, channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }],
  supply_state: 'ok',
});

let model: string | null = null;
const reads = {
  read: async () => ({ kind: 'current' as const, value: [vibeySupply(model ? [model] : [])] }),
  refresh: async () => ({ kind: 'current' as const, value: [vibeySupply(model ? [model] : [])] }),
  readValue: async () => [vibeySupply(model ? [model] : [])],
  invalidate: () => undefined,
};

beforeEach(() => {
  vi.resetAllMocks();
  model = null;
  effort = null;
  mock.api.getConfig.mockResolvedValue(data());
  mock.api.detectCli.mockResolvedValue({ found: false });
  mock.api.getBackendRuntime.mockResolvedValue({ installed: false, has_update: false });
  mock.api.getBackendConnection.mockImplementation(async (backend: string) => backend === 'vibey'
    ? { ok: true, backend, installed: true, enabled: true, auth: 'api_key', application: 'applied', ready: true, entry_eligible: true, supply_mode: 'hub' }
    : { ok: true, backend, installed: false, enabled: true, auth: 'none', application: 'applied', ready: false, entry_eligible: false, supply_mode: 'hub' });
  mock.api.listVibeAgents.mockImplementation(async () => ({ ok: true, agents: [vibeyAgent(model)], default_agent_name: 'claude' }));
  mock.api.getVibeAgent.mockImplementation(async () => ({ ok: true, agent: vibeyAgent(model), default_agent_name: 'claude' }));
  mock.api.updateVibeAgent.mockImplementation(async (_name: string, payload: { model: string; reasoning_effort?: string | null }) => {
    model = payload.model;
    if ('reasoning_effort' in payload) effort = payload.reasoning_effort ?? null;
    return { ok: true, agent: vibeyAgent(model) };
  });
  mock.models.listSources.mockResolvedValue([source]);
  mock.models.getAgentChains.mockImplementation(async () => (model ? [chain(model)] : []));
  mock.models.getAgentModelCandidates.mockImplementation(async () => candidates());
  mock.models.putAgentModels.mockImplementation(async () => vibeySupply());
});
afterEach(cleanup);

describe('the built-in assistant card', () => {
  it('offers the first supplied candidates inline, in the picker order', () => {
    expect(inlineCandidates(candidates()).map((row) => row.id)).toEqual(['gpt-5.6-sol', 'gpt-5.6-mini', 'gpt-5.6-nano']);
    const listed = { ...candidates(), in_list: [candidate('gpt-5.6-pro')] };
    expect(inlineCandidates(listed).map((row) => row.id)).toEqual(['gpt-5.6-pro', 'gpt-5.6-sol', 'gpt-5.6-mini']);
  });

  it('is always on, picks its first model inline, and lets setup finish on it alone', async () => {
    const onNext = vi.fn();
    render(wrap(<AgentDetection data={data()} onNext={onNext} onNavigate={vi.fn()} agentReads={reads} />));
    expect(card().getByText(en.onboarding.setup.builtinAlwaysOn)).toBeTruthy();
    expect(card().queryByRole('switch')).toBeNull();
    expect(await card().findByText(en.onboarding.setup.noteBuiltinUnset)).toBeTruthy();
    const pick = await card().findByRole('button', { name: /gpt-5\.6-mini/ });
    expect(card().getAllByRole('button').map((node) => node.textContent)).toEqual([
      'gpt-5.6-solOpenAI', 'gpt-5.6-miniOpenAI', 'gpt-5.6-nanoOpenAI', en.onboarding.setup.allModels,
    ]);
    expect(enter().disabled).toBe(true);
    expect(screen.getByText(en.onboarding.connection.builtinModelHint.replace('{{name}}', 'Vibey'))).toBeTruthy();

    fireEvent.click(pick);
    await waitFor(() => expect(mock.api.updateVibeAgent).toHaveBeenCalledWith('vibey', { model: 'gpt-5.6-mini' }));
    expect(mock.models.putAgentModels).toHaveBeenCalledWith('vibey', expect.objectContaining({
      baseline: [],
      models: [expect.objectContaining({ id: 'gpt-5.6-mini', origin: 'provider' })],
      expected_suppliers: { 'gpt-5.6-mini': [{ source_id: 'src_openai', model_id: 'gpt-5.6-mini' }] },
    }));
    // The catalog write lands before the Agent names the model it adds.
    expect(mock.models.putAgentModels.mock.invocationCallOrder[0]).toBeLessThan(mock.api.updateVibeAgent.mock.invocationCallOrder[0]);
    expect(await card().findByText('gpt-5.6-mini')).toBeTruthy();
    expect(card().getByText(en.onboarding.setup.noteEnabled)).toBeTruthy();
    await waitFor(() => expect(enter().disabled).toBe(false));
    fireEvent.click(enter());
    await waitFor(() => expect(onNext).toHaveBeenCalledWith(expect.objectContaining({ readyBackends: ['vibey'] })));
  });

  it('does not add a model the list already holds; it only names it', async () => {
    mock.models.getAgentModelCandidates.mockImplementation(async () => ({ ...candidates(), in_list: [candidate('gpt-5.6-pro')] }));
    const listed = { ...reads, readValue: async () => [vibeySupply(['gpt-5.6-pro'])] };
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} onNavigate={vi.fn()} agentReads={listed} />));
    fireEvent.click(await card().findByRole('button', { name: /gpt-5\.6-pro/ }));
    await waitFor(() => expect(mock.api.updateVibeAgent).toHaveBeenCalledWith('vibey', { model: 'gpt-5.6-pro' }));
    expect(mock.models.putAgentModels).not.toHaveBeenCalled();
  });

  it('asks again when the suppliers it showed have moved, offering none of the refused rows meanwhile', async () => {
    mock.models.putAgentModels.mockRejectedValueOnce(new ApiCallError('candidate_suppliers_changed', undefined, true, [], [], [], 409));
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} onNavigate={vi.fn()} agentReads={reads} />));
    const pick = await card().findByRole('button', { name: /gpt-5\.6-sol/ });
    let answer!: (read: BackendModelCandidates) => void;
    mock.models.getAgentModelCandidates.mockImplementationOnce(() => new Promise((resolve) => { answer = resolve; }));
    fireEvent.click(pick);
    expect(await card().findByText(en.onboarding.setup.suppliersChanged)).toBeTruthy();
    expect(mock.api.updateVibeAgent).not.toHaveBeenCalled();
    await waitFor(() => expect(mock.models.getAgentModelCandidates).toHaveBeenCalledTimes(2));
    expect(card().queryByRole('button', { name: /gpt-5\.6-/ })).toBeNull();
    await act(async () => answer(candidates()));
    expect(await card().findByRole('button', { name: /gpt-5\.6-sol/ })).toBeTruthy();
    expect(enter().disabled).toBe(true);
  });

  it('moves the Agent\'s effort with its model when the picked model does not take it', async () => {
    effort = 'xhigh';
    mock.models.getAgentModelCandidates.mockImplementation(async () => ({
      ...candidates(), providers: [{ ...candidate('gpt-5.6-sol'), reasoning_efforts: ['low', 'medium', 'high'] }, candidate('gpt-5.6-mini')],
    }));
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} onNavigate={vi.fn()} agentReads={reads} />));
    fireEvent.click(await card().findByRole('button', { name: /gpt-5\.6-sol/ }));
    await waitFor(() => expect(mock.api.updateVibeAgent).toHaveBeenCalledWith('vibey', { model: 'gpt-5.6-sol', reasoning_effort: 'medium' }));
  });

  it('clears an effort a model that states none cannot run, and leaves no effort alone', async () => {
    effort = 'high';
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} onNavigate={vi.fn()} agentReads={reads} />));
    fireEvent.click(await card().findByRole('button', { name: /gpt-5\.6-mini/ }));
    await waitFor(() => expect(mock.api.updateVibeAgent).toHaveBeenCalledWith('vibey', { model: 'gpt-5.6-mini', reasoning_effort: null }));
  });

  it('lets setup finish on another runnable Agent of the built-in backend', async () => {
    const supply = (): AgentSupply[] => [{
      ...vibeySupply(['gpt-5.6-pro'], null),
      named_agents: [
        { name: 'vibey', effective_model_id: null, supply_status: null },
        { name: 'vibey-writer', effective_model_id: 'gpt-5.6-pro', supply_status: 'ok' },
      ],
    }];
    const runnable = { ...reads, read: async () => ({ kind: 'current' as const, value: supply() }), readValue: async () => supply() };
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} onNavigate={vi.fn()} agentReads={runnable} />));
    expect(await card().findByText(en.onboarding.setup.noteBuiltinUnset)).toBeTruthy();
    await waitFor(() => expect(enter().disabled).toBe(false));
  });

  it('never detects a CLI for the built-in backend, on Rescan or on return', async () => {
    const view = render(wrap(<AgentDetection data={data()} onNext={vi.fn()} onNavigate={vi.fn()} agentReads={reads} />));
    await card().findByText(en.onboarding.setup.noteBuiltinUnset);
    await waitFor(() => expect(mock.api.getBackendConnection).toHaveBeenCalledWith('vibey'));
    // A retained screen coming back detects whatever it holds as assistants, and Rescan
    // does too: three CLIs, and never the built-in backend.
    view.rerender(wrap(<AgentDetection data={{ ...data(), __onboardingDetected: false }} active={false} onNext={vi.fn()} onNavigate={vi.fn()} agentReads={reads} />));
    view.rerender(wrap(<AgentDetection data={{ ...data(), __onboardingDetected: false }} active onNext={vi.fn()} onNavigate={vi.fn()} agentReads={reads} />));
    await waitFor(() => expect(mock.api.detectCli).toHaveBeenCalledTimes(3));
    fireEvent.click(screen.getByRole('button', { name: en.agentDetection.rescan }));
    await waitFor(() => expect(mock.api.detectCli).toHaveBeenCalledTimes(6));
    expect(mock.api.detectCli.mock.calls.map(([binary]) => binary)).not.toContain('vibey');
  });

  it('opens every candidate behind All models and uses the first one added', async () => {
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} onNavigate={vi.fn()} agentReads={reads} />));
    fireEvent.click(await card().findByRole('button', { name: en.onboarding.setup.allModels }));
    const dialog = within(await screen.findByRole('dialog'));
    expect(dialog.queryByRole('button', { name: en.settings.models.gateway.picker.custom })).toBeNull();
    fireEvent.click(await dialog.findByRole('checkbox', { name: /gpt-5\.6-pro/ }));
    fireEvent.click(dialog.getByRole('button', { name: 'Add 1 model' }));
    await waitFor(() => expect(mock.api.updateVibeAgent).toHaveBeenCalledWith('vibey', { model: 'gpt-5.6-pro' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('lets All models choose a model the list already holds, without adding it again', async () => {
    const listedRow = { ...candidate('gpt-5.6-pro'), group_if_removed: 'providers' as const };
    mock.models.getAgentModelCandidates.mockImplementation(async () => ({ ...candidates(), in_list: [listedRow] }));
    // Listed, and the Agent still has no model: the server's own fill has not run yet.
    const supply = () => [vibeySupply(['gpt-5.6-pro'], null)];
    const listed = { ...reads, read: async () => ({ kind: 'current' as const, value: supply() }), readValue: async () => supply() };
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} onNavigate={vi.fn()} agentReads={listed} />));
    fireEvent.click(await card().findByRole('button', { name: en.onboarding.setup.allModels }));
    const dialog = within(await screen.findByRole('dialog'));
    const row = await dialog.findByRole('checkbox', { name: /gpt-5\.6-pro/ });
    expect(row.getAttribute('aria-disabled')).not.toBe('true');
    fireEvent.click(row);
    fireEvent.click(dialog.getByRole('button', { name: 'Add 1 model' }));
    await waitFor(() => expect(mock.api.updateVibeAgent).toHaveBeenCalledWith('vibey', { model: 'gpt-5.6-pro' }));
    expect(mock.models.putAgentModels).not.toHaveBeenCalled();
  });

  it('reads as an enabled assistant once it has a model, and opens its route', async () => {
    model = 'gpt-5.6-sol';
    render(wrap(<AgentDetection data={data()} onNext={vi.fn()} onNavigate={vi.fn()} agentReads={reads} />));
    const choice = await card().findByRole('button', { name: 'Model route for Vibey, review and reorder' });
    await waitFor(() => expect(within(choice).getByText('gpt-5.6-sol')).toBeTruthy());
    expect(card().getByText(en.onboarding.setup.noteEnabled)).toBeTruthy();
    expect(mock.models.getAgentModelCandidates).not.toHaveBeenCalled();
    await waitFor(() => expect(enter().disabled).toBe(false));
  });

  it('says the gateway is off and holds its model still', async () => {
    model = 'gpt-5.6-sol';
    render(wrap(<AgentDetection data={data({ agent_supply_blocks: { vibey: 'gateway_off' } })} onNext={vi.fn()} onNavigate={vi.fn()} agentReads={reads} />));
    expect(await card().findByText(en.onboarding.setup.noteGatewayOff.replace('{{name}}', 'Vibey'))).toBeTruthy();
    const choice = await card().findByRole('button', { name: 'Model route for Vibey, review and reorder' });
    await waitFor(() => expect(within(choice).getByText('gpt-5.6-sol')).toBeTruthy());
    expect(choice.getAttribute('aria-disabled')).toBe('true');
    await act(async () => undefined);
    expect(enter().disabled).toBe(true);
  });

  it('says the Hub is disabled on this instance', async () => {
    render(wrap(<AgentDetection data={data({ capabilities: { model_hub: { enabled: false } }, agent_supply_blocks: { vibey: 'hub_disabled' } })}
      onNext={vi.fn()} onNavigate={vi.fn()} agentReads={reads} />));
    expect(await card().findByText(en.onboarding.setup.noteHubDisabled.replace('{{name}}', 'Vibey'))).toBeTruthy();
    expect(card().queryByRole('group')).toBeNull();
    await act(async () => undefined);
    expect(enter().disabled).toBe(true);
  });
});
