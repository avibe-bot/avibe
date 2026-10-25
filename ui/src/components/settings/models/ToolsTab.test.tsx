// @vitest-environment jsdom
// What the 工具 tab writes and says (`vibey-hosted-tools.md` §8.1): every tool is
// on by default and nothing locks it, a control writes only the field it
// changes, status lines come from real use only, and every refusal can be
// cleared where the line that names it says so.
import * as React from 'react';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { readyRegion } from './regionRead';
import { KEYLESS_ACTIVE_SEARCH } from './searchServices.fixtures';
import { ToolsTab } from './ToolsTab';
import type {
  HostedExecutorCandidate,
  HostedModelExecutorCandidate,
  HostedImageModel,
  HostedImageToolStatus,
  HostedRefusal,
  HostedSearchToolStatus,
  HostedToolName,
  HostedToolPut,
  HostedToolRunStatus,
  HostedToolsStatus,
  Source,
} from './types';

const translator = (lng: 'en' | 'zh') => {
  const instance = createInstance();
  void instance.use(initReactI18next).init({
    lng,
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  return instance;
};

const source = (id: string, name: string, over: Partial<Source> = {}): Source => ({
  id,
  last_discovered_at: null,
  kind: 'api_key',
  vendor: 'openai',
  display_name: name,
  protocol: 'openai_responses',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [],
  ...over,
});

const RELAY = source('src_relay001', 'relay-gpt');
const GROK = source('src_grok0001', 'Grok', { vendor: 'xai' });
const SUBSCRIPTION = source('src_chatgpt1', 'OpenAI subscription', { kind: 'subscription', billing: 'monthly' });
const CLAUDE = source('src_claude01', 'relay-claude', { vendor: 'anthropic', protocol: 'anthropic' });
const SOURCES = [CLAUDE, RELAY, GROK, SUBSCRIPTION];

const run = (over: Partial<HostedToolRunStatus> = {}): HostedToolRunStatus => ({ latest_ok: null, verified: [], refused: [], ...over });
const refusal = (sourceId: string, modelId: string, at: string, aspect: HostedRefusal['aspect'] = 'call'): HostedRefusal => ({
  source_id: sourceId, model_id: modelId, aspect, at, reason: '400 unsupported_tool_type',
});

const candidate = (sourceId: string, modelId: string, over: Partial<HostedModelExecutorCandidate> = {}): HostedModelExecutorCandidate => ({
  kind: 'model',
  source_id: sourceId,
  model_id: modelId,
  billing: SOURCES.find((entry) => entry.id === sourceId)?.billing ?? 'metered',
  relay: false,
  price: null,
  ...over,
});

const search = (name: 'web_search' | 'x_search', over: Partial<HostedSearchToolStatus> = {}): HostedSearchToolStatus => {
  const common = {
    enabled: true, status: run(), executor_order: null,
    effective_order: (over.executor_candidates ?? []).map((entry) => entry.source_id),
    executor_candidates: [], ...over,
  };
  const candidates: HostedExecutorCandidate[] = common.executor_candidates;
  return name === 'web_search'
    ? { prefer_native: true, services: {}, service_order: [], ...common, name }
    : { ...common, name, executor_candidates: candidates.filter((entry): entry is HostedModelExecutorCandidate => entry.kind === 'model') };
};

const image = (over: Partial<HostedImageToolStatus> = {}): HostedImageToolStatus => ({
  name: 'image_generate',
  enabled: true,
  status: run(),
  default_model: null,
  effective_default: null,
  models: [],
  image_offered_by: ['openai', 'gemini'],
  ...over,
});

const renderTab = (
  value: HostedToolsStatus,
  { lng = 'en', pending = new Set<HostedToolName>() }: { lng?: 'en' | 'zh'; pending?: ReadonlySet<HostedToolName> } = {},
) => {
  const onSave = vi.fn();
  const onClear = vi.fn();
  render(
    <I18nextProvider i18n={translator(lng)}>
      <ToolsTab read={readyRegion(value)} sources={SOURCES} pending={pending} onSave={onSave} onClear={onClear} onRetry={() => {}} />
    </I18nextProvider>,
  );
  return { onSave, onClear };
};

const card = (name: string) => screen.getByRole('region', { name });
const openAdvanced = async (user: ReturnType<typeof userEvent.setup>, name: string) => {
  await user.click(within(card(name)).getByRole('button', { name: /^Advanced/ }));
};

afterEach(() => cleanup());

describe('ToolsTab settings', () => {
  it('WF-2V: a flag-off read has no fetch card or controls', () => {
    renderTab({ tools: [search('web_search')] });
    expect(screen.queryByText('Web fetch')).toBeNull();
    expect(screen.queryByText('src_localfetch')).toBeNull();
    screen.getByText('Web search');
  });
  it('MH-HOSTED-UI-018: shows every tool on by default under its capability, and never locks a switch', () => {
    renderTab({ tools: [search('web_search'), search('x_search'), image()] });

    expect(within(screen.getByRole('region', { name: 'Search' })).getAllByRole('region').map((region) => region.getAttribute('aria-label')))
      .toEqual(['Web search', 'X search']);
    expect(within(screen.getByRole('region', { name: 'Generate' })).getByRole('region', { name: 'Image generation' })).toBeTruthy();
    for (const name of ['Web search', 'X search', 'Image generation']) {
      const toggle = within(card(name)).getByRole('switch');
      expect(toggle.getAttribute('aria-checked')).toBe('true');
      expect((toggle as HTMLButtonElement).disabled).toBe(false);
    }
  });

  it('MH-HOSTED-UI-001: a switch writes only whether the tool is on', async () => {
    const user = userEvent.setup();
    const { onSave } = renderTab({ tools: [search('web_search', { executor_order: [GROK.id] }), image({ enabled: false })] });

    await user.click(within(card('Web search')).getByRole('switch'));
    await user.click(within(card('Image generation')).getByRole('switch'));

    expect(onSave.mock.calls).toEqual([['web_search', { enabled: false }], ['image_generate', { enabled: true }]]);
  });

  it('MH-HOSTED-UI-001: holds a tool\'s controls while its write is out, and leaves other tools free', async () => {
    const user = userEvent.setup();
    const { onSave, onClear } = renderTab({
      tools: [
        search('web_search', {
          status: run({ refused: [refusal(RELAY.id, 'gpt-6-astra', '2026-10-14T09:00:00Z')] }),
          executor_candidates: [candidate(GROK.id, 'grok-fast'), candidate(CLAUDE.id, 'claude-haiku-5-5')],
        }),
        search('x_search'),
      ],
    }, { pending: new Set<HostedToolName>(['web_search']) });

    expect((within(card('Web search')).getByRole('switch') as HTMLButtonElement).disabled).toBe(true);
    expect((within(card('Web search')).getByRole('button', { name: 'Clear relay-gpt\'s refusal of web search' }) as HTMLButtonElement).disabled).toBe(true);
    await openAdvanced(user, 'Web search');
    // The arrows stay focusable while the write is out, and do nothing.
    const down = within(card('Web search')).getByRole('button', { name: 'Move Grok down' });
    expect(down.getAttribute('aria-disabled')).toBe('true');
    await user.click(down);
    expect(onSave).not.toHaveBeenCalled();
    expect(onClear).not.toHaveBeenCalled();
    expect((within(card('X search')).getByRole('switch') as HTMLButtonElement).disabled).toBe(false);
  });
});

describe('ToolsTab status lines', () => {
  it('MH-HOSTED-UI-002: says nothing about a tool before its first use', () => {
    renderTab({ tools: [search('web_search'), image()] });

    for (const name of ['Web search', 'Image generation']) {
      expect(within(card(name)).queryByText(/Last run|support|replay/)).toBeNull();
      expect(within(card(name)).queryByRole('button', { name: /Clear/ })).toBeNull();
    }
    // The image card has no Advanced section until its clears move there.
    expect(within(card('Image generation')).queryByRole('button', { name: /^Advanced/ })).toBeNull();
  });

  it('MH-HOSTED-UI-002: names who ran the tool last, with the date only', () => {
    renderTab({ tools: [search('x_search', { status: run({ latest_ok: { source_id: GROK.id, model_id: 'grok-4', mode: 'native', at: '2026-10-15T06:02:00Z' } }) })] }, { lng: 'zh' });

    expect(within(card('X 搜索')).getByText('最近一次由 Grok 执行 · 正常 · 10-15')).toBeTruthy();
  });

  // Built-ins have catalog membership but no persisted Source. Existing status
  // tests only name persisted Sources, so they miss raw ids in latest/fallback copy.
  const builtinLabels = [
    { vendorId: 'exa-mcp', sourceId: 'src_exakeyless', catalogName: 'Exa (free)', en: 'Exa (free)', zh: 'Exa（免费）' },
    { vendorId: 'local-search', sourceId: 'src_local_search', catalogName: 'Local search', en: 'Local search', zh: 'Local search' },
    { vendorId: 'jina', sourceId: 'src_jina_builtin', catalogName: 'Jina', en: 'Jina', zh: 'Jina' },
    // A persisted user name still wins over catalog membership.
    { vendorId: 'local-search', sourceId: RELAY.id, catalogName: 'Local search', en: 'relay-gpt', zh: 'relay-gpt' },
  ];
  it.each(builtinLabels.flatMap((builtin) => (['en', 'zh'] as const).flatMap((lng) =>
    [false, true].map((switched) => ({ ...builtin, lng, switched })),
  )))('MH-HOSTED-UI-002: resolves $vendorId ($sourceId), $lng, fallback=$switched through the shared catalog lookup', ({ vendorId, sourceId, catalogName, lng, switched, ...labels }) => {
    const builtin = KEYLESS_ACTIVE_SEARCH.service_vendors!.find((vendor) => vendor.id === 'exa-mcp')!;
    renderTab({ tools: [{
      ...KEYLESS_ACTIVE_SEARCH,
      // No executor row is needed to name historical evidence; catalog membership
      // is authoritative even when the built-in is not a current candidate.
      executor_candidates: [], effective_order: [],
      service_vendors: [{ ...builtin, id: vendorId, name: catalogName, source_ids: [sourceId] }],
      status: run({
        latest_ok: { source_id: sourceId, model_id: 'builtin/search', mode: 'service', at: '2026-10-25T06:00:00Z' },
        refused: switched ? [refusal(CLAUDE.id, 'claude-haiku-5-5', '2026-10-24T06:00:00Z')] : [],
      }),
    }] }, { lng });

    const name = labels[lng];
    const expected = switched
      ? lng === 'zh' ? `relay-claude 不支持 · claude-haiku-5-5，已改用 ${name}` : `Not supported on relay-claude · claude-haiku-5-5; now using ${name}`
      : lng === 'zh' ? `最近一次由 ${name} 执行 · 正常 · 10-25` : `Last run by ${name} · OK · 10-25`;
    expect(within(card(lng === 'zh' ? '网页搜索' : 'Web search')).getByText(expected)).toBeTruthy();
  });

  it('MH-HOSTED-UI-020: one refusing Source: its line names the models and where the tool runs now, and clears that Source', async () => {
    const user = userEvent.setup();
    const { onClear } = renderTab({
      tools: [search('web_search', {
        status: run({
          latest_ok: { source_id: CLAUDE.id, model_id: 'claude-haiku-5-5', mode: 'executor', at: '2026-10-15T08:00:00Z' },
          refused: [refusal(RELAY.id, 'gpt-6-astra', '2026-10-14T09:00:00Z'), refusal(RELAY.id, 'gpt-6-luna', '2026-10-14T10:00:00Z')],
        }),
      })],
    }, { lng: 'zh' });

    const web = card('网页搜索');
    expect(within(web).getByText('relay-gpt 不支持 · gpt-6-astra、gpt-6-luna，已改用 relay-claude')).toBeTruthy();
    // That line already says where the tool runs, so no separate latest-run line repeats it.
    expect(within(web).queryByText(/最近一次/)).toBeNull();
    await user.click(within(web).getByRole('button', { name: '清除 relay-gpt 的网页搜索拒绝记录' }));
    expect(onClear).toHaveBeenCalledWith('web_search', { source_id: RELAY.id });
  });

  it('MH-HOSTED-UI-020: two refusing Sources: the line names them, and the clears and Clear all sit in Advanced', async () => {
    const user = userEvent.setup();
    const { onClear } = renderTab({
      tools: [search('web_search', {
        status: run({
          verified: [{ source_id: CLAUDE.id, model_id: 'claude-haiku-5-5', mode: 'executor', at: '2026-10-15T08:00:00Z' }],
          refused: [refusal(RELAY.id, 'gpt-6-astra', '2026-10-14T09:00:00Z'), refusal(SUBSCRIPTION.id, 'gpt-6.1-sol', '2026-10-15T07:00:00Z')],
        }),
      })],
    });

    const web = card('Web search');
    expect(within(web).getByText('Not supported on relay-gpt, OpenAI subscription')).toBeTruthy();
    expect(within(web).queryByRole('button', { name: /^Clear/ })).toBeNull();
    expect(within(web).getByRole('button', { name: /^Advanced/ }).textContent).toContain('Execution order: automatic · Status by Source');

    await openAdvanced(user, 'Web search');
    const rows = within(web).getAllByRole('listitem').filter((row) => row.hasAttribute('data-pair'));
    expect(rows.map((row) => row.textContent)).toEqual([
      'relay-claudeclaude-haiku-5-5OK · 10-15',
      'relay-gptgpt-6-astraNot supported · 10-14400 unsupported_tool_typeClear',
      'OpenAI subscriptiongpt-6.1-solNot supported · 10-15400 unsupported_tool_typeClear',
    ]);
    await user.click(within(web).getByRole('button', { name: 'Clear the web search refusal of gpt-6-astra on relay-gpt' }));
    await user.click(within(web).getByRole('button', { name: 'Clear every refusal of web search' }));
    expect(onClear.mock.calls).toEqual([
      ['web_search', { source_id: RELAY.id, model_id: 'gpt-6-astra' }],
      ['web_search', {}],
    ]);
  });

  it('MH-HOSTED-UI-020: a replay refusal has its own line, and a Source that refused the call is not in it', async () => {
    const user = userEvent.setup();
    const { onClear } = renderTab({
      tools: [
        search('web_search', { status: run({ refused: [refusal(GROK.id, 'grok-4', '2026-10-14T09:00:00Z', 'replay')] }) }),
        search('x_search', {
          status: run({ refused: [refusal(RELAY.id, 'gpt-6-astra', '2026-10-14T09:00:00Z'), refusal(RELAY.id, 'gpt-6-luna', '2026-10-14T09:00:00Z', 'replay')] }),
        }),
      ],
    });

    expect(within(card('Web search')).getByText('Grok can\'t replay earlier results')).toBeTruthy();
    await user.click(within(card('Web search')).getByRole('button', { name: 'Clear Grok\'s refusal of web search' }));
    expect(onClear).toHaveBeenCalledWith('web_search', { source_id: GROK.id });
    expect(within(card('X search')).getByText('Not supported on relay-gpt · gpt-6-astra')).toBeTruthy();
    expect(within(card('X search')).queryByText(/replay/)).toBeNull();
  });

  it('MH-HOSTED-UI-020: an image refusal reads and clears like a search refusal, and two move into Advanced', async () => {
    const user = userEvent.setup();
    const { onClear } = renderTab({
      tools: [image({ status: run({ refused: [refusal(RELAY.id, 'gpt-image-2', '2026-10-14T09:00:00Z'), refusal(CLAUDE.id, 'gpt-image-2', '2026-10-14T09:00:00Z')] }) })],
    });

    const imageCard = card('Image generation');
    expect(within(imageCard).getByText('Not supported on relay-gpt, relay-claude')).toBeTruthy();
    expect(within(imageCard).getByRole('button', { name: /^Advanced/ }).textContent).toContain('Status by Source');
    await openAdvanced(user, 'Image generation');
    // Only the clears live here: an image tool has no executor order.
    expect(within(imageCard).queryByText('Execution order')).toBeNull();
    await user.click(within(imageCard).getByRole('button', { name: 'Clear every refusal of image generation' }));
    expect(onClear).toHaveBeenCalledWith('image_generate', {});
  });
});

describe('ToolsTab executor order', () => {
  const PRICED = {
    unit: 'call' as const,
    usd_per_1k: 5,
    extra: [],
    as_of: '2026-10-01',
    url: 'https://docs.x.ai/pricing',
  };

  it('MH-HOSTED-UI-021: lists the executors in effective order, and a move saves the whole shown order', async () => {
    const user = userEvent.setup();
    const { onSave } = renderTab({
      tools: [search('web_search', {
        executor_candidates: [candidate(GROK.id, 'grok-fast', { price: PRICED }), candidate(CLAUDE.id, 'claude-haiku-5-5', { relay: true }), candidate(SUBSCRIPTION.id, 'gpt-6-mini')],
      })],
    });

    await openAdvanced(user, 'Web search');
    const rows = within(card('Web search')).getAllByRole('listitem');
    expect(rows.map((row) => row.getAttribute('data-executor'))).toEqual([GROK.id, CLAUDE.id, SUBSCRIPTION.id]);
    expect(within(card('Web search')).queryByRole('button', { name: 'Reset to automatic' })).toBeNull();
    expect((within(rows[0]).getByRole('button', { name: 'Move Grok up' }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(rows[2]).getByRole('button', { name: 'Move OpenAI subscription down' }) as HTMLButtonElement).disabled).toBe(true);

    await user.click(within(rows[1]).getByRole('button', { name: 'Move relay-claude up' }));
    expect(onSave).toHaveBeenCalledWith('web_search', { executor_order: [CLAUDE.id, GROK.id, SUBSCRIPTION.id] });
  });

  it('MH-HOSTED-UI-021: a move keeps a saved executor the read does not show', async () => {
    const user = userEvent.setup();
    const { onSave } = renderTab({
      tools: [search('web_search', {
        // relay-gpt is saved first but refused now, so it is not a candidate.
        executor_order: [RELAY.id, GROK.id, CLAUDE.id],
        executor_candidates: [candidate(GROK.id, 'grok-fast'), candidate(CLAUDE.id, 'claude-haiku-5-5')],
      })],
    });

    await openAdvanced(user, 'Web search');
    await user.click(screen.getByRole('button', { name: 'Move relay-claude up' }));
    expect(onSave).toHaveBeenCalledWith('web_search', { executor_order: [RELAY.id, CLAUDE.id, GROK.id] });
  });

  it('MH-HOSTED-UI-021: a customized order offers Reset, which saves automatic', async () => {
    const user = userEvent.setup();
    const { onSave } = renderTab({
      tools: [search('x_search', { executor_order: [GROK.id], executor_candidates: [candidate(GROK.id, 'grok-fast')] })],
    });

    expect(within(card('X search')).getByRole('button', { name: /^Advanced/ }).textContent).toContain('Execution order: customized');
    await openAdvanced(user, 'X search');
    await user.click(within(card('X search')).getByRole('button', { name: 'Reset to automatic' }));
    expect(onSave).toHaveBeenCalledWith('x_search', { executor_order: null });
  });

  it('MH-HOSTED-UI-021: announces a move and moves focus only once the server order shows it, and drops one that did not land', async () => {
    const user = userEvent.setup();
    const i18n = translator('en');
    let asked: string[] | null = null;
    const view = (order: string[], pending: ReadonlySet<HostedToolName> = new Set()) => (
      <I18nextProvider i18n={i18n}>
        <ToolsTab
          read={readyRegion({ tools: [search('web_search', { executor_order: order, executor_candidates: order.map((id) => candidate(id, `${id}-mini`)) })] })}
          sources={SOURCES}
          pending={pending}
          onSave={(_tool, body: HostedToolPut) => { asked = body.executor_order ?? null; }}
          onClear={() => {}}
          onRetry={() => {}}
        />
      </I18nextProvider>
    );
    const before = [GROK.id, CLAUDE.id, RELAY.id];
    const writing = new Set<HostedToolName>(['web_search']);
    const { rerender } = render(view(before));
    await openAdvanced(user, 'Web search');
    const status = screen.getByRole('status');

    // Refused: nothing moved, so nothing is announced and nothing waits to take focus.
    await user.click(screen.getByRole('button', { name: 'Move relay-gpt up' }));
    rerender(view(before, writing));
    expect(status.textContent).toBe('');
    rerender(view(before));
    expect(status.textContent).toBe('');
    const elsewhere = screen.getByRole('switch');
    elsewhere.focus();
    rerender(view([GROK.id, RELAY.id, CLAUDE.id]));
    expect(status.textContent).toBe('');
    expect(document.activeElement).toBe(elsewhere);

    // Landed: the server's order shows it, so it is announced and focus follows the row.
    await user.click(screen.getByRole('button', { name: 'Move relay-gpt up' }));
    rerender(view([GROK.id, RELAY.id, CLAUDE.id], writing));
    expect(status.textContent).toBe('');
    rerender(view(asked ?? []));
    expect(screen.getAllByRole('listitem').map((row) => row.getAttribute('data-executor'))).toEqual([RELAY.id, GROK.id, CLAUDE.id]);
    expect(status.textContent).toBe('Moved relay-gpt to position 1 of 3.');
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Move relay-gpt down' }));
  });

  it('MH-HOSTED-UI-021: keeps focus on the moved row once the answer lands, on its other arrow at an end', async () => {
    const user = userEvent.setup();
    const Harness = () => {
      const [order, setOrder] = React.useState([GROK.id, CLAUDE.id, RELAY.id]);
      const value: HostedToolsStatus = {
        tools: [search('web_search', {
          executor_order: order,
          executor_candidates: order.map((id) => candidate(id, `${id}-mini`)),
        })],
      };
      return (
        <I18nextProvider i18n={translator('en')}>
          <ToolsTab
            read={readyRegion(value)}
            sources={SOURCES}
            pending={new Set()}
            onSave={(_tool, body: HostedToolPut) => { if (body.executor_order) setOrder(body.executor_order); }}
            onClear={() => {}}
            onRetry={() => {}}
          />
        </I18nextProvider>
      );
    };
    render(<Harness />);

    await openAdvanced(user, 'Web search');
    await user.click(screen.getByRole('button', { name: 'Move relay-gpt up' }));
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Move relay-gpt up' }));
    await user.click(screen.getByRole('button', { name: 'Move relay-gpt up' }));
    expect(screen.getAllByRole('listitem').map((row) => row.getAttribute('data-executor'))).toEqual([RELAY.id, GROK.id, CLAUDE.id]);
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Move relay-gpt down' }));
  });

  it('MH-HOSTED-UI-003: prices each executor where a price is known, and never shows unknown as zero', async () => {
    const user = userEvent.setup();
    renderTab({
      tools: [search('web_search', {
        executor_candidates: [
          candidate(GROK.id, 'grok-fast', { price: { ...PRICED, extra: [{ unit: 'post', usd_per_1k: 5 }] } }),
          candidate(RELAY.id, 'gpt-6-mini', { relay: true }),
          candidate(SUBSCRIPTION.id, 'gpt-6-mini', { price: PRICED }),
        ],
      })],
    }, { lng: 'zh' });

    await user.click(within(card('网页搜索')).getByRole('button', { name: /^高级/ }));
    const [grok, relay, subscription] = within(card('网页搜索')).getAllByRole('listitem');
    expect(grok.textContent).toContain('$0.005 / 次API 标价 · 另加 $0.005 / 条帖子');
    expect(relay.textContent).toContain('暂无标价');
    expect(relay.textContent).not.toContain('$0');
    // A subscription is never billed per call, whatever list price its vendor has.
    expect(subscription.textContent).toContain('订阅内含不另计费');
  });
});

describe('ToolsTab image card', () => {
  const GOOGLE = source('src_google01', 'Google AI Studio', { vendor: 'gemini', protocol: 'google' });
  const models = [
    { source_id: RELAY.id, model_id: 'gpt-image-2', description: 'GPT Image 2: precise prompts, legible text, edits', billing: 'metered', price: null },
    { source_id: GROK.id, model_id: 'grok-imagine-image', description: 'Grok Imagine: fast drafts', billing: 'metered', price: { usd: 0.02, basis: 'list' } },
    { source_id: GOOGLE.id, model_id: 'gemini-2.5-flash-image', description: 'Nano Banana: fast generation', billing: 'metered', price: { usd: 0.067, basis: 'estimated' } },
  ] satisfies HostedImageModel[];
  const renderImage = (over: Partial<HostedImageToolStatus>, lng: 'en' | 'zh' = 'en', sources: readonly Source[] = [...SOURCES, GOOGLE]) => {
    const onSave = vi.fn();
    render(
      <I18nextProvider i18n={translator(lng)}>
        <ToolsTab read={readyRegion({ tools: [image(over)] })} sources={sources} pending={new Set()} onSave={onSave} onClear={() => {}} onRetry={() => {}} />
      </I18nextProvider>,
    );
    return onSave;
  };

  it('MH-HOSTED-UI-022: lists each offered model with its Source and per-image price, and marks the default in use', () => {
    renderImage({ models, effective_default: { source_id: RELAY.id, model_id: 'gpt-image-2' } }, 'zh');
    const [gpt, grok, banana] = within(card('图片生成')).getAllByRole('listitem');
    // A shipped model's description is the locale's copy; one without copy shows the overlay's line.
    expect(gpt.textContent).toBe('gpt-image-2默认写实照片、图中文字准确，支持按参考图编辑relay-gptAPI Key暂无单价');
    expect(grok.textContent).toContain('Grok Imagine: fast drafts');
    expect(grok.textContent).toContain('$0.02 / 张API 标价');
    expect(banana.textContent).toContain('≈ $0.067 / 张API 标价');
    expect(within(grok).queryByText('默认')).toBeNull();
  });

  it('MH-HOSTED-UI-022: the default picker names what automatic uses, and saves a pick or automatic', async () => {
    const user = userEvent.setup();
    const onSave = renderImage({ models, effective_default: { source_id: RELAY.id, model_id: 'gpt-image-2' } });
    const picker = within(card('Image generation')).getByRole('combobox', { name: 'Default image model' }) as HTMLSelectElement;
    expect(picker.selectedOptions[0].textContent).toBe('Automatic (gpt-image-2 · relay-gpt)');

    await user.selectOptions(picker, 'grok-imagine-image · Grok');
    expect(onSave).toHaveBeenLastCalledWith('image_generate', { default_model: { source_id: GROK.id, model_id: 'grok-imagine-image' } });
    await user.selectOptions(picker, 'Automatic (gpt-image-2 · relay-gpt)');
    expect(onSave).toHaveBeenLastCalledWith('image_generate', { default_model: null });
  });

  it('MH-HOSTED-UI-022: shows an image model on a subscription Source as included, whatever its list price', () => {
    // The billing is the read's own, so it holds while the Sources read has not landed.
    renderImage({ models: [{ ...models[1], source_id: SUBSCRIPTION.id, billing: 'monthly' }] }, 'zh', []);
    const [row] = within(card('图片生成')).getAllByRole('listitem');
    expect(row.textContent).toContain('订阅内含不另计费');
    expect(row.textContent).not.toContain('$0.02');
  });

  it('MH-HOSTED-UI-022: says how automatic picks, not which model, once a model is saved', () => {
    renderImage({
      models,
      default_model: { source_id: GROK.id, model_id: 'grok-imagine-image', available: true },
      effective_default: { source_id: GROK.id, model_id: 'grok-imagine-image' },
    });
    const picker = within(card('Image generation')).getByRole('combobox', { name: 'Default image model' }) as HTMLSelectElement;
    expect(picker.selectedOptions[0].textContent).toBe('grok-imagine-image · Grok');
    expect([...picker.options].map((option) => option.textContent)[0]).toBe('Automatic (first available in catalog order)');
  });

  it('MH-HOSTED-UI-022: keeps an unavailable saved default visible and resettable when no model is offered', async () => {
    const user = userEvent.setup();
    const onSave = renderImage({ models: [], default_model: { source_id: RELAY.id, model_id: 'gpt-image-2', available: false }, effective_default: null });
    const imageCard = card('Image generation');
    expect(within(imageCard).getByText('Image generation needs an API key from one of OpenAI, Google.')).toBeTruthy();
    const picker = within(imageCard).getByRole('combobox', { name: 'Default image model' }) as HTMLSelectElement;
    expect(picker.selectedOptions[0].textContent).toBe('gpt-image-2 · relay-gpt (unavailable)');
    expect(within(imageCard).getByText('The saved default isn\'t available now.')).toBeTruthy();
    await user.selectOptions(picker, 'Automatic (first available in catalog order)');
    expect(onSave).toHaveBeenLastCalledWith('image_generate', { default_model: null });
  });

  it('MH-HOSTED-UI-022: shows a saved default that is no longer offered as itself, and names the one in use', () => {
    renderImage({
      models: models.slice(1),
      default_model: { source_id: RELAY.id, model_id: 'gpt-image-2', available: false },
      effective_default: { source_id: GROK.id, model_id: 'grok-imagine-image' },
    });
    const picker = within(card('Image generation')).getByRole('combobox', { name: 'Default image model' }) as HTMLSelectElement;
    expect(picker.selectedOptions[0].textContent).toBe('gpt-image-2 · relay-gpt (unavailable)');
    expect(within(card('Image generation')).getByText('The saved default isn\'t available now, so grok-imagine-image · Grok is used.')).toBeTruthy();
  });

  it('MH-HOSTED-UI-022: with every offered model refused, points at the clears rather than asking for a key', () => {
    renderImage({ models: [], status: run({ refused: [refusal(RELAY.id, 'gpt-image-2', '2026-10-14T09:00:00Z')] }), image_offered_by: ['openai'] });
    const imageCard = card('Image generation');
    expect(within(imageCard).getByText('No image model is available now. Clear a refusal above to try it again.')).toBeTruthy();
    expect(within(imageCard).queryByText(/API key/)).toBeNull();
    expect(within(imageCard).getByRole('button', { name: 'Clear relay-gpt\'s refusal of image generation' })).toBeTruthy();
  });

  it('MH-HOSTED-UI-022: with no image model offered, names the vendors whose key would add one', () => {
    renderImage({ models: [], image_offered_by: ['openai', 'gemini'] });
    expect(within(card('Image generation')).getByText('Image generation needs an API key from one of OpenAI, Google.')).toBeTruthy();
    expect(within(card('Image generation')).queryByRole('combobox')).toBeNull();
    cleanup();
    // The locale joins the names; no list punctuation runs Latin names into Chinese.
    renderImage({ models: [], image_offered_by: ['openai', 'gemini'] }, 'zh');
    expect(within(card('图片生成')).getByText('图片生成需要 OpenAI、Google 其中一家的 API Key。')).toBeTruthy();
    cleanup();
    renderImage({ models: [], image_offered_by: ['openai'] }, 'zh');
    expect(within(card('图片生成')).getByText('图片生成需要 OpenAI 的 API Key。')).toBeTruthy();
  });
});

