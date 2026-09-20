/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it } from 'vitest';

import en from '@/i18n/en.json';
import type { CitationSource } from '@/lib/citations';
import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import { Markdown } from './markdown';

afterEach(cleanup);

describe('Markdown emphasis', () => {
  it.each([
    ['', 'p'],
    ['1. ', 'ol > li'],
    ['- ', 'ul > li'],
    ['> ', 'blockquote > p'],
    ['### ', 'h3'],
  ])('renders CJK punctuation boundaries inside %j blocks', (prefix, selector) => {
    const bold = 'A 不再出现在后续请求里，线程 ID 和聊天记录不变。';
    const continuation = '源码允许冷恢复时优先采用传入的基础指令。';
    const { container } = render(
      <Markdown content={`${prefix}**${bold}**${continuation}`} />,
    );
    const block = container.querySelector(selector);

    expect(block?.querySelector('strong')?.textContent).toBe(bold);
    expect(block?.textContent).toBe(bold + continuation);
  });

  it.each([false, true])('keeps adjacent CJK emphasis with softBreaks=%s', (softBreaks) => {
    const { container } = render(
      <Markdown
        content={'前文**「重点」**后文**结论。**继续\n下一行'}
        softBreaks={softBreaks}
        interactive={false}
      />,
    );

    expect(Array.from(container.querySelectorAll('strong'), (node) => node.textContent))
      .toEqual(['「重点」', '结论。']);
    expect(container.querySelectorAll('br')).toHaveLength(softBreaks ? 1 : 0);
  });

  it('preserves inline structure inside CJK strong emphasis', () => {
    const { container } = render(
      <Markdown content={'1. **保留 `Codex` 和[链接](https://example.com)。**后文'} />,
    );
    const strong = container.querySelector('li > strong');

    expect(strong?.textContent).toBe('保留 Codex 和链接。');
    expect(strong?.querySelector('code')?.textContent).toBe('Codex');
    expect(strong?.querySelector('a')?.getAttribute('href')).toBe('https://example.com');
    expect(container.querySelector('li')?.textContent).toBe('保留 Codex 和链接。后文');
  });

  it.each([
    ['`**中文。**后文`', 'code', '**中文。**后文'],
    ['```md\n**中文。**后文\n```', 'pre > code', '**中文。**后文\n'],
    ['    **中文。**后文', 'pre > code', '**中文。**后文\n'],
    [String.raw`\*\*中文。\*\*后文`, 'p', '**中文。**后文'],
    ['&#42;&#42;中文。&#42;&#42;后文', 'p', '**中文。**后文'],
    ['**中文。', 'p', '**中文。'],
    ['**中文。 **后文', 'p', '**中文。 **后文'],
  ])('preserves literal markers in %j', (content, selector, expected) => {
    const { container } = render(<Markdown content={content} interactive={false} />);

    expect(container.querySelector('strong')).toBeNull();
    expect(container.querySelector(selector)?.textContent).toBe(expected);
  });

  it('preserves standard emphasis and GFM rendering', () => {
    const { container } = render(
      <Markdown content={'- [x] **Bold** and *italic* and ~~deleted~~\n\n| A | B |\n| - | - |\n| one | two |'} />,
    );

    expect(container.querySelector('strong')?.textContent).toBe('Bold');
    expect(container.querySelector('em')?.textContent).toBe('italic');
    expect(container.querySelector('del')?.textContent).toBe('deleted');
    expect(container.querySelector<HTMLInputElement>('input')?.checked).toBe(true);
    expect(container.querySelectorAll('td')).toHaveLength(2);
  });
});

// Source citations: the backend already rewrote each marker into an ordinary
// Markdown link, so every surface is readable without this renderer. What is
// asserted here is the upgrade — recognizing exactly those links and drawing
// them as compact numbered badges — and, just as importantly, what must NOT be
// upgraded: a link the agent wrote in its own prose, and a stored sidecar row
// that no longer describes a safe destination.
describe('Markdown source citations', () => {
  const i18n = createInstance();
  void i18n.use(initReactI18next).init({
    lng: 'en',
    fallbackLng: 'en',
    resources: { en: { translation: en } },
    interpolation: { escapeValue: false },
  });

  const GUIDE = 'https://developers.openai.com/api/docs/guides/tools-web-search';
  const PROBE = 'https://example.com/citation-probe-source';
  const guide = (over: Partial<CitationSource> = {}): CitationSource => ({
    index: 1,
    ref_id: 'turn0view0',
    title: 'Web search — OpenAI API',
    url: GUIDE,
    label: 'developers.openai.com',
    ...over,
  });
  const link = (citation: { label: string; url: string }) => `[${citation.label}](${citation.url})`;

  const renderMarkdown = (content: string, citations?: unknown[], interactive = true) => render(
    <I18nextProvider i18n={i18n}>
      <RouteSurfaceActiveContext.Provider value>
        <Markdown
          content={content}
          citations={citations as CitationSource[] | undefined}
          interactive={interactive}
        />
      </RouteSurfaceActiveContext.Provider>
    </I18nextProvider>,
  );

  const badges = (container: HTMLElement) =>
    Array.from(container.querySelectorAll<HTMLAnchorElement>('a[data-citation-index]'));

  it('renders a cited link as a numbered badge that is itself the link', () => {
    const citation = guide();
    const { container } = renderMarkdown(`Documented. ${link(citation)}`, [citation]);
    const [badge] = badges(container);

    expect(badge.textContent).toBe('1');
    expect(badge.getAttribute('href')).toBe(GUIDE);
    // Safe external-link behavior, and the full attribution on the accessible
    // name — so the preview panel is an affordance, not the only carrier.
    expect(badge.getAttribute('target')).toBe('_blank');
    expect(badge.getAttribute('rel')).toBe('noopener noreferrer nofollow');
    expect(badge.getAttribute('aria-label')).toBe('Source 1: Web search — OpenAI API');
    expect(container.textContent).toBe('Documented. 1');
  });

  it('keeps several citations compact and in the order the backend numbered them', () => {
    const citations = [
      guide(),
      guide({ index: 2, ref_id: 'turn0view1', title: '引用探针来源', url: PROBE, label: 'example.com' }),
    ];
    const { container } = renderMarkdown(
      `Two sources. ${link(citations[0])} ${link(citations[1])}`,
      citations,
    );

    expect(badges(container).map((badge) => badge.textContent)).toEqual(['1', '2']);
    expect(container.querySelectorAll('a')).toHaveLength(2);
  });

  it('leaves a link the agent worded itself as an ordinary anchor', () => {
    const citation = guide();
    const { container } = renderMarkdown(
      `See [the web search guide](${GUIDE}) and ${link(citation)}.`,
      [citation],
    );
    const anchors = Array.from(container.querySelectorAll('a'));

    expect(anchors.map((anchor) => anchor.textContent)).toEqual(['the web search guide', '1']);
    expect(anchors[0].hasAttribute('data-citation-index')).toBe(false);
  });

  it('collapses to plain text on a non-interactive surface', () => {
    const citation = guide();
    const { container } = renderMarkdown(`Documented. ${link(citation)}`, [citation], false);

    // A badge is an anchor; nested inside a clickable preview row it would be
    // invalid interactive content, so the link text stands in as prose.
    expect(container.querySelector('a')).toBeNull();
    expect(container.textContent).toBe('Documented. developers.openai.com');
  });

  it.each([
    ['an unsafe scheme', { url: 'javascript:alert(1)' }],
    ['a missing label', { label: undefined }],
    ['an unusable index', { index: Number.NaN }],
  ])('degrades a stored row with %s to the link the text already carries', (_case, over) => {
    const citation = guide();
    const { container } = renderMarkdown(`Documented. ${link(citation)}`, [{ ...citation, ...over }]);
    const anchor = container.querySelector('a');

    expect(badges(container)).toHaveLength(0);
    expect(anchor?.getAttribute('href')).toBe(GUIDE);
    expect(anchor?.textContent).toBe('developers.openai.com');
  });

  it('previews the source title and domain on hover, and offers the page explicitly', () => {
    const citation = guide();
    const { container } = renderMarkdown(`Documented. ${link(citation)}`, [citation]);

    fireEvent.pointerOver(badges(container)[0], { pointerType: 'mouse' });

    expect(screen.getByText('Web search — OpenAI API')).toBeTruthy();
    expect(screen.getByText('developers.openai.com')).toBeTruthy();
    expect(screen.getByText('Open source page').closest('a')?.getAttribute('href')).toBe(GUIDE);
  });

  it('reveals the same preview on keyboard focus without moving focus off the link', () => {
    const citation = guide();
    const { container } = renderMarkdown(`Documented. ${link(citation)}`, [citation]);
    const [badge] = badges(container);

    // A real focus() rather than a synthetic event: the point of this test is
    // that the element the browser focuses is the element that opens the page.
    act(() => badge.focus());

    expect(screen.getByText('Web search — OpenAI API')).toBeTruthy();
    // Focus stays on the badge, so Enter opens the page instead of tabbing into
    // a portalled panel first.
    expect(document.activeElement).toBe(badge);
  });

  it('lets the first tap reveal the preview and the next one follow the link', () => {
    const citation = guide();
    const { container } = renderMarkdown(`Documented. ${link(citation)}`, [citation]);
    const [badge] = badges(container);

    fireEvent.pointerDown(badge, { pointerType: 'touch' });

    expect(fireEvent.click(badge)).toBe(false);
    expect(screen.getByText('Web search — OpenAI API')).toBeTruthy();

    fireEvent.pointerDown(badge, { pointerType: 'touch' });

    expect(fireEvent.click(badge)).toBe(true);
  });

  it('attributes a source whose search returned no title by its domain', () => {
    const citation = guide({ title: '' });
    const { container } = renderMarkdown(`Documented. ${link(citation)}`, [citation]);

    fireEvent.pointerOver(badges(container)[0], { pointerType: 'mouse' });

    expect(badges(container)[0].getAttribute('aria-label')).toBe('Source 1: developers.openai.com');
    expect(screen.getAllByText('developers.openai.com').length).toBeGreaterThan(0);
  });
});
