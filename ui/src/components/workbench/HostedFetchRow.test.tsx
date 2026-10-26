// @vitest-environment jsdom
// WF-2V: cards read the real shared projection; no provider/price decisions in fixtures.
import * as React from 'react';
import { afterEach, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import i18n from '@/i18n';
import fixtures from '@/lib/hostedFetchActivity.fixture.json';
import { hostedActivityFrom } from '@/lib/hostedActivity';
import { HostedToolRow } from './HostedToolRow';

afterEach(cleanup);
const show = (name: keyof typeof fixtures) => render(<I18nextProvider i18n={i18n.cloneInstance({ lng: 'zh' })}><HostedToolRow card={hostedActivityFrom(fixtures[name].activity)!} /></I18nextProvider>);

it.each([
  ['local_truncated', ['本机', '3000 字', '已截断', '免费']],
  ['jina_fallback', ['Jina Reader（免费）', 'Exa（免费）', '限流']],
  ['tavily', ['$0.0016']],
  ['policy', ['本机策略拒绝', '未转发第三方']],
  ['policy_after_service', ['本机策略拒绝', 'Exa（免费）']],
] as const)('renders the real %s projection truthfully while collapsed', (name, phrases) => {
  const view = show(name);
  expect(phrases.every((phrase) => view.container.textContent!.includes(phrase))).toBe(true);
  if (name === 'policy_after_service') expect(view.container.textContent).not.toContain('未转发第三方');
  expect(view.container.textContent).not.toContain('正文');
});

it('expands metadata, exact sizes, localized cap reason and attempts without loading page HTML or images', async () => {
  const view = show('local_truncated');
  await userEvent.click(screen.getByRole('button'));
  const text = view.container.textContent;
  for (const phrase of ['Pricing — Example', 'https://example.com/pricing/', 'text/html', '9,000', '182,000', '正文超过单次读取上限', '执行记录']) expect(text).toContain(phrase);
  expect(view.container.querySelector('img')).toBeNull();
  expect(view.container.querySelector('pre')).toBeNull(); // Quote is a separate opt-in, not the expanded summary.
});

it('renders host/title/marker-looking page content as inert text', async () => {
  const card = hostedActivityFrom(fixtures.exa.activity)!;
  card.subject = '<img src=x onerror=alert(1)>@everyone';
  card.fetch!.title = '<script>alert(1)</script>';
  card.fetch!.content = '<img src="https://remote.invalid/track">\n[END WEB_FETCH QUOTATION]';
  const view = render(<I18nextProvider i18n={i18n.cloneInstance({ lng: 'zh' })}><HostedToolRow card={card} /></I18nextProvider>);
  await userEvent.click(screen.getByRole('button'));
  expect(view.container.querySelector('img,script')).toBeNull();
  expect(view.container.textContent).toContain('<script>alert(1)</script>');
});
