/* @vitest-environment jsdom */

// The Web half of one shared recording.
//
// `tests/citation_bridge.py` runs the real Python producer and delivery pass
// over every case and writes what they actually emitted - the body the
// workbench is handed, the sidecar beside it, and the links a reader of that
// body must end up with. `tests/test_citation_consumers.py` asks each IM
// platform's renderer what it shows of that. This file asks the real `Markdown`
// component the same question, so the two ends of the contract are measured
// against one producer run instead of against each other's assumptions.
//
// Every assertion here is about what the reader gets: the label shown, the
// address the anchor navigates to, and which link carries the badge. A citation
// that lands on the wrong link is a false attribution, and a badge that is the
// only thing able to reach the source means the plain link underneath it is
// broken - so the sidecar is also removed, mismatched and downgraded to its
// pre-provenance shape, and the answer must stay readable and clickable in all
// four.

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { createInstance } from 'i18next';
import { cleanup, render } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it } from 'vitest';

import en from '@/i18n/en.json';
import { bindCitations, type CitationSource } from '@/lib/citations';
import type { MentionReference } from '@/lib/mentions';
import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import { Markdown } from './markdown';

afterEach(cleanup);

type Anchor = { label: string | null; url: string; cited: boolean };
type BridgeCase = {
  key: string;
  why: string;
  web_text: string;
  web_citations: CitationSource[];
  /** The same reply as IM delivers it, measured in that body. */
  im_citations: CitationSource[];
  web_anchors: Anchor[];
  anchors_exhaustive: boolean;
};

// Read at runtime rather than imported: the fixture is produced by the Python
// suite and lives outside Vite's module graph.
const { cases } = JSON.parse(
  readFileSync(resolve(process.cwd(), '../tests/fixtures/citation_consumer_bridge.json'), 'utf8'),
) as { cases: BridgeCase[] };

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

// Exactly what ChatPage does with a stored row: read the sidecar against the
// body it was measured in, and hand the renderer the result. Passing the raw
// rows and the text separately would let this file decide they match, which is
// the decision under test.
const renderCase = (content: string, citations?: CitationSource[]) => render(
  <I18nextProvider i18n={i18n}>
    <RouteSurfaceActiveContext.Provider value>
      <Markdown content={content} citations={bindCitations(citations, content)} interactive />
    </RouteSurfaceActiveContext.Provider>
  </I18nextProvider>,
);

// Only web links: a `file://` attachment is rewritten to a media-proxy URL
// before the workbench ever renders it, so the raw form recorded here is not a
// destination this component is meant to reach.
const webLinks = (container: HTMLElement) =>
  Array.from(container.querySelectorAll<HTMLAnchorElement>('a[href^="http"]'));

const badged = (anchor: HTMLAnchorElement) => anchor.hasAttribute('data-citation-index');

// A private marker is not an address. Where the model wrote one in a
// destination the producer leaves it exactly as typed - claiming a source
// there would be claiming one no reader can see - and every renderer past that
// point is free to spell those characters as percent escapes. So a rendered
// address is read back through that one spelling before it is compared, and
// through nothing else: a substituted host is still a link nobody wrote.
const asWritten = (href: string | null) => (href ?? '').replace(/(?:%EE%88%8[0-2])+/gi, decodeURIComponent);

// A label holding an image is not a string of text, and the producer says so by
// leaving it unsaid; that link is known by its address alone.
const shows = (links: HTMLAnchorElement[], anchor: Anchor) => links.some(
  (node) => asWritten(node.getAttribute('href')) === anchor.url
    && (anchor.label === null || node.textContent === anchor.label),
);

/** The characters a citation's first span covers, in the body it was measured in. */
const spelling = (row: BridgeCase, entry: CitationSource) => {
  const [start, end] = (entry.spans as number[][])[0];
  // A JavaScript string index is a UTF-16 code unit, which is the unit the
  // backend counted in.
  return row.web_text.slice(start, end);
};

describe('citation bridge: the real renderer, on one real producer run', () => {
  const cited = cases.map((row) => [row.key, row] as const);
  const withSidecar = cited.filter(([, row]) => row.web_citations.length > 0);

  it.each(cited)('%s shows every source the producer cited', (_key, row) => {
    const { container } = renderCase(row.web_text, row.web_citations);
    const links = webLinks(container);
    const expected = row.web_anchors.filter((anchor) => anchor.cited);

    // A badge is the citation's rendering, so it must carry the destination and
    // name the source - the label is shown in its preview, not in the strip of
    // text, which is the whole point of a compact badge.
    const badges = links.filter(badged);
    expect(badges).toHaveLength(expected.length);
    expect(badges.map((anchor) => asWritten(anchor.getAttribute('href')))).toEqual(
      expected.map((anchor) => anchor.url),
    );
    badges.forEach((anchor, at) => {
      const source = row.web_citations.find((entry) => entry.url === expected[at].url);
      const title = source?.title?.trim() || source?.label;
      expect(anchor.getAttribute('aria-label')).toBe(
        title === source?.label
          ? `Source ${source?.index}: ${title}`
          : `Source ${source?.index}: ${title} (${source?.label})`,
      );
    });
  });

  it.each(cited)('%s leaves every link the answer wrote itself ordinary', (_key, row) => {
    const { container } = renderCase(row.web_text, row.web_citations);
    const ordinary = webLinks(container).filter((anchor) => !badged(anchor));
    const expected = row.web_anchors.filter((anchor) => !anchor.cited);

    // The answer's own prose may point at the cited page with the very same
    // link. It is still the answer talking, not the search result, and it has
    // to read as what the agent wrote.
    for (const anchor of expected) {
      expect(shows(ordinary, anchor)).toBe(true);
    }
    if (row.anchors_exhaustive) expect(ordinary).toHaveLength(expected.length);
  });

  it.each(cited.filter(([, row]) => row.anchors_exhaustive))(
    '%s badges the occurrence the producer wrote and no other',
    (_key, row) => {
      const { container } = renderCase(row.web_text, row.web_citations);

      // Position, not just count: three identical links where the middle one is
      // the citation is exactly the case a count cannot tell apart.
      expect(webLinks(container).map((anchor) => ({
        url: asWritten(anchor.getAttribute('href')),
        cited: badged(anchor),
      }))).toEqual(row.web_anchors.map((anchor) => ({ url: anchor.url, cited: anchor.cited })));
    },
  );

  it.each(cited)('%s stays readable and clickable with no sidecar at all', (_key, row) => {
    const { container } = renderCase(row.web_text);
    const links = webLinks(container);

    // An older row, a failed load, a transcript from before the feature: the
    // badge is an enhancement, so its absence costs the reader nothing but the
    // preview. Every link still shows its label and still opens its page.
    expect(links.filter(badged)).toHaveLength(0);
    for (const anchor of row.web_anchors) {
      expect(shows(links, anchor)).toBe(true);
    }
  });

  it.each(withSidecar)(
    '%s refuses to guess when the sidecar no longer matches the text',
    (_key, row) => {
      // The stored text and the stored sidecar disagree - an edit after the row
      // was written, a truncation, a row written for another message. The spans
      // still land on real links here, which is exactly why the digest is what
      // decides: guessing is how an ordinary link gets impersonated.
      const edited = `${row.web_text}\n\nEdited.`;
      const { container } = renderCase(edited, row.web_citations);
      const links = webLinks(container);

      expect(links.filter(badged)).toHaveLength(0);
      for (const anchor of row.web_anchors) {
        expect(shows(links, anchor)).toBe(true);
      }
    },
  );

  it.each(withSidecar)('%s refuses a sidecar it cannot locate in this text', (_key, row) => {
    // Same body, but the measurement is damaged: a span past the end, or a row
    // that names the contract without stating it. One bad row drops the whole
    // set rather than leaving the rest to be matched some other way.
    for (const broken of [
      row.web_citations.map((entry) => ({ ...entry, spans: [[0, row.web_text.length + 1]] })),
      row.web_citations.map((entry) => ({ ...entry, spans: undefined })),
    ]) {
      const { container } = renderCase(row.web_text, broken as CitationSource[]);
      expect(webLinks(container).filter(badged)).toHaveLength(0);
      cleanup();
    }
  });

  it.each(withSidecar)(
    '%s still renders a row persisted before provenance existed',
    (_key, row) => {
      // Rows already on disk carry no measurement at all, and the match they
      // were written for is the exact destination and link text. It is less
      // precise - every identical link in the answer gets a badge - and it
      // keeps working.
      const legacy = row.web_citations.map(
        ({ spans: _spans, body_sha256: _digest, ...rest }) => rest,
      );
      const { container } = renderCase(row.web_text, legacy as CitationSource[]);
      const links = webLinks(container);
      const matches = row.web_anchors.filter((anchor) => legacy.some(
        (entry) => entry.url === anchor.url && entry.label === anchor.label,
      ));

      expect(links.filter(badged).length).toBeGreaterThan(0);
      if (row.anchors_exhaustive) expect(links.filter(badged)).toHaveLength(matches.length);
    },
  );
});

describe('citation bridge: the counterexamples this contract was written for', () => {
  const only = (key: string) => cases.find((row) => row.key === key) as BridgeCase;

  it('reaches a literal IPv6 source whether or not a badge is drawn', () => {
    const row = only('ipv6_port');
    const [url] = row.web_anchors.map((anchor) => anchor.url);

    const withBadge = webLinks(renderCase(row.web_text, row.web_citations).container);
    cleanup();
    const without = webLinks(renderCase(row.web_text).container);

    // The brackets are authority syntax, and percent-encoding them makes an
    // address no browser will open. A badge may not be the only thing that
    // navigates: both anchors end at the same place, and that place is the
    // source.
    expect(withBadge[0].getAttribute('href')).toBe(url);
    expect(without[0].getAttribute('href')).toBe(url);
    expect(new URL(without[0].href).hostname).toBe('[::1]');
    expect(new URL(without[0].href).port).toBe('8443');
  });

  it('keeps a footnote definition ordinary and badges the real citation', () => {
    const row = only('footnote');
    const { container } = renderCase(row.web_text, row.web_citations);
    const links = webLinks(container);

    // Both links point at the same page and only one was written by the
    // backend. Counting parsed links disagreed across the two parsers here and
    // the badge was silently lost; the span the producer measured does not.
    expect(links.filter(badged)).toHaveLength(1);
    expect(links.filter((anchor) => !badged(anchor)).map((anchor) => anchor.textContent))
      .toContain('p');
  });

  it('shows a host spelled with Markdown emphasis as that host', () => {
    const row = only('star_host');
    const { container } = renderCase(row.web_text, row.web_citations);
    const [badge] = webLinks(container);

    expect(badge.getAttribute('href')).toBe('https://a*b*.example/x');
    expect(badge.getAttribute('aria-label')).toContain('Star Host');
    expect(container.querySelector('em')).toBeNull();
  });

  it('shows a host that spells a character reference verbatim', () => {
    const row = only('entity_host');
    cleanup();
    const { container } = renderCase(row.web_text);

    expect(container.querySelector('a')?.textContent).toBe('a&copy;.example');
  });

  it('does not hand the badge to a surviving copy when the citation is deleted', () => {
    const row = only('prose_repeat');
    const [entry] = row.web_citations;
    const trimmed = row.web_text.replace(`Cited. ${spelling(row, entry)}\n\n`, '');

    const { container } = renderCase(trimmed, row.web_citations);
    const links = webLinks(container);

    // An edit that drops the sentence, or a quotation that copies only part of
    // the answer: what is left is the agent's own prose pointing at the same
    // page, character for character the citation's own spelling, and shifted
    // into the range it used to occupy. Inheriting the badge would credit the
    // search result for what the answer said, so the sidecar and the text
    // disagreeing costs the reader the preview and nothing else.
    expect(trimmed).not.toContain('Cited.');
    expect(trimmed.split(spelling(row, entry))).toHaveLength(3);
    expect(links).toHaveLength(2);
    expect(links.filter(badged)).toHaveLength(0);
    expect(links.map((anchor) => anchor.getAttribute('href'))).toEqual([entry.url, entry.url]);
  });

  it('does not read one surface of a reply with the other surface\'s measurement', () => {
    // One producer run writes two bodies - the workbench keeps the `file://`
    // attachment and IM flattens it to a label - so the same citation sits at
    // [63, 99) in one and [15, 51) in the other. Nothing in a bare range says
    // which body it was counted in; the digest does, and it is the only reason
    // a row cannot be read against the wrong one.
    const row = only('file_link_above');

    expect(row.im_citations[0].spans).not.toEqual(row.web_citations[0].spans);
    expect(webLinks(renderCase(row.web_text, row.im_citations).container).filter(badged))
      .toHaveLength(0);
    cleanup();
    expect(webLinks(renderCase(row.web_text, row.web_citations).container).filter(badged))
      .toHaveLength(1);
  });
});

// ChatPage hands ONE `Markdown` the citation sidecar, the mention sidecar and
// the secret-request opt-in together, and the last two take effect by REWRITING
// the source text before Markdown parses it: `$<NAME>` becomes a secure-input
// card, `@<…>` / `#<…>` become chips. That moves the very link the producer
// counted, which is the one thing a positional contract can be silently broken
// by — so every case is rendered a second time through that production
// combination, and the `marker_shaped` case, whose own producer-written body
// carries all three markers before the citation, is where the shift is real.
//
// The markers are not pasted around the recorded bodies: adding characters
// makes a different body, the digest says so, and the run would prove only that
// a refusal is a refusal. The rewrite has to be measured on a body the producer
// actually wrote.
//
// The archived-transcript card is the one drawn here because it needs no live
// vault client. The rewrite under test runs before either card exists.
const REFERENCES: MentionReference[] = [
  { kind: 'agent', name: 'claude' },
  { kind: 'session', session_id: 'ses6jr7c5h2q6', title: 'Bridge' },
];

const renderWithSurfaceRewrites = (content: string, citations?: CitationSource[]) => render(
  <I18nextProvider i18n={i18n}>
    <RouteSurfaceActiveContext.Provider value>
      <Markdown
        content={content}
        citations={bindCitations(citations, content)}
        references={REFERENCES}
        secretRequests
        readOnly
        interactive
      />
    </RouteSurfaceActiveContext.Provider>
  </I18nextProvider>,
);

describe('citation bridge: the rewrites that run beside it on the real surface', () => {
  const cited = cases.map((row) => [row.key, row] as const);

  it.each(cited)('%s badges the same links once chips and cards are enabled', (_key, row) => {
    const { container } = renderWithSurfaceRewrites(row.web_text, row.web_citations);
    const links = webLinks(container);

    expect(links.filter(badged).map((anchor) => asWritten(anchor.getAttribute('href')))).toEqual(
      row.web_anchors.filter((anchor) => anchor.cited).map((anchor) => anchor.url),
    );
    if (row.anchors_exhaustive) {
      expect(links.map((anchor) => ({
        url: asWritten(anchor.getAttribute('href')),
        cited: badged(anchor),
      }))).toEqual(row.web_anchors.map((anchor) => ({ url: anchor.url, cited: anchor.cited })));
    }
  });

  it('carries the citation across the chips and the card minted before it', () => {
    const row = cases.find((entry) => entry.key === 'marker_shaped') as BridgeCase;
    const [entry] = row.web_citations;
    const { container } = renderWithSurfaceRewrites(row.web_text, row.web_citations);
    const [badge] = webLinks(container);

    // Three markers stand in the prose ahead of the citation, and each is
    // replaced by something of a different length - so the link the backend
    // measured at [71, 142) is not written at 71 by the time Markdown parses
    // it. If no marker were rewritten nothing would have moved and this would
    // prove nothing, so the absence of the raw markers is asserted too.
    expect((entry.spans as number[][])[0][0]).toBe(71);
    expect(container.textContent).not.toContain('@<claude>');
    expect(container.textContent).not.toContain('#<ses6jr7c5h2q6>');
    expect(container.textContent).not.toContain('$<openAiKey>');
    // The chips carry the session's title rather than its id, so the text they
    // replaced the markers with is a different length in both directions.
    expect(container.textContent).toContain('@claude');
    expect(container.textContent).toContain('#Bridge');

    expect(badged(badge)).toBe(true);
    expect(badge.getAttribute('href')).toBe(entry.url);
  });

  it('leaves an address spelled like those markers alone', () => {
    const row = cases.find((entry) => entry.key === 'marker_shaped') as BridgeCase;
    const { container } = renderWithSurfaceRewrites(row.web_text, row.web_citations);
    const [badge] = webLinks(container);

    // `$<…>` and `@<…>` inside a destination are not markers to rewrite, and a
    // card minted inside the link would take the address apart. The producer's
    // percent-encoding is what keeps the two apart, so it is asserted here as
    // the address the reader reaches, not as a spelling rule.
    expect(badge.getAttribute('href')).toBe(row.web_citations[0].url);
    expect(new URL(badge.href).searchParams.get('q')).toBe('$<openAiKey>');
    expect(badge.querySelector('button')).toBeNull();
  });
});
