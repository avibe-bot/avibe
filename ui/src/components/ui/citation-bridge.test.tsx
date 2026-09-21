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
import type { CitationSource } from '@/lib/citations';
import type { MentionReference } from '@/lib/mentions';
import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import { Markdown } from './markdown';

afterEach(cleanup);

type Anchor = { label: string; url: string; cited: boolean };
type BridgeCase = {
  key: string;
  why: string;
  web_text: string;
  citations: CitationSource[];
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

const renderCase = (content: string, citations?: CitationSource[]) => render(
  <I18nextProvider i18n={i18n}>
    <RouteSurfaceActiveContext.Provider value>
      <Markdown content={content} citations={citations} interactive />
    </RouteSurfaceActiveContext.Provider>
  </I18nextProvider>,
);

// Only web links: a `file://` attachment is rewritten to a media-proxy URL
// before the workbench ever renders it, so the raw form recorded here is not a
// destination this component is meant to reach.
const webLinks = (container: HTMLElement) =>
  Array.from(container.querySelectorAll<HTMLAnchorElement>('a[href^="http"]'));

const badged = (anchor: HTMLAnchorElement) => anchor.hasAttribute('data-citation-index');

describe('citation bridge: the real renderer, on one real producer run', () => {
  const cited = cases.map((row) => [row.key, row] as const);

  it.each(cited)('%s shows every source the producer cited', (_key, row) => {
    const { container } = renderCase(row.web_text, row.citations);
    const links = webLinks(container);
    const expected = row.web_anchors.filter((anchor) => anchor.cited);

    // A badge is the citation's rendering, so it must carry the destination and
    // name the source - the label is shown in its preview, not in the strip of
    // text, which is the whole point of a compact badge.
    const badges = links.filter(badged);
    expect(badges).toHaveLength(expected.length);
    expect(badges.map((anchor) => anchor.getAttribute('href'))).toEqual(
      expected.map((anchor) => anchor.url),
    );
    badges.forEach((anchor, at) => {
      const source = row.citations.find((entry) => entry.url === expected[at].url);
      expect(anchor.getAttribute('aria-label')).toBe(
        `Source ${source?.index}: ${source?.title || source?.label}`,
      );
    });
  });

  it.each(cited)('%s leaves every link the answer wrote itself ordinary', (_key, row) => {
    const { container } = renderCase(row.web_text, row.citations);
    const ordinary = webLinks(container).filter((anchor) => !badged(anchor));
    const expected = row.web_anchors.filter((anchor) => !anchor.cited);

    // The answer's own prose may point at the cited page with the very same
    // link. It is still the answer talking, not the search result, and it has
    // to read as what the agent wrote.
    for (const anchor of expected) {
      expect(ordinary.some(
        (node) => node.getAttribute('href') === anchor.url && node.textContent === anchor.label,
      )).toBe(true);
    }
    if (row.anchors_exhaustive) expect(ordinary).toHaveLength(expected.length);
  });

  it.each(cited.filter(([, row]) => row.anchors_exhaustive))(
    '%s badges the occurrence the producer wrote and no other',
    (_key, row) => {
      const { container } = renderCase(row.web_text, row.citations);

      // Position, not just count: three identical links where the middle one is
      // the citation is exactly the case a count cannot tell apart.
      expect(webLinks(container).map((anchor) => ({
        url: anchor.getAttribute('href'),
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
      expect(links.some(
        (node) => node.getAttribute('href') === anchor.url && node.textContent === anchor.label,
      )).toBe(true);
    }
  });

  it.each(cited.filter(([, row]) => row.citations.length > 0))(
    '%s refuses to guess when the sidecar no longer matches the text',
    (_key, row) => {
      // The stored text and the stored sidecar disagree - an edit, a truncation,
      // a row written for another message. Guessing which link was meant is how
      // an ordinary link gets impersonated, so nothing is badged.
      const stale = row.citations.map((entry) => ({ ...entry, spelling: `${entry.spelling} ` }));
      const { container } = renderCase(row.web_text, stale);
      const links = webLinks(container);

      expect(links.filter(badged)).toHaveLength(0);
      for (const anchor of row.web_anchors) {
        expect(links.some(
          (node) => node.getAttribute('href') === anchor.url && node.textContent === anchor.label,
        )).toBe(true);
      }
    },
  );

  it.each(cited.filter(([, row]) => row.citations.length > 0))(
    '%s still renders a row persisted before provenance existed',
    (_key, row) => {
      // Rows already on disk carry no spelling, and the match they were written
      // for is the exact destination and link text. It is less precise - every
      // identical link in the answer gets a badge - and it keeps working.
      const legacy = row.citations.map(({ spelling: _spelling, ...rest }) => rest);
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

    const withBadge = webLinks(renderCase(row.web_text, row.citations).container);
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
    const { container } = renderCase(row.web_text, row.citations);
    const links = webLinks(container);

    // Both links point at the same page and only one was written by the
    // backend. Counting parsed links disagreed across the two parsers here and
    // the badge was silently lost; counting the exact spelling does not.
    expect(links.filter(badged)).toHaveLength(1);
    expect(links.filter((anchor) => !badged(anchor)).map((anchor) => anchor.textContent))
      .toContain('p');
  });

  it('shows a host spelled with Markdown emphasis as that host', () => {
    const row = only('star_host');
    const { container } = renderCase(row.web_text, row.citations);
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
    const [entry] = row.citations;
    const trimmed = row.web_text.replace(`Cited. ${entry.spelling}\n\n`, '');

    const { container } = renderCase(trimmed, row.citations);
    const links = webLinks(container);

    // An edit that drops the sentence, or a quotation that copies only part of
    // the answer: what is left points at the same page with the same spelling
    // and is still the agent's own prose. Inheriting the badge would credit it
    // to the search result, so the sidecar and the text disagreeing costs the
    // reader the preview and nothing else.
    expect(links).toHaveLength(2);
    expect(links.filter(badged)).toHaveLength(0);
    expect(links.map((anchor) => anchor.getAttribute('href'))).toEqual([entry.url, entry.url]);
  });
});

// ChatPage hands ONE `Markdown` the citation sidecar, the mention sidecar and
// the secret-request opt-in together, and the last two take effect by REWRITING
// the source text before Markdown parses it: `$<NAME>` becomes a secure-input
// card, `@<…>` / `#<…>` become chips. That inserts characters around the very
// link the producer counted, which is the one thing a positional contract can
// be silently broken by — so every case is rendered a second time through that
// production combination, with markers added to the prose.
//
// The archived-transcript card is the one drawn here because it needs no live
// vault client. The rewrite under test runs before either card exists.
const REFERENCES: MentionReference[] = [
  { kind: 'agent', name: 'claude' },
  { kind: 'session', session_id: 'ses6jr7c5h2q6', title: 'Bridge' },
];
const MARKERS_BEFORE = 'Ping @<claude> in #<ses6jr7c5h2q6>.\n\n';
const MARKERS_AFTER = '\n\nThen provide $<openAiKey>.';

const renderWithSurfaceRewrites = (content: string, citations?: CitationSource[]) => render(
  <I18nextProvider i18n={i18n}>
    <RouteSurfaceActiveContext.Provider value>
      <Markdown
        content={content}
        citations={citations}
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

  it.each(cited)('%s badges the same links once chips and cards are inserted', (_key, row) => {
    const { container } = renderWithSurfaceRewrites(
      `${MARKERS_BEFORE}${row.web_text}${MARKERS_AFTER}`,
      row.citations,
    );
    const links = webLinks(container);

    // If no marker was rewritten, nothing was inserted and this proves nothing.
    expect(container.textContent).not.toContain('@<claude>');
    expect(container.textContent).not.toContain('$<openAiKey>');

    expect(links.filter(badged).map((anchor) => anchor.getAttribute('href'))).toEqual(
      row.web_anchors.filter((anchor) => anchor.cited).map((anchor) => anchor.url),
    );
    if (row.anchors_exhaustive) {
      expect(links.map((anchor) => ({
        url: anchor.getAttribute('href'),
        cited: badged(anchor),
      }))).toEqual(row.web_anchors.map((anchor) => ({ url: anchor.url, cited: anchor.cited })));
    }
  });

  it('leaves an address spelled like those markers alone', () => {
    const row = cases.find((entry) => entry.key === 'marker_shaped') as BridgeCase;
    const { container } = renderWithSurfaceRewrites(row.web_text, row.citations);
    const [badge] = webLinks(container);

    // `$<…>` and `@<…>` inside a destination are not markers to rewrite, and a
    // card minted inside the link would take the address apart. The producer's
    // percent-encoding is what keeps the two apart, so it is asserted here as
    // the address the reader reaches, not as a spelling rule.
    expect(badge.getAttribute('href')).toBe(row.citations[0].url);
    expect(new URL(badge.href).searchParams.get('q')).toBe('$<openAiKey>');
    expect(badge.querySelector('button')).toBeNull();
  });
});
