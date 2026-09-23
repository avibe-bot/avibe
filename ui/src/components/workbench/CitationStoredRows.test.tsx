/* @vitest-environment jsdom */

// The rows the dispatcher stored, read by the surfaces that show them.
//
// `tests/citation_bridge.py` drives the real `ConsolidatedMessageDispatcher`
// over one cited reply against a temporary SQLite home and records the rows it
// wrote — text, sidecar and footer exactly as they land on disk. This file
// hands those rows to the two components a reader actually meets them through:
// the transcript row and the activity card.
//
// The question is narrow and it is the one a body digest could have broken. A
// citation names ranges in the body it was measured in, and for an IM result
// that body is the one with the footer folded into it — which is not the body
// the transcript renders, because the reader is shown the footer separately.
// If the measurement did not survive that split, every ordinary IM transcript
// row would silently drop to a plain link.

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { cleanup, render } from '@testing-library/react';
import { createInstance } from 'i18next';
import type { ReactElement } from 'react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

// The wire is the only stand-in here: a row carrying ``$<NAME>`` renders the
// real secure-input card, which reads the api context. The card is only ever
// rendered in these tests, never opened, so nothing is fetched - but it has to
// find a provider, and the provider has to find a fetch.
const apiFetch = vi.hoisted(() => vi.fn(async () => ({})));
vi.mock('../../lib/apiFetch', () => ({ apiFetch }));

// ``ChatPage`` reaches the composer's mention editor at import time, which reads
// ``matchMedia`` before any test body runs.
vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  });
});

import en from '../../i18n/en.json';
import { ApiProvider } from '../../context/ApiContext';
import type { WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import { ToastProvider } from '../../context/ToastProvider';
import { activityRowFromMessage } from '../../lib/agentActivity';
import type { CitationSource } from '../../lib/citations';
import { ActivityCard, ActivityChip } from './AgentActivityGroup';
import { MessageRow } from './ChatPage';

type StoredRow = {
  key: string;
  why: string;
  platform: string;
  type: 'result' | 'assistant';
  row: {
    type: string;
    text: string;
    content: {
      citations: CitationSource[];
      result_footer?: string;
    };
  };
};

const { rows: STORED } = JSON.parse(
  readFileSync(resolve(process.cwd(), '../tests/fixtures/citation_consumer_bridge.json'), 'utf8'),
) as { rows: StoredRow[] };

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

afterEach(cleanup);

const mount = (ui: ReactElement) => render(
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <ApiProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </ApiProvider>
    </ToastProvider>
  </I18nextProvider>,
);

const session = (): WorkbenchSession =>
  ({
    id: 'ses_bridge_rows',
    scope_id: 'scope-1',
    project_id: 'proj-1',
    title: 'Citations',
    agent_id: 'agt-1',
    agent_name: 'codex',
    agent_backend: 'codex',
    status: 'active',
    agent_status: 'idle',
    created_at: '2026-09-21T12:00:00Z',
    updated_at: '2026-09-21T12:00:00Z',
    metadata: {},
  }) as WorkbenchSession;

/** The stored row as the workbench receives it over the wire. */
const message = (entry: StoredRow, text = entry.row.text): WorkbenchMessage =>
  ({
    id: `msg_${entry.key}`,
    type: entry.row.type,
    author: 'agent',
    source: 'agent',
    text,
    content: entry.row.content,
    metadata: {},
    created_at: '2026-09-21T12:00:00Z',
  }) as unknown as WorkbenchMessage;

const links = (container: HTMLElement) =>
  Array.from(container.querySelectorAll<HTMLAnchorElement>('a[href^="http"]'));

const badged = (anchor: HTMLAnchorElement) => anchor.hasAttribute('data-citation-index');

const results = STORED.filter((entry) => entry.type === 'result').map(
  (entry) => [entry.key, entry] as const,
);

describe('a stored transcript row', () => {
  it.each(results)('%s badges the link the backend wrote and no other', (_key, entry) => {
    const { container } = mount(
      <MessageRow message={message(entry)} session={session()} messageFontSize={13} />,
    );
    const [cite] = entry.row.content.citations;
    const anchors = links(container);

    // Both links point at the same page and are spelled identically; only the
    // second one was written by the backend. A consumer that lost the ranges
    // and matched on text would badge both, and one of them would be crediting
    // the search result for a sentence the agent wrote itself.
    expect(anchors).toHaveLength(2);
    expect(anchors.map((anchor) => anchor.getAttribute('href'))).toEqual([cite.url, cite.url]);
    expect(anchors.map(badged)).toEqual([false, true]);
    expect(anchors[0].textContent).toBe(cite.label);
  });

  it.each(results)('%s shows its footer outside the body it measured', (_key, entry) => {
    const { container } = mount(
      <MessageRow message={message(entry)} session={session()} messageFontSize={13} />,
    );
    const footer = entry.row.content.result_footer;
    if (!footer) {
      expect(container.textContent).not.toContain('⏱️');
      return;
    }

    // The footer is shown once — as the row's own footer, never as part of the
    // answer — and the badge above survived it being taken out of the body.
    const shown = footer.replace(/^✅\s*/, '');
    expect(container.textContent?.split(shown)).toHaveLength(2);
    expect(container.querySelector('.vr-markdown')?.textContent ?? '').not.toContain(shown);
    expect(links(container).filter(badged)).toHaveLength(1);
  });

  it.each(results)('%s keeps every link when the row no longer matches', (_key, entry) => {
    // An edit after the row was written, a truncation, a row read against
    // another message: the ranges still land on real links here, which is
    // exactly why the digest is what decides.
    const { container } = mount(
      <MessageRow
        message={message(entry, `${entry.row.text}\n\nEdited.`)}
        session={session()}
        messageFontSize={13}
      />,
    );
    const anchors = links(container);

    expect(anchors).toHaveLength(2);
    expect(anchors.filter(badged)).toHaveLength(0);
    expect(anchors.every((anchor) => anchor.getAttribute('href') === entry.row.content.citations[0].url))
      .toBe(true);
  });

  it('carries the measurement through every edit stacked above the citation', () => {
    // Two rows, one reply, and the transforms that ran on it in order. The
    // backend rewrote an attachment before it measured anything — to a proxy
    // link on the workbench, to a bare label on IM — so the ranges it wrote
    // already describe a body the model never produced. Then the reader edits
    // that body again: a `$<NAME>` marker becomes a secure-input card above the
    // citation, and an IM row's folded footer is cut off below it. The badge
    // has to come out of all of that on the link the backend actually wrote.
    for (const key of ['workbench_row_rewritten_and_carded', 'im_row_flattened_carded_and_folded']) {
      const entry = STORED.find((row) => row.key === key) as StoredRow;
      const { container } = mount(
        <MessageRow message={message(entry)} session={session()} messageFontSize={13} />,
      );
      const body = container.querySelector('.vr-markdown') as HTMLElement;

      // The card replaced the marker with something 23 code units longer, so a
      // range carried across unchanged would miss its link by that much.
      expect(body.textContent).not.toContain('$<deployKey>');
      expect(body.querySelector('a[href^="avibe-secret:"], button')).not.toBeNull();
      expect(body.textContent).toContain('deployKey');
      // …and the attachment, in whichever form this surface stored it.
      const attachment = body.querySelector<HTMLAnchorElement>('a[href^="/api/media/"]');
      if (entry.platform === 'avibe') expect(attachment).not.toBeNull();
      else expect(attachment).toBeNull();
      expect(body.textContent).toContain('report');

      const anchors = links(container);
      expect(anchors.map(badged)).toEqual([false, true]);
      expect(anchors[1].getAttribute('href')).toBe(entry.row.content.citations[0].url);
      expect(anchors[1].textContent).toBe(String(entry.row.content.citations[0].index));
      cleanup();
    }
  });

  it.each(results)('%s draws no badge on a row the agent did not write', (_key, entry) => {
    // The sidecar travels in ``content``, and a quoted or user-authored row can
    // carry the same content shape. A numbered source badge asserts that the
    // ANSWER cited that page, so authorship gates it before the ranges matter.
    const { container } = mount(
      <MessageRow
        message={{ ...message(entry), author: 'user', type: 'user' } as WorkbenchMessage}
        session={session()}
        messageFontSize={13}
      />,
    );
    expect(links(container).filter(badged)).toHaveLength(0);
  });
});

describe('a stored narration row', () => {
  const entry = STORED.find((row) => row.type === 'assistant') as StoredRow;

  it.each(['live', 'history'])('badges the same link on the %s activity surface', (surface) => {
    const rows = [activityRowFromMessage(message(entry))];
    const { container } = mount(surface === 'live' ? (
      <ActivityCard
        rows={rows}
        startedAtMs={Date.parse('2026-09-21T12:00:00Z')}
        expanded
        onToggleExpanded={vi.fn()}
        showToolCalls
        onToggleTools={vi.fn()}
      />
    ) : (
      <ActivityChip
        group={{
          id: 'turn', anchorMessageId: 'reply', anchorPosition: 'before',
          open: false, status: 'done', steps: 1, durationMs: 1000, rows,
        }}
        expanded
        loading={false}
        onToggle={vi.fn()}
        showToolCalls
        onToggleTools={vi.fn()}
      />
    ));
    const [cite] = entry.row.content.citations;
    const anchors = links(container);

    // A narration row is rendered verbatim — no footer, no split — so the body
    // the sidecar was measured in IS the body handed to the renderer.
    expect(anchors.map(badged)).toEqual([false, true]);
    expect(anchors[1].getAttribute('href')).toBe(cite.url);
    expect(anchors[1].textContent).toBe(String(cite.index));
  });
});
