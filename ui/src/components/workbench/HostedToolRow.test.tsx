/** @vitest-environment jsdom */
// What the execution-process group shows for a hosted call (§6.4), rendered from
// the payloads `tests/test_hosted_activity.py` pins the producer to.
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import fixture from '../../lib/hostedActivity.fixture.json';
import { groupFromWire } from '../../lib/agentActivity';
import { ActivityChip } from './AgentActivityGroup';

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

type Payload = keyof Omit<typeof fixture, '_comment'>;

/** The stored rows, through the same wire mapping the /activity endpoint feeds. */
const renderGroup = (payloads: (Payload | Record<string, unknown>)[], lng: 'en' | 'zh' = 'en') => {
  const group = groupFromWire({
    id: 'turn', anchor_message_id: 'reply', anchor_position: 'before', open: false, status: 'done',
    steps: payloads.length, duration_ms: 38000,
    rows: payloads.map((payload, index) => ({
      id: `evt_${index}`, kind: 'tool_call' as const, text: 'the IM line', created_at: '2026-10-13T14:02:00Z',
      hosted: typeof payload === 'string' ? fixture[payload] : payload,
    })),
  });
  render(
    <I18nextProvider i18n={translator(lng)}>
      <ActivityChip group={group} expanded loading={false} onToggle={() => {}} showToolCalls onToggleTools={() => {}} />
    </I18nextProvider>,
  );
};

const row = (callId: string) => document.querySelector(`[data-hosted-call="${callId}"]`) as HTMLElement;

afterEach(() => cleanup());

describe('hosted tool cards', () => {
  it('MH-HOSTED-UI-008: shows a native and a proxied call the same way, each with how it ran and its known cost', () => {
    renderGroup(['native_web_search', 'proxy_x_search']);

    const native = row('srvtoolu_01');
    expect(native.textContent).not.toContain('the IM line');
    within(native).getByText('web_search');
    within(native).getByText('OpenAI announcements October 2026');
    within(native).getByText('Native · Anthropic');
    within(native).getByText('≈ $0.01');
    within(native).getByText('2 sources');

    const proxy = row('call_x1');
    within(proxy).getByText('Via xAI proxy · grok-4.6');
    within(proxy).getByText('$0.11');
    within(proxy).getByText('2 posts');

    // Expanded: who ran it, and the numbered sources with their excerpts.
    fireEvent.click(within(proxy).getByRole('button'));
    within(proxy).getByText('Run by proxy executor Grok · grok-4.6 · took 31 s · $0.11');
    const first = within(proxy).getByRole('link', { name: 'DevDay 2026 recap: everything we shipped this week' });
    expect(first.getAttribute('href')).toBe('https://x.com/OpenAI/status/1');
    within(proxy).getByText('Recordings and the launch list in one post.');
    within(proxy).getByText('x.com · 2 days ago');
  });

  it('says why a call failed instead of counting results it never had', () => {
    renderGroup(['proxy_failed']);
    const failed = row('call_x2');
    within(failed).getByText('Failed');
    expect(failed.textContent).not.toMatch(/\$|post/);
    fireEvent.click(within(failed).getByRole('button'));
    within(failed).getByText('no Source is verified for X search');
  });

  it('MH-HOSTED-UI-008: shows a direct image call by its model and Source, with its size, file and prompt, and no cost when none is known', () => {
    renderGroup([{
      ...fixture.direct_image,
      images: [{ ...fixture.direct_image.images[0], media_url: '/api/media/tok_3f2a?w=1536&h=1024', bytes: 1_993_000 }],
    }]);
    const image = row('call_ig1');
    within(image).getByText('image_generate');
    within(image).getByText('gpt-image-2 · relay-gpt');
    within(image).getByText('1 image · 1536×1024');
    expect(image.textContent).not.toMatch(/\$/);

    fireEvent.click(within(image).getByRole('button'));
    within(image).getByText('Ran directly on relay-gpt · gpt-image-2');
    const figure = image.querySelector('[data-hosted-image="call_ig1"]') as HTMLElement;
    expect((figure.querySelector('img') as HTMLImageElement).getAttribute('src')).toBe('/api/media/tok_3f2a?w=1536&h=1024');
    within(figure).getByRole('link', { name: '3f2a.png' });
    within(figure).getByText('1.9 MB');
    // The image as made: model and size, then where it ran.
    within(figure).getByText('gpt-image-2 · 1536×1024');
    within(figure).getByText('relay-gpt');
    within(figure).getByText('Prompt');
    within(figure).getByText(fixture.direct_image.revised_prompt);
  });

  it('MH-HOSTED-UI-008: shows a total an unpriced attempt left only as a lower bound', () => {
    renderGroup(['proxy_retried'], 'zh');
    within(row('call_x6')).getByText('至少 $0.11');
    cleanup();
    renderGroup(['proxy_retried']);
    within(row('call_x6')).getByText('at least $0.11');
  });

  it.each([
    ['pending', undefined, 'Cut off'],
    ['failed', 'upstream timeout', 'Failed'],
    ['incomplete', 'response ended', 'Incomplete'],
  ] as const)('MH-HOSTED-UI-014 (%s): shows its status and known charge, collapsed or expanded, never how it ran or what it found', (status, error, label) => {
    renderGroup([{ ...fixture.native_web_search, status, error }]);
    const card = row('srvtoolu_01');
    within(card).getByText(label);
    within(card).getByText('≈ $0.01');
    expect(within(card).queryByText('Native · Anthropic')).toBeNull();
    expect(within(card).queryByText('2 sources')).toBeNull();

    // Expanding it must not bring the claims back.
    fireEvent.click(within(card).getByRole('button'));
    expect(card.textContent).not.toMatch(/Ran natively|took \d+ s/);
    expect(within(card).queryByRole('link')).toBeNull();
    within(card).getAllByText('≈ $0.01');
    if (error) within(card).getByText(error);
  });

  it('keeps the plain tool line for a row whose card this client cannot read', () => {
    renderGroup([{ ...fixture.native_web_search, version: 2 }]);
    expect(document.querySelector('[data-hosted-call]')).toBeNull();
    // The generic tool row, drawn from the row's text as for any other tool call.
    screen.getByRole('button', { name: /IM line/ });
  });

  it('MH-HOSTED-UI-015: says a reason Avibe worded in the reader\'s language, and any other as it was written', () => {
    renderGroup(['proxy_unreachable'], 'zh');
    const card = row('call_x5');
    fireEvent.click(within(card).getByRole('button'));
    within(card).getByText('无法连接模型网关。');
    expect(card.textContent).not.toContain('the gateway could not run the call');
    cleanup();

    // A code this client does not know yet reads as the record's own words.
    renderGroup([{ ...fixture.proxy_unreachable, error_code: 'gateway_overheated' }], 'zh');
    const unknown = row('call_x5');
    fireEvent.click(within(unknown).getByRole('button'));
    within(unknown).getByText('the gateway could not run the call (ConnectError)');
  });

  it('reads in Chinese with the same facts', () => {
    renderGroup(['proxy_x_search'], 'zh');
    const proxy = row('call_x1');
    within(proxy).getByText('由 xAI 代理 · grok-4.6');
    within(proxy).getByText('2 条帖子');
  });
});
