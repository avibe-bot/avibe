// The Agent band's two contested behaviours: AC-7 (a Direct backend gets no Hub
// chain and no order editor, rather than an empty one) and AC-9 (a supply problem
// is named at the grain the server resolved it to — an Agent, or a model with no
// Agent, never a whole backend).
import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { AgentCard } from './AgentCard';
import type { AgentSupply, Source, SourceState } from './types';

const COPY = zh.settings.models.agents;

const instance = () => {
  const i18n = createInstance();
  void i18n.use(initReactI18next).init({
    lng: 'zh',
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  return i18n;
};

const ACTIVE: SourceState = { status: 'active', retry_at: null, detail_key: null };

const source = (id: string, name: string, state: SourceState = ACTIVE): Source => ({
  id,
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: name,
  protocol: 'anthropic',
  supply_channel: 'hub',
  billing: 'metered',
  state,
  models: [],
});

const hubAgent = (over: Partial<AgentSupply> = {}): AgentSupply => ({
  backend: 'claude',
  mode: 'hub',
  menu_kind: 'fixed',
  selected_by_agent: null,
  selected_model_id: 'claude-opus-4-6',
  current: { model_id: 'claude-opus-4-6', source_id: 'src_a', channel: 'hub' },
  sources: { policy: 'follow', order: ['src_a', 'src_b'], eligibility: [] },
  supply_status: 'ok',
  model_supply: [],
  named_agents: [],
  mappings: [],
  menu: null,
  builtin_models: ['claude-opus-4-6'],
  standard_vendors: null,
  ...over,
});

const SOURCES = [source('src_a', 'Claude Pro 订阅'), source('src_b', 'Anthropic API Key')];

const render = (agents: AgentSupply[], sources: Source[] = SOURCES) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <I18nextProvider i18n={instance()}>
        <AgentCard
          agents={agents}
          sources={sources}
          onConnectHub={vi.fn()}
          onOpenOrder={vi.fn()}
          connectingBackend={null}
        />
      </I18nextProvider>
    </MemoryRouter>,
  );

describe('AgentCard — Hub row', () => {
  it('draws the numbered chain in this backend’s order', () => {
    const html = render([hubAgent()]);
    expect(html).toContain('Claude Pro 订阅');
    expect(html).toContain('Anthropic API Key');
    expect(html.indexOf('Claude Pro 订阅')).toBeLessThan(html.indexOf('Anthropic API Key'));
    expect(html).toContain(COPY.sourceOrder);
  });

  it('says whether the chain is recommended or hand-picked', () => {
    expect(render([hubAgent()])).toContain(COPY.policy.follow);
    expect(render([hubAgent({ sources: { policy: 'custom', order: ['src_a'], eligibility: [] } })])).toContain(
      COPY.policy.custom,
    );
  });

  it('admits it when Hub is on but nothing can supply the backend', () => {
    const html = render([hubAgent({ sources: { policy: 'custom', order: [], eligibility: [] } })]);
    expect(html).toContain(COPY.hubNoSupply);
  });
});

describe('AgentCard — Direct row (AC-7)', () => {
  const direct = hubAgent({
    backend: 'opencode',
    mode: 'direct',
    current: null,
    sources: null,
    supply_status: null,
    model_supply: null,
    named_agents: [{ name: 'opencode', effective_model_id: null, supply_status: null }],
  });

  it('offers 接入中枢 instead of the order editor', () => {
    const html = render([direct]);
    expect(html).toContain(COPY.connectHub);
    expect(html).not.toContain(COPY.sourceOrder);
  });

  it('draws no chain and no policy badge, and says why', () => {
    const html = render([direct]);
    expect(html).toContain(COPY.directNote);
    expect(html).not.toContain('Claude Pro 订阅');
    expect(html).not.toContain(COPY.policy.follow);
    expect(html).not.toContain(COPY.policy.custom);
  });
});

describe('AgentCard — attribution line (AC-9)', () => {
  it('names the interrupted and waiting Agents from the server’s projection', () => {
    const html = render([
      hubAgent({
        named_agents: [
          { name: 'claude', effective_model_id: 'claude-opus-4-6', supply_status: 'interrupted' },
          { name: 'pm', effective_model_id: 'claude-sonnet-4-6', supply_status: 'waiting' },
          { name: 'reviewer', effective_model_id: 'claude-opus-4-6', supply_status: 'ok' },
        ],
      }),
    ]);
    expect(html).toContain('claude 供给中断');
    expect(html).toContain('pm 正在等待来源恢复');
    expect(html).not.toContain('reviewer');
  });

  it('names a ticked-but-unassigned model WITHOUT naming an Agent', () => {
    const html = render([
      hubAgent({
        named_agents: [{ name: 'claude', effective_model_id: 'claude-opus-4-6', supply_status: 'ok' }],
        model_supply: [
          { model_id: 'claude-opus-4-6', chain_length: 2 },
          { model_id: 'claude-haiku-4-5', chain_length: 0 },
        ],
      }),
    ]);
    expect(html).toContain('claude-haiku-4-5 暂无来源可供给');
    expect(html).not.toContain('供给中断');
  });

  it('stays silent when every named Agent is fine', () => {
    const html = render([
      hubAgent({ named_agents: [{ name: 'claude', effective_model_id: 'claude-opus-4-6', supply_status: 'ok' }] }),
    ]);
    expect(html).not.toContain('供给中断');
    expect(html).not.toContain('暂无来源可供给');
  });
});
