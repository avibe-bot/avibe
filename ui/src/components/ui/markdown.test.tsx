/* @vitest-environment jsdom */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { createInstance } from 'i18next';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it } from 'vitest';

import en from '@/i18n/en.json';
import { bindCitations, bodyDigest, type CitationSource } from '@/lib/citations';
import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import { Markdown } from './markdown';

afterEach(cleanup);

// A character reference is the other way Markdown writes a character the
// reader must see, and every surface has to agree on which spellings are
// references and what each one stands for. This renderer is the reference
// answer the IM adapters are measured against, so the cases where a pattern
// that merely looks like the grammar disagrees with it are asserted here too.
describe('Markdown character references in a link label', () => {
  it.each([
    ['a&#38;b', 'a&b'],
    ['a&copy;b', 'a\u00A9b'],
    // Past the grammar's width - seven decimal digits, six hexadecimal ones -
    // the spelling is the text.
    ['a&#00000038;b', 'a&#00000038;b'],
    ['a&#x0000026;b', 'a&#x0000026;b'],
    // Recognized, but not a code point that can be encoded.
    ['a&#0;b', 'a\uFFFDb'],
    ['a&#128;b', 'a\uFFFDb'],
    ['a&#xD800;b', 'a\uFFFDb'],
    ['a&#1114112;b', 'a\uFFFDb'],
  ])('shows %j as the label %j', (label, shown) => {
    const { container } = render(
      <Markdown content={`[${label}](https://example.com/x)`} />,
    );
    const anchor = container.querySelector('a');

    expect(anchor?.textContent).toBe(shown);
    expect(anchor?.getAttribute('href')).toBe('https://example.com/x');
  });
});

// The address half of the same unit. This renderer resolves a destination the
// way a Markdown reader does - escapes, character references and all - and
// then writes it as an href, so it is the independent answer for what address
// each of these links names. Two things are checked, and they are not the same
// check: the rows below pin the exact spelling this renderer writes, while the
// shared matrix further down compares the destination the Slack adapter
// delivers, decoded out of its wrapper, against the address an actual anchor
// here navigates to. Destinations that differ on the wire are not thereby the
// same address - only that measured navigation comparison says where each one
// arrives, and nothing here speaks for what a target server would accept.
describe('Markdown link destinations', () => {
  it.each([
    // A character the Slack wrapper is built out of, however Markdown spells it.
    [String.raw`[h](https://example.com/a>b)`, 'https://example.com/a%3Eb'],
    [String.raw`[h](https://example.com/a\>b)`, 'https://example.com/a%3Eb'],
    ['[h](https://example.com/a&gt;b)', 'https://example.com/a%3Eb'],
    ['[h](https://example.com/a&#62;b)', 'https://example.com/a%3Eb'],
    [String.raw`[h](https://example.com/a<b)`, 'https://example.com/a%3Cb'],
    [String.raw`[h](https://example.com/a\<b)`, 'https://example.com/a%3Cb'],
    ['[h](https://example.com/a&lt;b)', 'https://example.com/a%3Cb'],
    [String.raw`[h](https://example.com/a|b)`, 'https://example.com/a%7Cb'],
    [String.raw`[h](https://example.com/a\|b)`, 'https://example.com/a%7Cb'],
    // A query separator is structure, and stays one.
    ['[h](https://example.com/s?a=1&b=2)', 'https://example.com/s?a=1&b=2'],
    ['[h](https://example.com/a&amp;b)', 'https://example.com/a&b'],
    // A reference that spells the text of a reference, and the escape that
    // spells the same five characters.
    ['[h](https://example.com/a&amp;amp;b)', 'https://example.com/a&amp;b'],
    [String.raw`[h](https://example.com/a\&amp;b)`, 'https://example.com/a&amp;b'],
    // A reference can put a control character or a space in an address.
    ['[h](https://example.com/p&#10;q)', 'https://example.com/p%0Aq'],
    ['[h](https://example.com/p&#9;q)', 'https://example.com/p%09q'],
    ['[h](<https://example.com/a b>)', 'https://example.com/a%20b'],
    // An escape already spelled stays spelled once.
    ['[h](https://example.com/a%3Eb)', 'https://example.com/a%3Eb'],
    ['[h](https://example.com/a%26amp%3Bb)', 'https://example.com/a%26amp%3Bb'],
    // Structure a URI is made of, including a bracketed IPv6 authority.
    [String.raw`[h](https://[::1]:8443/a\>b)`, 'https://[::1]:8443/a%3Eb'],
    ['[h](https://u:p@[::1]:8443/a?x=1&y=2)', 'https://u:p@[::1]:8443/a?x=1&y=2'],
    ['[h](https://example.com/p?x=1#f%20g)', 'https://example.com/p?x=1#f%20g'],
    [String.raw`[h](https://example.com/a\\b)`, 'https://example.com/a%5Cb'],
    ['[h](https://example.com/q=`x`)', 'https://example.com/q=%60x%60'],
    // A scheme that is not http.
    [
      String.raw`[h](mailto:a+b@example.com?subject=a\>b)`,
      'mailto:a+b@example.com?subject=a%3Eb',
    ],
    // A `%` that starts no escape is data and is spelled; one that starts an
    // escape already spelled is left alone, malformed-looking or not.
    ['[h](https://example.com/a%b)', 'https://example.com/a%25b'],
    ['[h](https://example.com/a%zzb)', 'https://example.com/a%zzb'],
    // Brackets are the host's syntax in an authority and data anywhere else,
    // and a bracketed host this platform rejects is left spelled rather than
    // repaired into an address the source never wrote.
    ['[h](https://example.com/a[b])', 'https://example.com/a%5Bb%5D'],
    [
      '[h](https://u:p@[::1]:8443/a[b]?x=[c]#f[d])',
      'https://u:p@[::1]:8443/a%5Bb%5D?x=%5Bc%5D#f%5Bd%5D',
    ],
    ['[h](https://[nope]/x)', 'https://%5Bnope%5D/x'],
    [
      '[h](https://例子.测试/路径?q=a&b=c)',
      'https://%E4%BE%8B%E5%AD%90.%E6%B5%8B%E8%AF%95/%E8%B7%AF%E5%BE%84?q=a&b=c',
    ],
  ])('opens %j at %j', (markdown, href) => {
    const { container } = render(<Markdown content={markdown} />);
    const anchor = container.querySelector('a');

    expect(anchor?.getAttribute('href')).toBe(href);
    expect(anchor?.textContent).toBe('h');
  });

  // One shared table with tests/test_link_unit_delivery.py, read at runtime so
  // the fixture stays outside Vite's module graph. That suite asserts what the
  // real Slack adapter decodes out of its wrapper for each `markdown`; this one
  // renders the same source through the real component and asks the platform's
  // own URL parser whether the anchor it produced and that address are one URL.
  // Neither suite can drift without the other failing, and neither one claims
  // the two wires are equal - only that they arrive in the same place.
  type DestinationCase = { markdown: string; address: string };
  const destinationMatrix = (JSON.parse(
    readFileSync(resolve(process.cwd(), '../tests/fixtures/link_destination_matrix.json'), 'utf8'),
  ) as { cases: DestinationCase[] }).cases;

  it.each(destinationMatrix)(
    'resolves $markdown to the address the Slack adapter delivers',
    ({ markdown, address }) => {
      const { container } = render(<Markdown content={markdown} />);
      const href = container.querySelector('a')?.getAttribute('href');

      expect(href).toBeDefined();
      expect(new URL(href as string).href).toBe(new URL(address).href);
    },
  );

  it('keeps a bare destination and two neighbouring links whole', () => {
    const empty = render(<Markdown content={String.raw`[](https://example.com/a\>b)`} />);

    expect(empty.container.querySelector('a')?.getAttribute('href'))
      .toBe('https://example.com/a%3Eb');
    expect(empty.container.querySelector('a')?.textContent).toBe('');

    const pair = render(
      <Markdown content={String.raw`[a](https://example.com/1\>x) [b](https://example.com/2|y)`} />,
    );

    expect([...pair.container.querySelectorAll('a')].map((a) => a.getAttribute('href')))
      .toEqual(['https://example.com/1%3Ex', 'https://example.com/2%7Cy']);
  });

  // The brackets half of the same boundary, and the third shared table. A
  // bracket is an IPv6 host's syntax in the authority and data everywhere
  // else, and `normalizeUri` writes both as `%5B`/`%5D` - so by the time an
  // href exists, `https://[::1]/admin` and `https://%5B::1%5D/admin` are one
  // string, and restoring brackets to whatever looks like an authority hands
  // the second one a live loopback address its author never wrote. The
  // distinction is taken from the parsed destination, before that spelling,
  // and carried to the href.
  //
  // Two facts per row, and they are not the same fact: `href` is the exact
  // attribute this component writes, and `navigates` is what a browser's URL
  // parser resolves from it - `null` where it refuses to. The refusals are
  // asserted rather than skipped: `new URL('https://%5B::1%5D/admin')` throws,
  // and leaving those rows out because the assertion is awkward is exactly how
  // an invented address goes unnoticed. The citation and Slack suites assert
  // their own columns of this table; see the fixture's description.
  type AuthorityCase = {
    why: string;
    markdown: string;
    href: string;
    navigates: string | null;
  };
  const authorityMatrix = JSON.parse(
    readFileSync(resolve(process.cwd(), '../tests/fixtures/citation_authority_matrix.json'), 'utf8'),
  ) as {
    cases: AuthorityCase[];
    per_occurrence: {
      markdown: string;
      links: { href: string; label: string }[];
    };
  };

  it.each(authorityMatrix.cases)('keeps only a real authority\'s brackets: $why', ({
    markdown,
    href,
    navigates,
  }) => {
    const { container } = render(<Markdown content={markdown} />);
    const anchors = [...container.querySelectorAll('a')];

    expect(anchors).toHaveLength(1);
    expect(anchors[0].getAttribute('href')).toBe(href);

    if (navigates === null) {
      expect(() => new URL(href)).toThrow();
    } else {
      expect(new URL(href).href).toBe(navigates);
    }
  });

  it('keeps two destinations that spell alike apart', () => {
    // Both hrefs are `https://%5B::1%5D/admin` if the answer is looked up by
    // the finished text rather than kept per occurrence.
    const { markdown, links } = authorityMatrix.per_occurrence;
    const { container } = render(<Markdown content={markdown} />);

    expect([...container.querySelectorAll('a')].map((a) => ({
      href: a.getAttribute('href'),
      label: a.textContent,
    }))).toEqual(links);
  });
});

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
  // And what its _link_spelling writes: the URL keeps its parentheses, and the
  // destination escapes them so an unbalanced one cannot end the link early.
  const link = (citation: { label: string; url: string }) =>
    `[${escapeLabel(citation.label)}](${citation.url.replace(/[()]/g, '\\$&')})`;
  const guide = (over: Partial<CitationSource> = {}): CitationSource => ({
    index: 1,
    ref_id: 'turn0view0',
    title: 'Web search — OpenAI API',
    url: GUIDE,
    label: 'developers.openai.com',
    ...over,
  });

  /**
   * One reply, assembled the way the backend assembles one.
   *
   * The backend writes each citation's link itself, so it knows the exact range
   * it wrote it at and the exact body it ended up in. A test says the same
   * thing by building the body out of its parts: a `string` is prose the answer
   * wrote (including a link the answer worded itself), and a source is a link
   * the backend wrote, which contributes both its spelling and its range. Every
   * row then carries the digest of the finished body, because the ranges are
   * only meaningful in it.
   */
  const body = (...parts: Array<string | CitationSource>) => {
    let text = '';
    const measured = new Map<CitationSource, number[][]>();
    for (const part of parts) {
      if (typeof part === 'string') {
        text += part;
        continue;
      }
      const start = text.length;
      text += link(part);
      measured.set(part, [...(measured.get(part) ?? []), [start, text.length]]);
    }
    const body_sha256 = bodyDigest(text);
    const citations = [...measured].map(([source, spans]) => ({ ...source, spans, body_sha256 }));
    return { text, citations };
  };

  const renderMarkdown = (content: string, citations?: unknown[], interactive = true) => render(
    <I18nextProvider i18n={i18n}>
      <RouteSurfaceActiveContext.Provider value>
        <Markdown
          content={content}
          citations={bindCitations(citations, content)}
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
    const { text, citations } = body('Cited. ', guide({ url, label }));
    const [badge] = badges(renderMarkdown(text, citations).container);

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
    const { text, citations } = body('Documented. ', guide());
    const { container } = renderMarkdown(text, citations);
    const [badge] = badges(container);

    expect(badge.textContent).toBe('1');
    expect(badge.getAttribute('href')).toBe(GUIDE);
    // Safe external-link behavior, and the full attribution on the accessible
    // name — the source's own title and the domain the link opens — so the
    // preview panel is an affordance, not the only carrier.
    expect(badge.getAttribute('target')).toBe('_blank');
    expect(badge.getAttribute('rel')).toBe('noopener noreferrer nofollow');
    expect(badge.getAttribute('aria-label')).toBe(
      'Source 1: Web search — OpenAI API (developers.openai.com)',
    );
    expect(container.textContent).toBe('Documented. 1');
  });

  it('keeps several citations compact and in the order the backend numbered them', () => {
    const second = guide({
      index: 2, ref_id: 'turn0view1', title: '引用探针来源', url: PROBE, label: 'example.com',
    });
    const { text, citations } = body('Two sources. ', guide(), ' ', second);
    const { container } = renderMarkdown(text, citations);

    expect(badges(container).map((badge) => badge.textContent)).toEqual(['1', '2']);
    expect(container.querySelectorAll('a')).toHaveLength(2);
  });

  it('leaves a link the agent worded itself as an ordinary anchor', () => {
    // Same page, different characters, and in a range the backend never claimed.
    const { text, citations } = body(`See [the web search guide](${GUIDE}) and `, guide(), '.');
    const { container } = renderMarkdown(text, citations);
    const anchors = Array.from(container.querySelectorAll('a'));

    expect(anchors.map((anchor) => anchor.textContent)).toEqual(['the web search guide', '1']);
    expect(anchors[0].hasAttribute('data-citation-index')).toBe(false);
  });

  it('collapses to plain text on a non-interactive surface', () => {
    const { text, citations } = body('Documented. ', guide());
    const { container } = renderMarkdown(text, citations, false);

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
    const { text, citations } = body('Documented. ', guide());
    const { container } = renderMarkdown(text, citations.map((row) => ({ ...row, ...over })));
    const anchor = container.querySelector('a');

    expect(badges(container)).toHaveLength(0);
    expect(anchor?.getAttribute('href')).toBe(GUIDE);
    expect(anchor?.textContent).toBe('developers.openai.com');
  });

  it('previews the source title and domain on hover, and offers the page explicitly', () => {
    const { text, citations } = body('Documented. ', guide());
    const { container } = renderMarkdown(text, citations);

    fireEvent.pointerOver(badges(container)[0], { pointerType: 'mouse' });

    expect(screen.getByText('Web search — OpenAI API')).toBeTruthy();
    expect(screen.getByText('developers.openai.com')).toBeTruthy();
    expect(screen.getByText('Open source page').closest('a')?.getAttribute('href')).toBe(GUIDE);
  });

  it('reveals the same preview on keyboard focus without moving focus off the link', () => {
    const { text, citations } = body('Documented. ', guide());
    const { container } = renderMarkdown(text, citations);
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
    const { text, citations } = body('Documented. ', guide());
    const { container } = renderMarkdown(text, citations);
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
  // since the prose link is the one nobody vouched for.
  //
  // So the row carries the ranges the backend wrote its links at, in the body
  // it wrote them into - named by its digest, because a range means nothing
  // without the version of the text it was counted in. A badge is drawn only on
  // a link whose parsed position IS one of those ranges, exactly.
  describe('provenance', () => {
    it('leaves a prose link alone even when it is word-for-word the citation', () => {
      const citation = guide();
      const { text, citations } = body('See ', link(citation), ' first. ', citation);
      const { container } = renderMarkdown(text, citations);
      const anchors = Array.from(container.querySelectorAll('a'));

      expect(anchors).toHaveLength(2);
      expect(anchors[0].hasAttribute('data-citation-index')).toBe(false);
      expect(anchors[0].textContent).toBe('developers.openai.com');
      expect(anchors[1].getAttribute('data-citation-index')).toBe('1');
    });

    it('draws a badge on each link one source wrote', () => {
      const citation = guide();
      const { text, citations } = body('One. ', citation, ' Two. ', citation);
      const { container } = renderMarkdown(text, citations);

      expect(citations[0].spans).toHaveLength(2);
      expect(badges(container).map((badge) => badge.textContent)).toEqual(['1', '1']);
    });

    it('locates each source independently, so one between two others shifts none', () => {
      const first = guide();
      const second = guide({
        index: 2, ref_id: 'turn0view1', title: '引用探针来源', url: PROBE, label: 'example.com',
      });
      const { text, citations } = body('A. ', first, ' B. ', second, ' C. ', first);
      const { container } = renderMarkdown(text, citations);

      expect(badges(container).map((badge) => badge.textContent)).toEqual(['1', '2', '1']);
    });

    it('degrades to the plain link when the body is not the one that was measured', () => {
      // The ranges still land on real links here - the edit is at the end. What
      // says they are no longer this body's ranges is the digest, and the honest
      // answer is the link the reader already has rather than a badge on
      // whichever link happens to sit in that position now.
      const { text, citations } = body('Documented. ', guide());
      const { container } = renderMarkdown(`${text} Edited.`, citations);

      expect(badges(container)).toHaveLength(0);
      expect(container.querySelector('a')?.getAttribute('href')).toBe(GUIDE);
    });

    it('draws nothing on a link a range covers only part of', () => {
      // A range is the WHOLE `[label](url)` the backend wrote. One that starts a
      // character early describes something that is not a link at all, so it
      // matches no link - a near miss is not a match.
      const { text, citations } = body('Documented. ', guide());
      const [[start, end]] = citations[0].spans;
      const { container } = renderMarkdown(
        text,
        [{ ...citations[0], spans: [[start - 1, end]] }],
      );

      expect(badges(container)).toHaveLength(0);
      expect(container.querySelector('a')?.getAttribute('href')).toBe(GUIDE);
    });

    it.each([
      ['an image', `![alt](${'https://developers.openai.com/api/docs/guides/tools-web-search'})`],
      ['an autolink', `<${'https://developers.openai.com/api/docs/guides/tools-web-search'}>`],
      ['a reference link', '[dup][k]'],
    ])('does not badge %s, which spells the destination some other way', (_why, decoy) => {
      // Same page, different characters, and none of them written by the
      // backend. An image is not a link, and an autolink and a reference link
      // are links whose position is their own - never a range the backend
      // measured, because the backend wrote an inline link.
      const { text, citations } = body(`${decoy} Shown. `, guide(), `\n\n[k]: ${GUIDE}`);
      const { container } = renderMarkdown(text, citations);

      expect(badges(container).map((badge) => badge.textContent)).toEqual(['1']);
    });

    it.each([
      ['a code span', (l: string) => `\`${l}\``],
      ['a fenced block', (l: string) => `\`\`\`\n${l}\n\`\`\``],
      ['a footnote definition', (l: string) => `[^f]: ${l}`],
      ['a table cell', (l: string) => `| a |\n| - |\n| ${l} |`],
      ['an image alt', (l: string) => `!${l}`],
    ])('is unmoved by the same exact spelling inside %s', (_why, wrap) => {
      // This is the case that used to break: the two sides disagreed about
      // whether a footnote definition holds a link, their counts diverged, and
      // the real citation silently lost its badge. Neither side counts anything
      // now - the backend says where it wrote, and the parser says where this
      // link is.
      const citation = guide();
      const { text, citations } = body(`${wrap(link(citation))}\n\nShown. `, citation);
      const badged = badges(renderMarkdown(text, citations).container);

      expect(badged.map((badge) => badge.textContent)).toEqual(['1']);
      expect(badged[0].getAttribute('href')).toBe(GUIDE);
    });

    it('still recognizes a row persisted before provenance existed', () => {
      // History is a shipped surface: a message stored by an earlier release
      // carries no ranges at all, so it keeps the match it was written for -
      // the exact destination and link text.
      const legacy = guide();
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
      const { text, citations } = body('Documented. ', guide({ url, label }));
      const { container } = renderMarkdown(text, citations);
      const plain = renderMarkdown(text);
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
      const { text, citations } = body(
        'Documented. ', guide({ label: 'ex`ample.com' }), ' and `code` prose.',
      );
      const { container } = renderMarkdown(text, citations);

      expect(badges(container).map((badge) => badge.textContent)).toEqual(['1']);
      expect(container.querySelector('code')?.textContent).toBe('code');
    });
  });

  it('attributes a source whose search returned no title by its domain', () => {
    const { text, citations } = body('Documented. ', guide({ title: '' }));
    const { container } = renderMarkdown(text, citations);

    fireEvent.pointerOver(badges(container)[0], { pointerType: 'mouse' });

    expect(badges(container)[0].getAttribute('aria-label')).toBe('Source 1: developers.openai.com');
    expect(screen.getAllByText('developers.openai.com').length).toBeGreaterThan(0);
  });
});
