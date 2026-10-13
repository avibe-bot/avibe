// @vitest-environment jsdom
// What the 工具 tab writes and claims (`vibey-hosted-tools.md` §6.1): a toggle and
// an executor pick each replace the tool's whole setting, the picker offers only
// what is verified, and no cost is invented where none is known.
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { readyRegion } from './regionRead';
import { ToolsTab } from './ToolsTab';
import type { HostedEvidence, HostedToolName, HostedToolsStatus, HostedToolStatus, Source } from './types';

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

const unknown: HostedEvidence = { status: 'unknown', stale: null, basis: null, observed_at: null, detail: null };
const verified: HostedEvidence = { status: 'verified', stale: null, basis: 'probe', observed_at: '2026-10-13T10:20:00Z', detail: null };

const tool = (name: HostedToolName, over: Partial<HostedToolStatus> = {}): HostedToolStatus => ({
  name,
  enabled: false,
  executor: null,
  candidates: [],
  ...over,
});

const status = (tools: HostedToolStatus[], reverify = { engine_upgraded: 0, aged: 0 }): HostedToolsStatus => ({ tools, reverify });

const renderTab = (value: HostedToolsStatus, { lng = 'en' as 'en' | 'zh', pending = new Set<HostedToolName>() } = {}) => {
  const onChange = vi.fn();
  render(
    <I18nextProvider i18n={translator(lng)}>
      <ToolsTab read={readyRegion(value)} sources={[CLAUDE, RELAY, GROK, SUBSCRIPTION]} pending={pending} onChange={onChange} onRetry={() => {}} />
    </I18nextProvider>,
  );
  return onChange;
};

const card = (name: string) => screen.getByRole('region', { name });

afterEach(() => cleanup());

describe('ToolsTab writes', () => {
  it('MH-HOSTED-UI-001: a toggle replaces the whole setting and keeps the saved executor', async () => {
    const executor = { source_id: GROK.id, model_id: 'grok-4.6' };
    const onChange = renderTab(status([
      tool('web_search', { enabled: true, executor, candidates: [{ ...executor, call: verified, replay: verified }] }),
      tool('x_search', { candidates: [{ ...executor, call: verified, replay: verified }] }),
    ]));

    await userEvent.click(within(card('Web search')).getByRole('switch', { name: 'Turn off Web search' }));
    // The PUT body is total: a toggle that sent only `enabled` would drop the
    // user's executor back to automatic.
    expect(onChange).toHaveBeenLastCalledWith('web_search', { enabled: false, executor });

    await userEvent.click(within(card('X search')).getByRole('switch', { name: 'Turn on X search' }));
    expect(onChange).toHaveBeenLastCalledWith('x_search', { enabled: true, executor: null });
  });

  it('cannot turn a tool on until a pair is verified to run it, but can always turn it off', async () => {
    const onChange = renderTab(status([
      tool('web_search', { candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: unknown, replay: unknown }] }),
      tool('x_search', { enabled: true, candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: unknown, replay: unknown }] }),
    ]));

    // On with nothing verified, every call would fail with no way to fix it here.
    const off = within(card('Web search')).getByRole('switch', { name: 'Turn on Web search' }) as HTMLButtonElement;
    expect(off.disabled).toBe(true);
    within(card('Web search')).getByText('Off · check a Source first');

    await userEvent.click(within(card('X search')).getByRole('switch', { name: 'Turn off X search' }));
    expect(onChange).toHaveBeenLastCalledWith('x_search', { enabled: false, executor: null });
  });

  it('writes a saved executor back only while the server would still accept it', async () => {
    // The saved pair left the candidates (its model went away): resolution skips
    // it already, and echoing it would get the whole PUT refused.
    const onChange = renderTab(status([tool('web_search', {
      enabled: true,
      executor: { source_id: RELAY.id, model_id: 'gpt-6-retired' },
      candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: unknown, replay: unknown }],
    })]));

    await userEvent.click(within(card('Web search')).getByRole('switch', { name: 'Turn off Web search' }));
    expect(onChange).toHaveBeenLastCalledWith('web_search', { enabled: false, executor: null });
  });

  it('the executor picker offers only verified pairs, and a pick keeps the toggle', async () => {
    const onChange = renderTab(status([
      tool('web_search', {
        enabled: true,
        candidates: [
          { source_id: RELAY.id, model_id: 'gpt-6-astra', call: unknown, replay: unknown },
          { source_id: GROK.id, model_id: 'grok-4.6', call: verified, replay: verified },
        ],
      }),
      tool('x_search', { candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: unknown, replay: unknown }] }),
    ]));

    const picker = within(card('Web search')).getByRole('combobox', { name: 'Proxy executor for Web search' });
    expect(within(picker).getAllByRole('option').map((option) => option.textContent))
      .toEqual(["Automatic: first verified in Vibey's order", 'Grok · grok-4.6']);
    await userEvent.selectOptions(picker, 'Grok · grok-4.6');
    expect(onChange).toHaveBeenLastCalledWith('web_search', { enabled: true, executor: { source_id: GROK.id, model_id: 'grok-4.6' } });
    await userEvent.selectOptions(picker, "Automatic: first verified in Vibey's order");
    expect(onChange).toHaveBeenLastCalledWith('web_search', { enabled: true, executor: null });

    // Nothing verified: nothing to pick, and the control says why.
    const unverified = within(card('X search')).getByRole('combobox', { name: 'Proxy executor for X search' });
    expect((unverified as HTMLButtonElement | HTMLSelectElement).disabled).toBe(true);
    expect((unverified as HTMLSelectElement).selectedOptions[0]?.textContent).toBe('Available after a check');
  });

  it('a saved executor that is no longer verified stays visible as itself', () => {
    const executor = { source_id: GROK.id, model_id: 'grok-4.6' };
    renderTab(status([tool('web_search', {
      enabled: true,
      executor,
      candidates: [
        { source_id: RELAY.id, model_id: 'gpt-6-astra', call: verified, replay: verified },
        { ...executor, call: { ...verified, status: 'unknown', stale: 'wire' }, replay: unknown },
      ],
    })]));

    const picker = within(card('Web search')).getByRole('combobox', { name: 'Proxy executor for Web search' });
    expect((picker as HTMLSelectElement).selectedOptions[0]?.textContent).toBe('Grok · grok-4.6 (not verified)');
    // Only a verified choice is what a turn binds, so no row claims the badge.
    expect(within(card('Web search')).queryByText('Proxy executor', { selector: '.model-hub-pill' })).toBeNull();
  });

  it('keeps a saved executor visible as itself when nothing is verified any more', () => {
    const executor = { source_id: GROK.id, model_id: 'grok-4.6' };
    const onChange = renderTab(status([tool('x_search', {
      enabled: true,
      executor,
      candidates: [{ ...executor, call: { ...verified, status: 'unknown', stale: 'wire' }, replay: unknown }],
    })]));

    const picker = within(card('X search')).getByRole('combobox', { name: 'Proxy executor for X search' }) as HTMLSelectElement;
    expect(picker.disabled).toBe(true);
    expect(picker.selectedOptions[0]?.textContent).toBe('Grok · grok-4.6 (not verified)');
    expect(within(picker).queryByText('Available after a check')).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('holds a tool\'s controls while its write is in flight', () => {
    renderTab(status([
      tool('web_search', { candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: verified, replay: verified }] }),
      tool('x_search', { candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: verified, replay: verified }] }),
    ]), { pending: new Set(['web_search']) });

    expect((within(card('Web search')).getByRole('switch') as HTMLButtonElement | HTMLSelectElement).disabled).toBe(true);
    expect((within(card('Web search')).getByRole('combobox') as HTMLButtonElement | HTMLSelectElement).disabled).toBe(true);
    expect((within(card('X search')).getByRole('switch') as HTMLButtonElement).disabled).toBe(false);
  });
});

describe('ToolsTab claims', () => {
  it('claims routing only while the tool is on, and shows an off tool\'s executor as a saved preference', () => {
    const executor = { source_id: GROK.id, model_id: 'grok-4.6' };
    const candidates = [{ ...executor, call: verified, replay: verified }];
    renderTab(status([
      tool('web_search', { enabled: true, executor, candidates }),
      tool('x_search', { enabled: false, executor, candidates }),
    ]));

    const on = card('Web search');
    within(on).getByText('Proxy executor', { selector: '.model-hub-pill' });
    within(on).getByText(/Other models use Grok/);

    // Off: resolution leaves the tool out entirely, so nothing here runs or bills.
    const off = card('X search');
    expect(within(off).queryByText('Proxy executor', { selector: '.model-hub-pill' })).toBeNull();
    expect(off.textContent).not.toMatch(/Other models use|billed/);
    within(off).getByText('Applies when the tool is on.');
    expect((within(off).getByRole('combobox') as HTMLSelectElement).selectedOptions[0]?.textContent).toBe('Grok · grok-4.6');
  });

  it('MH-HOSTED-UI-003: states a cost only where one is known, and never prices an image', () => {
    renderTab(status([
      tool('web_search', { candidates: [
        { source_id: RELAY.id, model_id: 'gpt-6-astra', call: unknown, replay: unknown },
        { source_id: SUBSCRIPTION.id, model_id: 'gpt-6.1-sol', call: unknown, replay: unknown },
      ] }),
      tool('image_generation', { candidates: [{ source_id: RELAY.id, model_id: 'gpt-6.1-sol', call: unknown, replay: null }] }),
    ]));

    const search = card('Web search');
    const metered = search.querySelector('[data-candidate="src_relay001:gpt-6-astra"]') as HTMLElement;
    const included = search.querySelector('[data-candidate="src_chatgpt1:gpt-6.1-sol"]') as HTMLElement;
    within(included).getByText('Included in subscription');
    // No list price yet for a metered search: unknown is not drawn as zero.
    expect(metered.textContent).not.toMatch(/\$|Included/);
    const image = card('Image generation').querySelector('[data-candidate="src_relay001:gpt-6.1-sol"]') as HTMLElement;
    within(image).getByText('Billed per image token · no per-image price yet');
    expect(image.textContent).not.toMatch(/\$/);
  });

  it('the image cost copy is the orchestrator-decided Chinese line', () => {
    renderTab(status([tool('image_generation', { candidates: [
      { source_id: RELAY.id, model_id: 'gpt-6.1-sol', call: unknown, replay: null },
      { source_id: SUBSCRIPTION.id, model_id: 'gpt-6.1-sol', call: unknown, replay: null },
    ] })]), { lng: 'zh' });

    const image = card('图片生成');
    within(image.querySelector('[data-candidate="src_relay001:gpt-6.1-sol"]') as HTMLElement).getByText('按图像 token 计费 · 暂无单价');
    within(image.querySelector('[data-candidate="src_chatgpt1:gpt-6.1-sol"]') as HTMLElement).getByText('订阅内含');
  });

  it('MH-HOSTED-UI-004: says how many checks an engine upgrade made stale, and nothing when none did', () => {
    renderTab(status([tool('web_search')], { engine_upgraded: 2, aged: 1 }));
    const line = screen.getByRole('status');
    within(line).getByText('Engine upgraded · 2 need a new check');
    within(line).getByText('1 check is over 30 days old');
    cleanup();

    renderTab(status([tool('web_search')]));
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('names the Sources that have no native pair for a tool', () => {
    renderTab(status([tool('x_search', { candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: verified, replay: verified }] })]));

    const note = within(card('X search')).getByText(/have no native X search/);
    // Grok offers the tool natively, so it is the one Source not named. Whether
    // a proxy executor serves the others is not this read's to say.
    expect(note.textContent).toMatch(/^Models on relay-claude, relay-gpt,? (and )?OpenAI subscription have no native X search\.$/);
  });
});
