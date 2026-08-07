import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { ToastProvider } from '@/context/ToastProvider';
import { AgentCard } from './AgentCard';
import { modelChainKey, type ModelChainIndex } from './modelRows';
import type { AgentChain, AgentSupply, Source } from './types';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'zh',
  fallbackLng: 'en',
  resources: { en: { translation: en }, zh: { translation: zh } },
  interpolation: { escapeValue: false },
});

const source = (id: string, name: string): Source => ({
  id,
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: name,
  protocol: 'anthropic',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  last_discovered_at: null,
  models: [{ id: 'claude-opus-4-6', provenance: 'discovered' }],
});

const agent = (over: Partial<AgentSupply> = {}): AgentSupply => ({
  backend: 'claude',
  mode: 'hub',
  menu_kind: 'fixed',
  selected_by_agent: null,
  selected_model_id: 'claude-opus-4-6',
  sources: { policy: 'follow', order: ['src_a'], eligibility: [] },
  supply_status: 'ok',
  model_supply: [{ model_id: 'claude-opus-4-6', chain_length: 1 }],
  named_agents: [],
  mappings: [],
  menu: null,
  builtin_models: ['claude-opus-4-6', 'claude-sonnet-4-6'],
  standard_vendors: null,
  ...over,
});

const chain = (modelId: string, over: Partial<AgentChain> = {}): AgentChain => ({
  contract_version: 4,
  backend: 'claude',
  model_id: modelId,
  supply_state: 'ok',
  chain: [{
    source_id: 'src_a',
    channel: 'hub',
    via_mapping: false,
    resolved_model_id: null,
    health: 'healthy',
    runnable: true,
    reason: null,
    retry_at: null,
  }],
  ...over,
});

const chains = (...rows: AgentChain[]): ModelChainIndex => Object.fromEntries(
  rows.map((row) => [modelChainKey(row.backend, row.model_id), { kind: 'ready', chain: row }]),
);

const render = (
  agents: AgentSupply[],
  reads: ModelChainIndex,
  issuesOnly = false,
  sourceRows: Source[] = [source('src_a', 'Anthropic API Key'), source('src_b', 'OpenAI API Key')],
  pendingBackends: ReadonlySet<string> = new Set(),
) => renderToStaticMarkup(
  <MemoryRouter>
    <I18nextProvider i18n={i18n}>
      <ToastProvider>
        <AgentCard
          agents={agents}
          sources={sourceRows}
          chains={reads}
          runtime={null}
          issuesOnly={issuesOnly}
          pendingBackends={pendingBackends}
          onConnectHub={vi.fn()}
          onOpenOrder={vi.fn()}
          onOpenModels={vi.fn()}
          onSetRoute={vi.fn()}
          onAddModel={vi.fn()}
          onRepair={vi.fn()}
          onRetest={vi.fn()}
          retestingSourceId={null}
          onProbeSettled={vi.fn()}
          connectingBackend={null}
        />
      </ToastProvider>
    </I18nextProvider>
  </MemoryRouter>,
);

describe('AgentCard model list', () => {
  it('shows every model and the actual serving source without a current badge', () => {
    const html = render(
      [agent()],
      chains(chain('claude-opus-4-6'), chain('claude-sonnet-4-6')),
    );
    expect(html).toContain('claude-opus-4-6');
    expect(html).toContain('claude-sonnet-4-6');
    expect(html).toContain('当前由 Anthropic API Key 供给');
    expect(html).not.toContain('>当前</');
  });

  it('explains an automatic switch on the current model row', () => {
    const failedOver = chain('claude-opus-4-6', {
      chain: [
        {
          source_id: 'src_a',
          channel: 'hub',
          via_mapping: false,
          resolved_model_id: null,
          health: 'needs_action',
          runnable: false,
          reason: null,
          retry_at: null,
        },
        {
          source_id: 'src_b',
          channel: 'hub',
          via_mapping: true,
          resolved_model_id: 'gpt-5.5',
          health: 'healthy',
          runnable: true,
          reason: null,
          retry_at: null,
        },
      ],
    });
    const html = render([agent()], chains(failedOver, chain('claude-sonnet-4-6')));
    expect(html).toContain('当前已自动换到 OpenAI API Key');
  });

  it('does not turn an absent selection into a Models-page state', () => {
    const html = render([agent({ selected_model_id: null })], chains(chain('claude-opus-4-6')));
    expect(html).toContain('claude-opus-4-6');
    expect(html).not.toContain('尚未选择型号');
    expect(html).not.toContain('href="/agents"');
  });

  it('gives an interrupted model its route door', () => {
    const broken = chain('claude-opus-4-6', { chain: [], supply_state: 'interrupted' });
    const html = render([agent()], chains(broken, chain('claude-sonnet-4-6')));
    expect(html).toContain(zh.settings.models.modelStatus.needsAction);
    expect(html).toContain(zh.settings.models.routes.manual);
  });

  it('gives a blocked non-credential source its retest remedy on the model row', () => {
    const blockedSource = {
      ...source('src_a', 'Anthropic API Key'),
      state: { status: 'error' as const, detail_key: 'models.source.error.unclassified' as const },
    };
    const blocked = chain('claude-opus-4-6', {
      supply_state: 'interrupted',
      chain: [{
        source_id: 'src_a',
        channel: 'hub',
        via_mapping: false,
        resolved_model_id: null,
        health: 'needs_action',
        runnable: false,
        reason: null,
        retry_at: null,
      }],
    });
    const html = render([agent()], chains(blocked, chain('claude-sonnet-4-6')), false, [blockedSource]);
    expect(html).toContain(zh.settings.models.repair.retest);
    expect(html).not.toContain(zh.settings.models.routes.manual);
  });

  it('routes a native CLI process blocker to backend settings instead of inventory actions', () => {
    const nativeSource = {
      ...source('src_a', 'Claude subscription'),
      kind: 'subscription' as const,
      supply_channel: 'native_cli' as const,
      billing: 'monthly' as const,
    };
    const blocked = chain('claude-opus-4-6', {
      supply_state: 'interrupted',
      chain: [{
        source_id: 'src_a',
        channel: 'native_cli',
        via_mapping: false,
        resolved_model_id: null,
        health: 'healthy',
        runnable: false,
        reason: 'native_cli_unavailable',
        retry_at: null,
      }],
    });
    const html = render([agent()], chains(blocked, chain('claude-sonnet-4-6')), false, [nativeSource]);
    expect(html).toContain(zh.models.probe.native_cli_unavailable);
    expect(html).toContain('href="/admin/settings/backends/claude"');
    expect(html).not.toContain(zh.settings.models.routes.manual);
    expect(html).not.toContain(zh.settings.models.sources.addModel);
  });

  it('routes an off-order OpenCode supplier to source order instead of manual inventory', () => {
    const enabled = {
      ...source('src_enabled', 'Enabled source'),
      models: [{ id: 'another-model', provenance: 'discovered' as const }],
    };
    const outside = source('src_outside', 'Disabled supplier');
    const open = agent({
      backend: 'opencode',
      menu_kind: 'open',
      selected_model_id: 'anthropic/claude-opus-4-6',
      sources: {
        policy: 'custom',
        order: ['src_enabled'],
        eligibility: [
          { source_id: 'src_enabled', eligible: true },
          { source_id: 'src_outside', eligible: true },
        ],
      },
      model_supply: [{ model_id: 'anthropic/claude-opus-4-6', chain_length: 0 }],
      mappings: [],
      menu: { view: 'featured', checked: ['anthropic/claude-opus-4-6'] },
      builtin_models: null,
      standard_vendors: ['anthropic'],
    });
    const interrupted = chain('anthropic/claude-opus-4-6', { backend: 'opencode', supply_state: 'interrupted', chain: [] });
    const html = render([open], chains(interrupted), false, [enabled, outside]);
    expect(html.match(new RegExp(zh.settings.models.agents.sourceOrder, 'g'))).toHaveLength(2);
    expect(html).not.toContain(zh.settings.models.sources.addModel);
  });

  it('disables every OpenCode model-menu entry while the agent write is pending', () => {
    const open = agent({ backend: 'opencode', menu_kind: 'open', builtin_models: null });
    const pending = new Set(['opencode']);
    const footer = render([open], {}, false, undefined, pending);
    expect(footer).toMatch(/<button[^>]*disabled=""[^>]*>(?:(?!<\/button>)[\s\S])*?管理型号<\/button>/);

    const emptyMenu = render([
      agent({
        backend: 'opencode',
        menu_kind: 'open',
        selected_model_id: null,
        model_supply: [],
        mappings: [],
        menu: { view: 'featured', checked: [] },
        builtin_models: null,
      }),
    ], {}, false, undefined, pending);
    expect(emptyMenu).toContain(zh.settings.models.agents.emptyModels);
    expect(emptyMenu).toMatch(/<button[^>]*disabled=""[^>]*>(?:(?!<\/button>)[\s\S])*?管理型号<\/button>/);
  });

  it('filters to affected rows without leaving healthy siblings behind', () => {
    const html = render(
      [agent()],
      chains(chain('claude-opus-4-6', { chain: [], supply_state: 'interrupted' }), chain('claude-sonnet-4-6')),
      true,
    );
    expect(html).toContain('claude-opus-4-6');
    expect(html).not.toContain('claude-sonnet-4-6');
  });

  it('keeps a cooling model in the affected view without offering manual repair', () => {
    const waiting = chain('claude-opus-4-6', {
      supply_state: 'waiting',
      chain: [{
        source_id: 'src_a',
        channel: 'hub',
        via_mapping: false,
        resolved_model_id: null,
        health: 'cooldown',
        runnable: false,
        reason: null,
        retry_at: '2026-07-31T04:00:00Z',
      }],
    });
    const html = render([agent({ builtin_models: ['claude-opus-4-6'] })], chains(waiting), true);
    expect(html).toContain('claude-opus-4-6');
    expect(html).toContain(zh.settings.models.modelStatus.cooldown);
    expect(html).toContain('data-model-issue="true"');
    expect(html).not.toContain(zh.settings.models.routes.manual);
  });

  it('gives a chain read failure a retry door', () => {
    const html = render([agent()], {
      [modelChainKey('claude', 'claude-opus-4-6')]: { kind: 'error' },
      [modelChainKey('claude', 'claude-sonnet-4-6')]: { kind: 'ready', chain: chain('claude-sonnet-4-6') },
    });
    expect(html).toContain(zh.settings.models.modelStatus.needsAction);
    expect(html).toContain(zh.settings.models.modelStatus.retry);
  });

  it('keeps Direct honest and offers the managed-mode action instead of order editing', () => {
    const html = render([agent({ mode: 'direct', sources: null })], {});
    expect(html).toContain(zh.settings.models.modelStatus.direct);
    expect(html).toContain(zh.settings.models.agents.enableManaged);
    expect(html).not.toContain(zh.settings.models.agents.sourceOrder);
    expect(html).not.toContain(zh.settings.models.routes.expand);
  });
});
