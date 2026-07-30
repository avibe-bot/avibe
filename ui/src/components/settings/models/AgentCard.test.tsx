import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { ToastProvider } from '@/context/ToastContext';
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
  models: [{ id: 'claude-opus-4-6', provenance: 'discovered' }],
});

const agent = (over: Partial<AgentSupply> = {}): AgentSupply => ({
  backend: 'claude',
  mode: 'hub',
  menu_kind: 'fixed',
  selected_by_agent: null,
  selected_model_id: 'claude-opus-4-6',
  current: { model_id: 'claude-opus-4-6', source_id: 'src_a', channel: 'hub' },
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

const render = (agents: AgentSupply[], reads: ModelChainIndex, issuesOnly = false) => renderToStaticMarkup(
  <MemoryRouter>
    <I18nextProvider i18n={i18n}>
      <ToastProvider>
        <AgentCard
          agents={agents}
          sources={[source('src_a', 'Anthropic API Key'), source('src_b', 'OpenAI API Key')]}
          chains={reads}
          runtime={null}
          issuesOnly={issuesOnly}
          pendingBackends={new Set()}
          onConnectHub={vi.fn()}
          onOpenOrder={vi.fn()}
          onOpenModels={vi.fn()}
          onSetRoute={vi.fn()}
          onAddModel={vi.fn()}
          onRepair={vi.fn()}
          onProbeSettled={vi.fn()}
          connectingBackend={null}
        />
      </ToastProvider>
    </I18nextProvider>
  </MemoryRouter>,
);

describe('AgentCard model list', () => {
  it('shows every model, the current marker, and the actual serving source', () => {
    const html = render(
      [agent()],
      chains(chain('claude-opus-4-6'), chain('claude-sonnet-4-6')),
    );
    expect(html).toContain('claude-opus-4-6');
    expect(html).toContain('claude-sonnet-4-6');
    expect(html).toContain(zh.settings.models.current);
    expect(html).toContain('当前由 Anthropic API Key 供给');
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

  it('renders an honest row-zero state when no model is selected', () => {
    const html = render([agent({ selected_model_id: null, current: null })], chains(chain('claude-opus-4-6')));
    expect(html).toContain(zh.settings.models.emptySelection.title);
    expect(html).toContain(zh.settings.models.emptySelection.action);
  });

  it('gives an interrupted model its route door', () => {
    const broken = chain('claude-opus-4-6', { chain: [], supply_state: 'interrupted' });
    const html = render([agent()], chains(broken, chain('claude-sonnet-4-6')));
    expect(html).toContain(zh.settings.models.modelStatus.needsAction);
    expect(html).toContain(zh.settings.models.routes.manual);
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

  it('gives a chain read failure a retry door', () => {
    const html = render([agent()], {
      [modelChainKey('claude', 'claude-opus-4-6')]: { kind: 'error' },
      [modelChainKey('claude', 'claude-sonnet-4-6')]: { kind: 'ready', chain: chain('claude-sonnet-4-6') },
    });
    expect(html).toContain(zh.settings.models.modelStatus.needsAction);
    expect(html).toContain(zh.settings.models.modelStatus.retry);
  });

  it('keeps Direct honest and offers the managed-mode action instead of order editing', () => {
    const html = render([agent({ mode: 'direct', sources: null, current: null })], {});
    expect(html).toContain(zh.settings.models.modelStatus.direct);
    expect(html).toContain(zh.settings.models.agents.enableManaged);
    expect(html).not.toContain(zh.settings.models.agents.sourceOrder);
    expect(html).not.toContain(zh.settings.models.routes.expand);
  });
});
