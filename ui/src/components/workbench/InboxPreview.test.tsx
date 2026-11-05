/* @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import type { InboxSession } from '../../context/ApiContext';
import { writeInboxFilter } from '../../lib/inboxFilterMemory';
import { INBOX_PREVIEW_MAX_CHARS, inboxPreviewSource } from '../../lib/inboxPreview';
import { InboxPage } from './InboxPage';

// Every card previews the latest reply as Markdown, and a reply can be many
// kilobytes. These hold the Inbox to parsing only what a card can show, and
// only for the card whose text changed (measured: one live update re-parsed
// every card, about 100 ms per update on a 4x-throttled CPU).

const feed = vi.hoisted(() => ({
  inboxSessions: [] as InboxSession[],
  unreadBySession: {} as Record<string, number>,
}));
const parsed = vi.hoisted(() => ({ contents: [] as string[] }));
vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: () => ({
    ...feed,
    totalUnread: 0,
    unreadSessions: 0,
    unreadByCategory: { bot: 0, session: 0 },
    nextCursor: null,
    loading: false,
    loadingMore: false,
    refresh: vi.fn(),
    loadMore: vi.fn(),
    markRead: vi.fn(),
  }),
}));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_chat: true, can_read_instance: false } }),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../ui/markdown', () => ({
  Markdown: ({ content }: { content: string }) => {
    parsed.contents.push(content);
    return <p>{content}</p>;
  },
}));

const row = (id: number, preview = `Reply ${id}`): InboxSession => ({
  session_id: `session-${id}`, title: `Session ${id}`, scope_id: null, project_id: null, project_name: null,
  last_activity_at: `2026-11-05T00:00:${String(60 - id).padStart(2, '0')}Z`, last_message_author: 'agent',
  replied: false, preview_text: preview, preview_at: null, unread_count: 0, unread: false,
});

const page = () => (
  <MemoryRouter initialEntries={['/inbox']}>
    <InboxPage onOpenSearch={() => {}} />
  </MemoryRouter>
);

beforeEach(() => {
  writeInboxFilter('all', 0);
  feed.inboxSessions = Array.from({ length: 20 }, (_, index) => row(index + 1));
  feed.unreadBySession = {};
  parsed.contents = [];
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('Inbox previews', () => {
  it('parses again only the card whose reply changed on a live update', () => {
    const view = render(page());
    expect(parsed.contents).toHaveLength(20);

    // What an ``inbox.session.updated`` upsert hands the page: one new row, at
    // the top, and the same row objects for everything else.
    const updated = { ...row(7, 'A new reply'), last_activity_at: '2026-11-05T00:01:00Z' };
    feed.inboxSessions = [updated, ...feed.inboxSessions.filter((s) => s.session_id !== 'session-7')];
    feed.unreadBySession = { 'session-7': 1 };
    parsed.contents = [];
    view.rerender(page());

    expect(parsed.contents).toEqual(['A new reply']);
  });

  const cap = INBOX_PREVIEW_MAX_CHARS;
  const paragraph = (length: number) => 'word '.repeat(Math.ceil(length / 5)).slice(0, length);

  it.each([
    {
      name: 'keeps a reply within the cap as it is',
      text: paragraph(cap),
      expected: paragraph(cap),
    },
    {
      name: 'keeps the start of a reply past the cap, cutting inside a line',
      text: `${paragraph(100)}\n${paragraph(cap * 3)}`,
      expected: `${paragraph(100)}\n${paragraph(cap * 3)}`.slice(0, cap),
    },
    {
      name: 'keeps the paragraph under a short heading',
      text: `## Summary\n\n${paragraph(cap * 2)}`,
      expected: `## Summary\n\n${paragraph(cap * 2)}`.slice(0, cap),
    },
    {
      name: 'keeps the paragraph after a leading blank line',
      text: `\n${paragraph(cap * 2)}`,
      expected: `\n${paragraph(cap * 2)}`.slice(0, cap),
    },
    {
      name: 'never splits a surrogate pair',
      text: `${'y'.repeat(cap - 1)}😀${'y'.repeat(50)}`,
      expected: 'y'.repeat(cap - 1),
    },
    {
      name: 'keeps link definitions from the rest of the reply',
      text: `See [the plan][plan] and the note[^1].\n${paragraph(cap * 2)}\n[plan]: https://example.com/plan\n[^1]: A footnote.`,
      expected: `${`See [the plan][plan] and the note[^1].\n${paragraph(cap * 2)}`.slice(0, cap)}\n\n[plan]: https://example.com/plan\n[^1]: A footnote.`,
    },
  ])('$name', ({ text, expected }) => {
    expect(inboxPreviewSource(text)).toBe(expected);
  });

  it('hands the card only the part of a long reply it can show', () => {
    const reply = paragraph(cap * 6);
    feed.inboxSessions = [row(1, reply)];
    render(page());
    expect(parsed.contents).toEqual([reply.slice(0, cap)]);
  });
});
