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
import type { ProbeTarget } from './hostedTools';
import { readyRegion } from './regionRead';
import { ToolsTab } from './ToolsTab';
import type {
  HostedEvidence,
  HostedToolCandidate,
  HostedToolName,
  HostedToolProbeResult,
  HostedToolsStatus,
  HostedToolStatus,
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

const unknown: HostedEvidence = { status: 'unknown', stale: null, basis: null, observed_at: null, detail: null, changes_at: null };
const verified: HostedEvidence = {
  status: 'verified', stale: null, basis: 'probe', observed_at: '2026-10-13T10:20:00Z', detail: null, changes_at: '2026-11-12T10:20:00Z',
};
const SOURCES = [CLAUDE, RELAY, GROK, SUBSCRIPTION];

/** A candidate as the server reports it; billing follows its Source unless a test states it. */
type CandidateInput = Pick<HostedToolCandidate, 'source_id' | 'model_id' | 'call' | 'replay'> & Partial<HostedToolCandidate>;
const tool = (
  name: HostedToolName,
  over: Omit<Partial<HostedToolStatus>, 'candidates'> & { candidates?: CandidateInput[] } = {},
): HostedToolStatus => ({
  name,
  enabled: false,
  executor: null,
  bound: null,
  ...over,
  candidates: (over.candidates ?? []).map((candidate) => ({
    billing: SOURCES.find((entry) => entry.id === candidate.source_id)?.billing ?? 'metered',
    price: null,
    probe_estimate: null,
    ...candidate,
  })),
});

const status = (tools: HostedToolStatus[], reverify = { engine_upgraded: 0, aged: 0 }): HostedToolsStatus => ({ tools, reverify });

type RunProbe = (target: ProbeTarget) => Promise<HostedToolProbeResult | null>;
const renderTab = (
  value: HostedToolsStatus,
  {
    lng = 'en',
    pending = new Set<HostedToolName>(),
    onProbe = vi.fn<RunProbe>(async () => null),
  }: { lng?: 'en' | 'zh'; pending?: ReadonlySet<HostedToolName>; onProbe?: RunProbe } = {},
) => {
  const onChange = vi.fn();
  render(
    <I18nextProvider i18n={translator(lng)}>
      <ToolsTab
        read={readyRegion(value)}
        sources={SOURCES}
        pending={pending}
        probing={new Set()}
        onChange={onChange}
        onProbe={onProbe}
        onRetry={() => {}}
      />
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
      tool('x_search', { bound: executor, candidates: [{ ...executor, call: verified, replay: verified }] }),
    ]));

    await userEvent.click(within(card('Web search')).getByRole('switch', { name: 'Turn off Web search' }));
    // The PUT body is total: a toggle that sent only `enabled` would drop the
    // user's executor back to automatic.
    expect(onChange).toHaveBeenLastCalledWith('web_search', { enabled: false, executor });

    await userEvent.click(within(card('X search')).getByRole('switch', { name: 'Turn on X search' }));
    expect(onChange).toHaveBeenLastCalledWith('x_search', { enabled: true, executor: null });
  });

  it('MH-HOSTED-UI-018: cannot turn a tool on until a pair is verified to run it, but can always turn it off', async () => {
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

  it('MH-HOSTED-UI-018: keeps a tool off while the server binds no executor for it, and says to choose one when a verified pair exists', async () => {
    // Verified, but outside Vibey's Source order: Automatic binds nothing (`bound: null`).
    const onChange = renderTab(status([tool('web_search', {
      candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: verified, replay: verified }],
    })]));
    const toggle = within(card('Web search')).getByRole('switch', { name: 'Turn on Web search' }) as HTMLButtonElement;
    expect(toggle.disabled).toBe(true);
    within(card('Web search')).getByText('Off · choose an executor first');
    // Picking the verified pair explicitly is how it binds.
    await userEvent.selectOptions(within(card('Web search')).getByRole('combobox'), 'Grok · grok-4.6');
    expect(onChange).toHaveBeenLastCalledWith('web_search', { enabled: false, executor: { source_id: GROK.id, model_id: 'grok-4.6' } });
  });

  it('MH-HOSTED-UI-001: writes a saved executor back only while the server would still accept it', async () => {
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

  it('MH-HOSTED-UI-001: the executor picker offers only verified pairs, and a pick keeps the toggle', async () => {
    const onChange = renderTab(status([
      tool('web_search', {
        enabled: true,
        bound: { source_id: GROK.id, model_id: 'grok-4.6' },
        candidates: [
          { source_id: RELAY.id, model_id: 'gpt-6-astra', call: unknown, replay: unknown },
          { source_id: GROK.id, model_id: 'grok-4.6', call: verified, replay: verified },
        ],
      }),
      tool('x_search', { candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: unknown, replay: unknown }] }),
    ]));

    const picker = within(card('Web search')).getByRole('combobox', { name: 'Proxy executor for Web search' });
    expect(within(picker).getAllByRole('option').map((option) => option.textContent))
      // Automatic names what the server binds for it now.
      .toEqual(['Automatic (Grok · grok-4.6)', 'Grok · grok-4.6']);
    await userEvent.selectOptions(picker, 'Grok · grok-4.6');
    expect(onChange).toHaveBeenLastCalledWith('web_search', { enabled: true, executor: { source_id: GROK.id, model_id: 'grok-4.6' } });
    await userEvent.selectOptions(picker, 'Automatic (Grok · grok-4.6)');
    expect(onChange).toHaveBeenLastCalledWith('web_search', { enabled: true, executor: null });

    // Nothing verified: nothing to pick, and the control says why.
    const unverified = within(card('X search')).getByRole('combobox', { name: 'Proxy executor for X search' });
    expect((unverified as HTMLButtonElement | HTMLSelectElement).disabled).toBe(true);
    expect((unverified as HTMLSelectElement).selectedOptions[0]?.textContent).toBe('Available after a check');
  });

  it('MH-HOSTED-UI-001: a saved executor that is no longer verified stays visible as itself', () => {
    const executor = { source_id: GROK.id, model_id: 'grok-4.6' };
    renderTab(status([tool('web_search', {
      enabled: true,
      executor,
      // The saved choice is stale, so the server binds the first verified pair.
      bound: { source_id: RELAY.id, model_id: 'gpt-6-astra' },
      candidates: [
        { source_id: RELAY.id, model_id: 'gpt-6-astra', call: verified, replay: verified },
        { ...executor, call: { ...verified, status: 'unknown', stale: 'wire' }, replay: unknown },
      ],
    })]));

    const picker = within(card('Web search')).getByRole('combobox', { name: 'Proxy executor for Web search' });
    expect((picker as HTMLSelectElement).selectedOptions[0]?.textContent).toBe('Grok · grok-4.6 (not verified)');
    // The badge is on what a turn binds, not on the saved choice.
    const badge = within(card('Web search')).getByText('Proxy executor', { selector: '.model-hub-pill' });
    expect(badge.closest('[data-candidate]')?.getAttribute('data-candidate')).toBe('src_relay001:gpt-6-astra');
  });

  it('MH-HOSTED-UI-001: keeps a saved executor visible as itself when nothing is verified any more', () => {
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

  it('MH-HOSTED-UI-001: holds a tool\'s controls while its write is in flight', () => {
    renderTab(status([
      tool('web_search', { candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: verified, replay: verified }] }),
      tool('x_search', { bound: { source_id: GROK.id, model_id: 'grok-4.6' }, candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: verified, replay: verified }] }),
    ]), { pending: new Set(['web_search']) });

    expect((within(card('Web search')).getByRole('switch') as HTMLButtonElement | HTMLSelectElement).disabled).toBe(true);
    expect((within(card('Web search')).getByRole('combobox') as HTMLButtonElement | HTMLSelectElement).disabled).toBe(true);
    expect((within(card('X search')).getByRole('switch') as HTMLButtonElement).disabled).toBe(false);
  });
});

describe('ToolsTab claims', () => {
  it('MH-HOSTED-UI-018: claims routing only while the tool is on, and shows an off tool\'s executor as a saved preference', () => {
    const executor = { source_id: GROK.id, model_id: 'grok-4.6' };
    const candidates = [{ ...executor, call: verified, replay: verified }];
    renderTab(status([
      tool('web_search', { enabled: true, executor, bound: executor, candidates }),
      tool('x_search', { enabled: false, executor, bound: executor, candidates }),
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
        {
          source_id: RELAY.id, model_id: 'gpt-6-astra', call: unknown, replay: unknown,
          price: { unit: 'call', usd_per_1k: 10, extra: [], as_of: '2026-10-13', url: 'https://openai.com/api/pricing' },
        },
        { source_id: CLAUDE.id, model_id: 'claude-opus-5-5', call: unknown, replay: unknown },
        { source_id: SUBSCRIPTION.id, model_id: 'gpt-6.1-sol', call: unknown, replay: unknown },
      ] }),
      tool('x_search', { candidates: [{
        source_id: GROK.id, model_id: 'grok-4.6', call: unknown, replay: unknown,
        price: { unit: 'post', usd_per_1k: 5, extra: [{ unit: 'profile', usd_per_1k: 10 }], as_of: '2026-10-13', url: 'https://docs.x.ai/pricing' },
      }] }),
      tool('image_generation', { candidates: [{ source_id: RELAY.id, model_id: 'gpt-6.1-sol', call: unknown, replay: null }] }),
    ]));

    const row = (name: string, key: string) => card(name).querySelector(`[data-candidate="${key}"]`) as HTMLElement;
    within(row('Web search', 'src_relay001:gpt-6-astra')).getByText('≈ $0.01 / call');
    within(row('Web search', 'src_relay001:gpt-6-astra')).getByText('At API price');
    // No list price for this provider: said to be unknown, never drawn as zero.
    within(row('Web search', 'src_claude01:claude-opus-5-5')).getByText('No list price');
    expect(row('Web search', 'src_claude01:claude-opus-5-5').textContent).not.toMatch(/\$/);
    within(row('Web search', 'src_chatgpt1:gpt-6.1-sol')).getByText('Included in subscription');
    within(row('X search', 'src_grok0001:grok-4.6')).getByText('≈ $0.005 / post');
    within(row('X search', 'src_grok0001:grok-4.6')).getByText('At API price · + $0.01 / profile');
    within(row('Image generation', 'src_relay001:gpt-6.1-sol')).getByText('Billed per image token · no per-image price yet');
    expect(row('Image generation', 'src_relay001:gpt-6.1-sol').textContent).not.toMatch(/\$/);
  });

  it('MH-HOSTED-UI-003: the image cost copy is the orchestrator-decided Chinese line', () => {
    renderTab(status([tool('image_generation', { candidates: [
      { source_id: RELAY.id, model_id: 'gpt-6.1-sol', call: unknown, replay: null },
      { source_id: SUBSCRIPTION.id, model_id: 'gpt-6.1-sol', call: unknown, replay: null },
    ] })]), { lng: 'zh' });

    const image = card('图片生成');
    within(image.querySelector('[data-candidate="src_relay001:gpt-6.1-sol"]') as HTMLElement).getByText('按图像 token 计费 · 暂无单价');
    within(image.querySelector('[data-candidate="src_chatgpt1:gpt-6.1-sol"]') as HTMLElement).getByText('订阅内含');
  });

  it('MH-HOSTED-UI-003: lets the locale compose a price with its extras', () => {
    renderTab(status([tool('x_search', { candidates: [{
      source_id: GROK.id, model_id: 'grok-4.6', call: unknown, replay: unknown,
      price: { unit: 'post', usd_per_1k: 5, extra: [{ unit: 'profile', usd_per_1k: 10 }], as_of: '2026-10-13', url: 'https://docs.x.ai/pricing' },
    }] })]), { lng: 'zh' });
    within(card('X 搜索').querySelector('[data-candidate="src_grok0001:grok-4.6"]') as HTMLElement).getByText('API 标价 · 另加 $0.01 / 个账号');
  });

  it('MH-HOSTED-UI-004: says how many checks an engine upgrade made stale, and nothing when none did', () => {
    renderTab(status([tool('web_search')], { engine_upgraded: 2, aged: 1 }));
    const line = screen.getByRole('status');
    // One sentence the locale composes, its first part marked.
    expect(line.textContent).toBe('Engine upgraded · 2 need a new check · 1 check is over 30 days old');
    expect(within(line).getByText('Engine upgraded · 2 need a new check').className).toContain('model-hub-ink-gold');
    cleanup();

    renderTab(status([tool('web_search')]));
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('MH-HOSTED-UI-006: names the Sources that have no native pair for a tool', () => {
    renderTab(status([tool('x_search', { candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: verified, replay: verified }] })]));

    const note = within(card('X search')).getByText(/have no native X search/);
    // Grok offers the tool natively, so it is the one Source not named. Whether
    // a proxy executor serves the others is not this read's to say.
    expect(note.textContent).toMatch(/^Models on relay-claude, relay-gpt,? (and )?OpenAI subscription have no native X search\.$/);
  });
});

describe('ToolsTab checks', () => {
  const searchable = () => status([
    tool('web_search', { candidates: [
      { source_id: RELAY.id, model_id: 'gpt-6-astra', call: unknown, replay: unknown, probe_estimate: { basis: 'list', usd: 0.02 } },
      { source_id: SUBSCRIPTION.id, model_id: 'gpt-6.1-sol', call: unknown, replay: unknown, probe_estimate: { basis: 'included' } },
      { source_id: GROK.id, model_id: 'grok-4.6', call: verified, replay: verified, probe_estimate: { basis: 'list', usd: 0.01 } },
    ] }),
    tool('image_generation', { candidates: [{ source_id: RELAY.id, model_id: 'gpt-6.1-sol', call: unknown, replay: null }] }),
  ]);
  const result = (outcome: HostedToolProbeResult['call']['outcome'], source_id: string, model_id: string): HostedToolProbeResult => ({
    tool: 'web_search', source_id, model_id, call: { outcome, error: null }, replay: null,
  });

  it('MH-HOSTED-UI-013: checks one row with its cost on the button, and says when the answer was not evidence', async () => {
    const onProbe = vi.fn<RunProbe>(async () => result('inconclusive', RELAY.id, 'gpt-6-astra'));
    renderTab(searchable(), { onProbe });

    const row = card('Web search').querySelector('[data-candidate="src_relay001:gpt-6-astra"]') as HTMLElement;
    await userEvent.click(within(row).getByRole('button', { name: 'Check · about $0.02' }));
    expect(onProbe).toHaveBeenCalledTimes(1);
    expect(onProbe.mock.calls[0][0]).toMatchObject({ tool: 'web_search', candidate: { source_id: RELAY.id, model_id: 'gpt-6-astra' } });
    // The model never used the tool: not recorded as unsupported, and the row offers another try.
    within(row).getByText("Not verified · the model didn't use the tool; try again");
    // Another try is as paid as the first, and says so.
    within(row).getByRole('button', { name: 'Retry · about $0.02' });

    // A verified row checks again; a subscription row says the check costs nothing extra.
    within(card('Web search').querySelector('[data-candidate="src_grok0001:grok-4.6"]') as HTMLElement).getByRole('button', { name: 'Check again · about $0.01' });
    within(card('Web search').querySelector('[data-candidate="src_chatgpt1:gpt-6.1-sol"]') as HTMLElement)
      .getByRole('button', { name: 'Check · included in subscription' });
    // Images cannot be checked yet, so their rows offer nothing to run.
    expect(within(card('Image generation')).queryByRole('button', { name: /Check/ })).toBeNull();
  });

  it('MH-HOSTED-UI-013: runs the header check only after the dialog shows what it sends and costs', async () => {
    const onProbe = vi.fn<RunProbe>(async (target) => target.candidate.source_id === RELAY.id
      ? result('verified', target.candidate.source_id, target.candidate.model_id)
      : { ...result('transient', target.candidate.source_id, target.candidate.model_id), call: { outcome: 'transient', error: 'models.source.cooldown.rate_limited' } });
    renderTab(searchable(), { onProbe });

    // Two unchecked pairs (the verified one and the image are not offered); the subscription adds nothing.
    screen.getByText('2 pairs to check · about $0.02 in total');
    await userEvent.click(screen.getByRole('button', { name: 'Check available tools' }));
    const dialog = await screen.findByRole('dialog');
    within(dialog).getByText('This sends 2 real requests to the Sources below to confirm they can run these tools natively.');
    expect(onProbe).not.toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole('button', { name: 'Start check · about $0.02' }));
    await within(dialog).findByText('Check finished');
    expect(onProbe).toHaveBeenCalledTimes(2);
    within(dialog).getByText('1 runs natively');
    within(dialog).getByText('1 not verified');
    // The reason sits where the locale puts it in the sentence.
    within(dialog).getByText('Couldn\'t verify this time (Rate limited). Try again; this is not recorded as unsupported.');

    // Retrying runs only the pair that got no answer.
    await userEvent.click(within(dialog).getByRole('button', { name: 'Retry 1 · included in subscription' }));
    await within(dialog).findByText('Check finished');
    expect(onProbe).toHaveBeenCalledTimes(3);
    expect(onProbe.mock.calls[2][0]).toMatchObject({ candidate: { source_id: SUBSCRIPTION.id } });
  });

  it('MH-HOSTED-UI-013: says why a failed check failed, and never shows the server\'s key as text', async () => {
    const failed = (error: string): HostedToolProbeResult => ({ ...result('inconclusive', RELAY.id, 'gpt-6-astra'), call: { outcome: 'transient', error } });
    const onProbe = vi.fn<RunProbe>()
      .mockResolvedValueOnce(failed('models.source.cooldown.rate_limited'))
      // A detail key this bundle has no copy for.
      .mockResolvedValueOnce(failed('models.source.cooldown.network'));
    renderTab(searchable(), { onProbe });
    const row = card('Web search').querySelector('[data-candidate="src_relay001:gpt-6-astra"]') as HTMLElement;

    await userEvent.click(within(row).getByRole('button', { name: 'Check · about $0.02' }));
    within(row).getByText("Couldn't verify · Rate limited");
    await userEvent.click(within(row).getByRole('button', { name: 'Retry · about $0.02' }));
    within(row).getByText("Couldn't verify · try again");
    expect(row.textContent).not.toContain('models.source');
  });

  it('MH-HOSTED-UI-018: says plainly when a tool cannot be checked yet, instead of asking for a check it cannot run', () => {
    renderTab(status([tool('image_generation', { candidates: [{ source_id: RELAY.id, model_id: 'gpt-6.1-sol', call: unknown, replay: null }] })]));
    const image = card('Image generation');
    within(image).getByText('Off · checks for this tool come in a later update');
    expect((within(image).getByRole('switch') as HTMLButtonElement).disabled).toBe(true);
    expect(within(image).queryByRole('button', { name: /Check/ })).toBeNull();
  });

  it('MH-HOSTED-UI-003: says a check\'s cost is unknown before it runs, when no estimate is known', () => {
    renderTab(status([tool('web_search', { candidates: [{ source_id: CLAUDE.id, model_id: 'claude-opus-5-5', call: unknown, replay: unknown }] })]));
    within(card('Web search')).getByRole('button', { name: 'Check · cost unknown' });
  });

  it('MH-HOSTED-UI-018: says an enabled tool with nothing verified fails its calls, instead of naming a proxy', () => {
    renderTab(status([tool('x_search', {
      enabled: true,
      candidates: [{ source_id: GROK.id, model_id: 'grok-4.6', call: { ...verified, status: 'unknown', stale: 'wire' }, replay: unknown }],
    })]));
    within(card('X search')).getByText('No Source is verified, so calls to it fail until you check one.');
  });
});

