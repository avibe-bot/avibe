/* @vitest-environment jsdom */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

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
  // What core/citations.py's _escape_label writes: every character a Markdown
  // reader - or an IM dialect - would not show verbatim.
  const escapeLabel = (label: string) => label.replace(/([\\[\]`<*_~&|])/g, '\\$1');
  const link = (citation: { label: string; url: string }) =>
    `[${escapeLabel(citation.label)}](${citation.url})`;
  // A row the backend writes today: the complete link it wrote, and the one
  // occurrence of that exact spelling in the message that is its own. A test
  // that renders the same link twice says so by overriding both numbers, the
  // same way the backend would.
  const guide = (over: Partial<CitationSource> = {}): CitationSource => {
    const row: CitationSource = {
      index: 1,
      ref_id: 'turn0view0',
      title: 'Web search — OpenAI API',
      url: GUIDE,
      label: 'developers.openai.com',
      occurrences: [1],
      occurrence_total: 1,
      ...over,
    };
    return { ...row, spelling: over.spelling ?? link(row) };
  };

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

  // One shared table with tests/test_citations.py: the backend writes `url`,
  // and this asserts the real renderer hands back exactly that href, so a badge
  // cannot quietly stop matching the link it describes. Read at runtime rather
  // than imported so the fixture stays outside Vite's module graph.
  type IdentityCase = { why: string; raw: string; url: string | null; label: string | null };
  const fixture = JSON.parse(
    readFileSync(resolve(process.cwd(), '../tests/fixtures/citation_url_identity.json'), 'utf8'),
  ) as { cases: IdentityCase[]; authority: IdentityCase[] };
  const urlIdentity = [...fixture.cases, ...fixture.authority]
    .filter((entry): entry is { why: string; raw: string; url: string; label: string } =>
      Boolean(entry.url));

  it.each(urlIdentity)('keeps a canonical citation URL identical through the renderer ($why)', ({ url, label }) => {
    const citation = guide({ url, label });
    const { container } = renderMarkdown(`Cited. ${link(citation)}`, [citation]);
    const [badge] = badges(container);

    expect(badge).toBeDefined();
    expect(badge.getAttribute('href')).toBe(url);
  });

  it.each(urlIdentity)('labels a citation with the host a browser resolves ($why)', ({ url, label }) => {
    // Attribution names the host the browser reaches - punycode included, and
    // an IPv4 form serialized the way the URL parser serializes it - not the
    // bytes the URL spells. A host too long for a badge is elided from the
    // LEFT and keeps the host's tail verbatim, so the shown text is exactly the
    // last 63 characters: a label cut from the right would read as a domain the
    // link never opens, and keeping whole labels only left `…co.uk`, a public
    // suffix shared by every site a long label could hide behind.
    // `hostname`, not `host`: a port belongs to the URL and never to the
    // attribution, so a label beside `:8080` still names the site.
    const host = new URL(url).hostname;
    const elided = label.startsWith('…');
    const shown = elided ? label.slice(1) : label;

    expect(label.length).toBeLessThanOrEqual(64);
    if (elided) {
      expect(shown).toBe(host.slice(-63));
      expect(label.length).toBe(64);
    } else {
      expect(host === shown || host === `www.${shown}`).toBe(true);
    }
  });

  it.each([
    // Measured rather than assumed: micromark - the parser behind this renderer
    // - replaces the whole C1 range with U+FFFD, so `&#x80;` is NOT HTML's
    // Windows-1252 `€`.
    ['&#x80;', '%EF%BF%BD'],
    // The character a reference spells is part of the destination, so micromark
    // percent-encodes it. Only a LITERAL tab or newline is removed - and that
    // one never reaches here, because it ends the destination and leaves no
    // link at all. The backend cleans literals off first and resolves
    // references after, which is the only order that keeps these two hrefs.
    ['&Tab;', '%09'],
    ['&#x9;', '%09'],
    ['&#xA;', '%0A'],
    ['&#xD;', '%0D'],
  ])('resolves a %s reference in a destination the way the backend does', (reference, encoded) => {
    // The backend never writes a live character reference into a destination -
    // it percent-encodes the character, or escapes the `&` that would start one
    // - so this is the boundary being held rather than a shape to recognize:
    // both sides must agree on what the destination says.
    const { container } = renderMarkdown(
      `Cited. [developers.openai.com](https://example.com/p${reference}q)`,
    );

    expect(container.querySelector('a')?.getAttribute('href'))
      .toBe(`https://example.com/p${encoded}q`);
  });

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
    // Same page, different characters, so it is not an occurrence of this
    // citation's link at all - the backend numbers the citation 1 of 1.
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

  // A badge is an attribution claim, so it belongs to a link the backend wrote.
  // Recognizing one used to mean matching the rendered label back to a sidecar
  // row, which cannot tell that link apart from one the answer's own prose
  // wrote to the same page - and got it wrong in the direction that matters,
  // since the prose link is the one nobody vouched for. So the row carries
  // the complete link it wrote plus which of that spelling's literal
  // occurrences are its own, and the renderer counts the same characters in the
  // same delivered text.
  describe('provenance', () => {
    it('leaves a prose link alone even when it is word-for-word the citation', () => {
      const citation = guide({ occurrences: [2], occurrence_total: 2 });
      const { container } = renderMarkdown(
        `See ${link(citation)} first. ${link(citation)}`,
        [citation],
      );
      const anchors = Array.from(container.querySelectorAll('a'));

      expect(anchors).toHaveLength(2);
      expect(anchors[0].hasAttribute('data-citation-index')).toBe(false);
      expect(anchors[0].textContent).toBe('developers.openai.com');
      expect(anchors[1].getAttribute('data-citation-index')).toBe('1');
    });

    it('draws a badge on each link one source wrote', () => {
      const citation = guide({ occurrences: [1, 2], occurrence_total: 2 });
      const { container } = renderMarkdown(
        `One. ${link(citation)} Two. ${link(citation)}`,
        [citation],
      );

      expect(badges(container).map((badge) => badge.textContent)).toEqual(['1', '1']);
    });

    it('counts ordinals per spelling, so another citation between shifts neither', () => {
      const citations = [
        guide({ occurrences: [1, 2], occurrence_total: 2 }),
        guide({ index: 2, ref_id: 'turn0view1', title: '引用探针来源', url: PROBE, label: 'example.com' }),
      ];
      const { container } = renderMarkdown(
        `A. ${link(citations[0])} B. ${link(citations[1])} C. ${link(citations[0])}`,
        citations,
      );

      expect(badges(container).map((badge) => badge.textContent)).toEqual(['1', '2', '1']);
    });

    it('degrades to the plain link when the two sides counted different text', () => {
      // A total this render disagrees with means the row describes text that is
      // not what is on screen. The honest answer is the link the reader already
      // has, not a badge on whichever link landed in that position.
      const citation = guide({ occurrences: [1], occurrence_total: 3 });
      const { container } = renderMarkdown(`Documented. ${link(citation)}`, [citation]);

      expect(badges(container)).toHaveLength(0);
      expect(container.querySelector('a')?.getAttribute('href')).toBe(GUIDE);
    });

    it.each([
      ['an image', `![alt](${'https://developers.openai.com/api/docs/guides/tools-web-search'})`],
      ['an autolink', `<${'https://developers.openai.com/api/docs/guides/tools-web-search'}>`],
      ['a reference link', '[dup][k]'],
    ])('does not count %s, which spells the destination some other way', (_why, decoy) => {
      // Same page, different characters. Only the exact spelling is counted,
      // so none of these moves the citation's position - and the backend,
      // scanning the same text, reaches the same number.
      const citation = guide();
      const { container } = renderMarkdown(
        `${decoy} Shown. ${link(citation)}\n\n[k]: ${GUIDE}`,
        [citation],
      );

      expect(badges(container).map((badge) => badge.textContent)).toEqual(['1']);
    });

    it.each([
      ['a code span', (l: string) => `\`${l}\``],
      ['a fenced block', (l: string) => `\`\`\`\n${l}\n\`\`\``],
      ['a footnote definition', (l: string) => `[^f]: ${l}`],
      ['a table cell', (l: string) => `| a |\n| - |\n| ${l} |`],
      ['an image alt', (l: string) => `!${l}`],
    ])('counts the same exact link inside %s, because the backend does too', (_why, wrap) => {
      // This is the case that used to break: the two sides disagreed about
      // whether a footnote definition holds a link, the totals diverged, and
      // the real citation silently lost its badge. Neither side has an opinion
      // now - both count characters.
      const citation = guide({ occurrences: [2], occurrence_total: 2 });
      const { container } = renderMarkdown(
        `${wrap(link(citation))}\n\nShown. ${link(citation)}`,
        [citation],
      );
      const badged = badges(container);

      expect(badged.map((badge) => badge.textContent)).toEqual(['1']);
      expect(badged[0].getAttribute('href')).toBe(GUIDE);
    });

    it('still recognizes a row persisted before provenance existed', () => {
      // History is a shipped surface: a message stored by an earlier release
      // has no occurrences to compare, so it keeps the match it was written
      // for - the exact destination and link text.
      const { occurrences, occurrence_total, spelling, ...legacy } = guide();
      void occurrences;
      void occurrence_total;
      void spelling;
      const { container } = renderMarkdown(`Documented. ${link(legacy)}`, [legacy]);

      expect(badges(container).map((badge) => badge.textContent)).toEqual(['1']);
    });

    it.each([
      ['a bare literal', 'https://[2001:db8::1]/x', '[2001:db8::1]'],
      ['a port', 'https://[2001:db8::1]:8443/x', '[2001:db8::1]'],
      ['userinfo in front of it', 'https://attacker.example@[::1]/x', '[::1]'],
    ])('reaches an IPv6 source with %s, badge or not', (_why, url, label) => {
      // An IPv6 host is REQUIRED to be bracketed, and `[`/`]` are two of the
      // characters mdast-util-to-hast percent-encodes into an href: the link
      // rendered, looked right, and went nowhere. A badge is a presentation
      // upgrade, so the ORDINARY link has to reach the page on its own and the
      // badge has to agree with it - same address, whether or not a sidecar
      // ever arrives.
      const citation = guide({ url, label });
      const { container } = renderMarkdown(`Documented. ${link(citation)}`, [citation]);
      const plain = renderMarkdown(`Documented. ${link(citation)}`);
      const badged = badges(container)[0].getAttribute('href') as string;
      const ordinary = plain.container.querySelector('a')?.getAttribute('href') as string;

      expect(badged).toBe(url);
      expect(ordinary).toBe(url);
      expect(new URL(ordinary).hostname).toBe(label);
      expect(new URL(badged).href).toBe(new URL(ordinary).href);
    });

    it('recognizes a citation whose label had to be escaped, and keeps the code after it', () => {
      // The escape is what stops the backtick opening a code span that runs
      // past `](url)` to the next one and takes the link with it.
      const citation = guide({ label: 'ex`ample.com' });
      const { container } = renderMarkdown(
        `Documented. ${link(citation)} and \`code\` prose.`,
        [citation],
      );

      expect(badges(container).map((badge) => badge.textContent)).toEqual(['1']);
      expect(container.querySelector('code')?.textContent).toBe('code');
    });
  });

  it('attributes a source whose search returned no title by its domain', () => {
    const citation = guide({ title: '' });
    const { container } = renderMarkdown(`Documented. ${link(citation)}`, [citation]);

    fireEvent.pointerOver(badges(container)[0], { pointerType: 'mouse' });

    expect(badges(container)[0].getAttribute('aria-label')).toBe('Source 1: developers.openai.com');
    expect(screen.getAllByText('developers.openai.com').length).toBeGreaterThan(0);
  });
});
