// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createInstance } from 'i18next';
import { useState } from 'react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import { blankBackendModel } from '../settings/models/backendCatalog';
import type { CollectionReadAuthority } from '../settings/models/collectionReadAuthority';
import type { AgentChain, AgentSupply, RouteHop, Source } from '../settings/models/types';
import { DefaultRouteDialog } from './DefaultRouteDialog';
import { INITIAL_SETUP_FLOW_STATE, type SetupFlowState, type SetupScreenId } from './setupFlow';
import type { SetupRouteFocus } from './setupRoute';

const mock = vi.hoisted(() => ({
  api: {
    listVibeAgents: vi.fn(),
    getVibeAgent: vi.fn(),
    updateVibeAgent: vi.fn(),
  },
  models: {
    listSources: vi.fn(),
    getAgentChain: vi.fn(),
    previewAgentChain: vi.fn(),
    putAgentChain: vi.fn(),
  },
}));

vi.mock('../../context/ApiContext', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../context/ApiContext')>(),
  useApi: () => mock.api,
}));
vi.mock('../settings/models/modelsApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../settings/models/modelsApi')>();
  return { ...actual, modelsApi: { ...actual.modelsApi, ...mock.models } };
});

const i18n = createInstance();
await i18n.init({ lng: 'en', fallbackLng: 'en', resources: { en: { translation: en } } });

const A: RouteHop = { source_id: 'src_a', model_id: 'opus-5' };
const B: RouteHop = { source_id: 'src_b', model_id: 'sonnet-4' };

const chainOf = (hops: RouteHop[], origin: AgentChain['route_origin'] = 'manual'): AgentChain => ({
  contract_version: 10,
  backend: 'claude',
  model_id: 'opus-5',
  manual_override: origin === 'manual' ? { hops } : null,
  route_origin: origin,
  current: hops[0] ?? null,
  chain: hops.map((hop) => ({
    source_id: hop.source_id,
    model_id: hop.model_id,
    channel: 'hub',
    health: 'healthy',
    runnable: true,
    reason: null,
    retry_at: null,
  })),
  supply_state: 'ok',
});

const sources: Source[] = [
  {
    id: 'src_a', vendor: 'anthropic', display_name: 'Anthropic', kind: 'api_key', protocol: 'anthropic',
    supply_channel: 'hub', billing: 'metered', state: { status: 'active' }, last_discovered_at: null,
    models: [{ id: 'opus-5', origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null }],
  },
  {
    id: 'src_b', vendor: 'anthropic', display_name: 'Claude subscription', kind: 'subscription', protocol: 'anthropic',
    supply_channel: 'hub', billing: 'monthly', state: { status: 'active' }, last_discovered_at: null,
    models: [{ id: 'sonnet-4', origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null }],
  },
];

const agent = {
  id: 'claude-claude', name: 'claude', display_name: 'claude', description: null, backend: 'claude' as const,
  model: 'opus-5', reasoning_effort: null, enabled: true, archived: false, archived_at: null, source: 'file',
  updated_at: '', system_prompt: null, created_at: '', metadata: { builtin_default: true },
};

const supplies: AgentSupply[] = [{
  backend: 'claude', cli_present: true, mode: 'hub', menu_kind: 'fixed',
  named_agents: [{ name: 'claude', effective_model_id: 'opus-5', supply_status: 'ok' }],
  sources: {
    order: ['src_a', 'src_b'],
    eligibility: [
      { source_id: 'src_a', eligible: true },
      { source_id: 'src_b', eligible: true },
    ],
  },
}];

const agentReads: CollectionReadAuthority<AgentSupply[]> = {
  read: async () => ({ kind: 'current', value: supplies }),
  refresh: async () => ({ kind: 'current', value: supplies }),
  readValue: async () => supplies,
  invalidate: () => undefined,
};

function Host({
  onNavigate = vi.fn(),
  focus,
  onSaved,
  reads = agentReads,
}: {
  onNavigate?: (screen: SetupScreenId) => void;
  focus?: SetupRouteFocus;
  onSaved?: (focus: SetupRouteFocus) => void;
  reads?: CollectionReadAuthority<AgentSupply[]>;
}) {
  const [flowState, setFlowState] = useState<SetupFlowState>(INITIAL_SETUP_FLOW_STATE);
  const [open, setOpen] = useState(true);
  return (
    <I18nextProvider i18n={i18n}>
      <button type="button" onClick={() => setOpen(true)}>reopen</button>
      <DefaultRouteDialog
        open={open}
        onClose={() => setOpen(false)}
        flowState={flowState}
        setFlowState={setFlowState}
        onNavigate={(screen) => { setOpen(false); onNavigate(screen); }}
        agentReads={reads}
        focus={focus}
        onSaved={onSaved}
      />
    </I18nextProvider>
  );
}

describe('DefaultRouteDialog', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
    Element.prototype.scrollIntoView = vi.fn();
    mock.api.listVibeAgents.mockResolvedValue({ ok: true, agents: [agent], default_agent_name: 'claude' });
    mock.api.getVibeAgent.mockResolvedValue({ ok: true, agent });
    mock.api.updateVibeAgent.mockResolvedValue({ ok: true, agent });
    mock.models.listSources.mockResolvedValue(sources);
    mock.models.getAgentChain.mockResolvedValue(chainOf([A, B]));
    mock.models.previewAgentChain.mockImplementation(async (_backend, _model, body) =>
      chainOf(body.manual_override?.hops ?? []));
    mock.models.putAgentChain.mockImplementation(async (_backend, _model, body) => {
      const next = chainOf(body.hops);
      mock.models.getAgentChain.mockResolvedValue(next);
      return { chain: next };
    });
  });
  afterEach(cleanup);

  it('saves a reordered exact chain and shows that order on reopen', async () => {
    render(<Host />);
    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.route.moveDownNamed.replace('{{name}}', 'Anthropic · opus-5') }));
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.route.done }));
    await waitFor(() => expect(mock.models.putAgentChain).toHaveBeenCalledWith('claude', 'opus-5', { hops: [B, A] }));
    fireEvent.click(screen.getByRole('button', { name: 'reopen' }));
    expect((await screen.findByText(en.onboarding.route.preferred)).closest('.setup-add-row')?.textContent).toContain('sonnet-4');
  });

  it('does not write when Done is pressed on an unchanged opening', async () => {
    render(<Host />);
    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.route.done }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(mock.models.putAgentChain).not.toHaveBeenCalled();
  });

  it('requires an explicit menu model before a model-less assistant can join the route', async () => {
    let configured = false;
    const missing = { ...agent, model: null };
    mock.api.listVibeAgents.mockImplementation(async () => ({
      ok: true, agents: [configured ? agent : missing], default_agent_name: 'claude',
    }));
    mock.api.getVibeAgent.mockImplementation(async () => ({ ok: true, agent: configured ? agent : missing }));
    mock.api.updateVibeAgent.mockImplementation(async () => { configured = true; return { ok: true, agent }; });
    const reads = { ...agentReads, read: async () => ({ kind: 'current' as const, value: [{ ...supplies[0]!, builtin_models: ['opus-5'] }] }) };
    render(<Host reads={reads} />);
    const choose = await screen.findByRole('combobox', { name: 'Choose a model for claude' });
    expect(screen.getByRole('button', { name: en.onboarding.route.done }).hasAttribute('disabled')).toBe(true);
    fireEvent.change(choose, { target: { value: 'opus-5' } });
    fireEvent.click(screen.getByRole('button', { name: 'Set model' }));
    await waitFor(() => expect(mock.api.updateVibeAgent).toHaveBeenCalledWith('claude', { model: 'opus-5' }));
    await waitFor(() => expect(screen.queryByRole('combobox', { name: 'Choose a model for claude' })).toBeNull());
    expect(screen.getByRole('button', { name: en.onboarding.route.done }).hasAttribute('disabled')).toBe(false);
  });

  it('offers only routeable catalog models when repairing a model-less assistant', async () => {
    const missing = { ...agent, model: null };
    mock.api.listVibeAgents.mockResolvedValue({ ok: true, agents: [missing], default_agent_name: 'claude' });
    mock.api.getVibeAgent.mockResolvedValue({ ok: true, agent: missing });
    const reads = { ...agentReads, read: async () => ({ kind: 'current' as const, value: [{
      ...supplies[0]!, catalog_models: [
        { ...blankBackendModel(), id: 'Default', display_name: 'Default', routeable: false },
        { ...blankBackendModel(), id: 'opus-5', display_name: 'Claude Opus', routeable: true },
      ],
    }] }) };
    render(<Host reads={reads} />);
    const choose = await screen.findByRole('combobox', { name: 'Choose a model for claude' });
    expect(Array.from(choose.querySelectorAll('option')).map((option) => option.value)).toEqual(['', 'opus-5']);
  });

  it('keeps a dirty draft when adding a source and returning', async () => {
    const onNavigate = vi.fn();
    render(<Host onNavigate={onNavigate} />);
    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.route.moveDownNamed.replace('{{name}}', 'Anthropic · opus-5') }));
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.route.addSource }));
    expect(onNavigate).toHaveBeenCalledWith('providers');
    expect(mock.models.putAgentChain).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'reopen' }));
    expect(await screen.findByText('Claude subscription · sonnet-4')).toBeTruthy();
    const preferred = screen.getByText(en.onboarding.route.preferred);
    expect(preferred.closest('.setup-add-row')?.textContent).toContain('sonnet-4');
  });

  it('reconciles a chain changed elsewhere while a dirty draft was away', async () => {
    render(<Host />);
    fireEvent.click(await screen.findByRole('button', { name: en.onboarding.route.moveDownNamed.replace('{{name}}', 'Anthropic · opus-5') }));
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.route.addSource }));
    mock.models.getAgentChain.mockResolvedValue(chainOf([A]));
    fireEvent.click(screen.getByRole('button', { name: 'reopen' }));
    await waitFor(() => expect(mock.models.getAgentChain).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: en.onboarding.route.done }));
    await waitFor(() => expect(screen.getByRole('button', { name: en.common.retry })).toBeTruthy());
    expect(mock.models.putAgentChain).not.toHaveBeenCalled();
  });

  it('selects the first exact hop on an empty route and shows it on reopen', async () => {
    const user = userEvent.setup();
    mock.models.getAgentChain.mockResolvedValue(chainOf([], 'automatic'));
    render(<Host />);
    await user.click(await screen.findByRole('button', { name: en.settings.models.routeDialog.addHop }));
    await user.click(screen.getByRole('option', { name: /opus-5/ }));
    await user.click(screen.getByRole('button', { name: en.settings.models.routeDialog.add.confirm }));
    await user.click(screen.getByRole('button', { name: en.onboarding.route.done }));
    await waitFor(() => expect(mock.models.putAgentChain).toHaveBeenCalledWith('claude', 'opus-5', { hops: [A] }));
    fireEvent.click(screen.getByRole('button', { name: 'reopen' }));
    expect((await screen.findByText(en.onboarding.route.preferred)).closest('.setup-add-row')?.textContent).toContain('opus-5');
  });

  it('adds a backup hop to a one-hop route and reads both back', async () => {
    const user = userEvent.setup();
    mock.models.getAgentChain.mockResolvedValue(chainOf([A]));
    render(<Host />);
    await user.click(await screen.findByRole('button', { name: en.settings.models.routeDialog.addHop }));
    await user.click(screen.getByRole('option', { name: /sonnet-4/ }));
    await user.click(screen.getByRole('button', { name: en.settings.models.routeDialog.add.confirm }));
    await user.click(screen.getByRole('button', { name: en.onboarding.route.done }));
    await waitFor(() => expect(mock.models.putAgentChain).toHaveBeenCalledWith('claude', 'opus-5', { hops: [A, B] }));
    fireEvent.click(screen.getByRole('button', { name: 'reopen' }));
    expect((await screen.findByText(en.onboarding.route.preferred)).closest('.setup-add-row')?.textContent).toContain('opus-5');
    expect(screen.getByText(en.onboarding.route.backup.replace('{{index}}', '1')).closest('.setup-add-row')?.textContent).toContain('sonnet-4');
  });

  it('saves the focused Codex card route to both enabled targets', async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    const codex = {
      ...agent,
      id: 'codex-codex',
      name: 'codex',
      display_name: 'codex',
      backend: 'codex' as const,
      model: 'gpt-5',
    };
    const empty = (backend: 'claude' | 'codex', model: string): AgentChain => ({
      ...chainOf([], 'automatic'),
      backend,
      model_id: model,
    });
    mock.api.listVibeAgents.mockResolvedValue({
      ok: true,
      agents: [agent, codex],
      default_agent_name: 'claude',
    });
    mock.api.getVibeAgent.mockImplementation(async (name: string) => ({
      ok: true,
      agent: name === 'codex' ? codex : agent,
    }));
    mock.api.updateVibeAgent.mockImplementation(async (name: string, payload: { model: string }) => {
      codex.model = payload.model;
      return { ok: true, agent: name === 'codex' ? codex : agent };
    });
    mock.models.getAgentChain.mockImplementation(async (backend: string, model: string) => (
      backend === 'codex' ? empty('codex', model) : empty('claude', model)
    ));
    mock.models.previewAgentChain.mockImplementation(async (backend, model, body) => ({
      ...chainOf(body.manual_override?.hops ?? [], 'manual'),
      backend,
      model_id: model,
    }));
    mock.models.putAgentChain.mockImplementation(async (backend, model, body) => {
      const next = { ...chainOf(body.hops), backend, model_id: model };
      mock.models.getAgentChain.mockImplementation(async (nextBackend: string, nextModel: string) => (
        nextBackend === backend && nextModel === model ? next : empty(nextBackend as 'claude' | 'codex', nextModel)
      ));
      return { chain: next };
    });
    const both = [
      ...supplies,
      { ...supplies[0]!, backend: 'codex' as const, named_agents: [{ name: 'codex', effective_model_id: 'gpt-5', supply_status: 'ok' as const }] },
    ];
    const bothReads: CollectionReadAuthority<AgentSupply[]> = {
      read: async () => ({ kind: 'current', value: both }),
      refresh: async () => ({ kind: 'current', value: both }),
      readValue: async () => both,
      invalidate: () => undefined,
    };
    render(<Host focus={{ backend: 'codex', agentName: 'codex' }} onSaved={onSaved} reads={bothReads} />);
    await user.click(await screen.findByRole('button', { name: en.settings.models.routeDialog.addHop }));
    await user.click(screen.getByRole('option', { name: /opus-5/ }));
    await user.click(screen.getByRole('button', { name: en.settings.models.routeDialog.add.confirm }));
    await user.click(screen.getByRole('button', { name: en.onboarding.route.done }));
    await waitFor(() => expect(mock.models.putAgentChain).toHaveBeenCalledWith('codex', 'opus-5', { hops: [A] }));
    expect(mock.models.putAgentChain).toHaveBeenCalledWith('claude', 'opus-5', { hops: [A] });
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith({ backend: 'codex', agentName: 'codex' }));
  });
});
