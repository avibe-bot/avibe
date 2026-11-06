/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useEffect, useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import type { InboxSession, WorkbenchEventHandlers } from '../../context/ApiContext';
import { WorkbenchInboxProvider } from '../../context/WorkbenchInboxProvider';
import { useWorkbenchInbox } from '../../context/WorkbenchInboxContext';
import { writeInboxFilter } from '../../lib/inboxFilterMemory';
import { InboxPage } from './InboxPage';

// "Load more" belongs to the view the reader is looking at. Owner report
// (2026-11-06): with nothing more to show it stayed on screen, and a click did
// nothing. These hold the real provider and the real page to the rule: the
// button is there only when the current view has more, and a click shows more,
// keeps loading until it does, or takes the button away. Never a click that does
// nothing.

const apiRef = vi.hoisted(() => ({ current: null as unknown }));
vi.mock('../../context/ApiContext', async () => {
  const actual = await vi.importActual<typeof import('../../context/ApiContext')>('../../context/ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_chat: true, can_read_instance: false } }),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../ui/markdown', () => ({ Markdown: ({ content }: { content: string }) => <p>{content}</p> }));

type ListInboxArgs = { limit?: number; before?: string; onlySession?: string };

const minute = (n: number) => new Date(Date.UTC(2026, 10, 6, 8, 0) - n * 60_000).toISOString();
const key = (row: InboxSession) => `${row.last_activity_at}|${row.session_id}`;
const row = (n: number): InboxSession => ({
  session_id: `ses_${String(n).padStart(3, '0')}`, scope_id: null, project_id: 'proj', project_name: 'avibe',
  title: `Conversation ${n}`, last_activity_at: minute(n), last_message_author: 'agent', replied: false,
  preview_text: `Reply ${n}`, preview_at: minute(n), unread_count: 0, unread: false,
});

/** `GET /api/inbox` as the server pages it: newest first, keyset cursor, and a
 *  cursor only when another row exists past the page. */
function fakeServer(count: number, unread: Record<number, number>) {
  const rows = Array.from({ length: count }, (_, i) => row(i + 1));
  const unreadBySession = Object.fromEntries(Object.entries(unread).map(([n, c]) => [row(Number(n)).session_id, c]));
  const page = ({ limit = 30, before, onlySession }: ListInboxArgs) => {
    const matching = rows.filter((r) => (onlySession ? r.session_id === onlySession : before === undefined || key(r) < before));
    const window = matching.slice(0, limit);
    return {
      sessions: window,
      next_cursor: matching.length > limit ? key(window[window.length - 1]) : null,
      unread_by_session: unreadBySession,
    };
  };
  const pending: Array<() => void> = [];
  const handlers: WorkbenchEventHandlers[] = [];
  const server = {
    handlers,
    hold: false,
    listInbox: vi.fn((args: ListInboxArgs = {}) => {
      const answer = page(args);
      if (!server.hold) return Promise.resolve(answer);
      return new Promise((resolve) => pending.push(() => resolve(answer)));
    }),
    release: () => act(async () => {
      server.hold = false;
      for (const go of pending.splice(0)) go();
    }),
    cursorReads: () => server.listInbox.mock.calls.filter(([args]) => args?.before).length,
  };
  apiRef.current = {
    listInbox: server.listInbox,
    markSessionRead: vi.fn(async () => ({ updated: 0 })),
    connectWorkbenchEvents: (next: WorkbenchEventHandlers) => {
      handlers.push(next);
      return () => undefined;
    },
  };
  return server;
}

const settle = () => act(async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
});

const mount = async () => {
  render(
    <MemoryRouter initialEntries={['/inbox']}>
      <WorkbenchInboxProvider>
        <InboxPage onOpenSearch={() => {}} />
      </WorkbenchInboxProvider>
    </MemoryRouter>,
  );
  await settle();
};

const cards = () => Array.from(document.querySelectorAll('[data-inbox-session-id]')).map((node) => node.getAttribute('data-inbox-session-id'));
const loadMoreButton = () => screen.queryByRole('button', { name: 'workbench.inbox.loadMore' });
const loadingMore = () => document.querySelector('[data-flip-id="more"] [role="status"]') !== null;
const click = async () => {
  fireEvent.click(loadMoreButton()!);
  await settle();
};

beforeEach(() => {
  writeInboxFilter('unread', 0);
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('Inbox "Load more"', () => {
  it('is not offered on the Unread tab when every unread conversation is already shown', async () => {
    const server = fakeServer(70, { 1: 2, 3: 1 });
    await mount();

    expect(cards()).toEqual(['ses_001', 'ses_003']);
    expect(loadMoreButton()).toBeNull();
    expect(server.cursorReads()).toBe(0);
  });

  it('on the Unread tab, keeps loading in one click until an unread conversation further down shows', async () => {
    const server = fakeServer(100, { 2: 1, 65: 3 });
    await mount();
    expect(cards()).toEqual(['ses_002']);

    await click();

    // Pages 2 (rows 31-60) and 3 (61-90) in one click: page 2 holds nothing unread.
    expect(server.cursorReads()).toBe(2);
    expect(cards()).toEqual(['ses_002', 'ses_065']);
    // Nothing unread is left to find, though the feed itself goes on.
    expect(loadMoreButton()).toBeNull();
  });

  it('shows the next page on the All tab, and goes when the feed ends', async () => {
    writeInboxFilter('all', 0);
    const server = fakeServer(31, {});
    await mount();
    expect(cards()).toHaveLength(30);

    await click();

    expect(server.cursorReads()).toBe(1);
    expect(cards()).toHaveLength(31);
    expect(loadMoreButton()).toBeNull();
  });

  it('answers a click at once while another read of the feed is out, then loads the page', async () => {
    writeInboxFilter('all', 0);
    const server = fakeServer(45, {});
    await mount();
    server.hold = true;
    // A refresh the reader started: the page read waits behind it.
    fireEvent.click(screen.getAllByRole('button', { name: 'workbench.inbox.refresh' })[0]);
    await settle();

    await click();
    expect(loadMoreButton()).toBeNull();
    expect(loadingMore()).toBe(true);

    await server.release();
    await settle();
    await server.release();
    await settle();
    expect(cards()).toHaveLength(45);
    expect(loadingMore()).toBe(false);
    expect(loadMoreButton()).toBeNull();
  });

  it('reads past a page that only brings conversations already shown', async () => {
    writeInboxFilter('all', 0);
    const server = fakeServer(70, {});
    await mount();
    // Restored conversations further down are already on screen (a foreground
    // restore reads one Session by id, wherever it sorts), so the next page holds
    // nothing new.
    await act(async () => {
      for (let n = 31; n <= 60; n += 1) {
        for (const handler of server.handlers) {
          handler.onSessionActivity?.({ session_id: row(n).session_id, scope_id: null, event: 'updated', visibility: 'foreground' });
        }
      }
    });
    await settle();
    expect(cards()).toHaveLength(60);

    await click();

    expect(server.cursorReads()).toBe(2);
    expect(cards()).toHaveLength(70);
    expect(loadMoreButton()).toBeNull();
  });

  it('does not leave the indicator behind when the reader leaves while a click waits', async () => {
    writeInboxFilter('all', 0);
    const server = fakeServer(45, {});
    let leave: () => void = () => {};
    let indicator = false;
    const Shell = () => {
      const [onInbox, setOnInbox] = useState(true);
      const { loadingMore } = useWorkbenchInbox({ feed: false });
      useEffect(() => {
        leave = () => setOnInbox((shown) => !shown);
        indicator = loadingMore;
      });
      return onInbox ? <InboxPage onOpenSearch={() => {}} /> : null;
    };
    render(
      <MemoryRouter initialEntries={['/inbox']}>
        <WorkbenchInboxProvider><Shell /></WorkbenchInboxProvider>
      </MemoryRouter>,
    );
    await settle();
    server.hold = true;
    fireEvent.click(screen.getAllByRole('button', { name: 'workbench.inbox.refresh' })[0]);
    await settle();
    await click();
    expect(indicator).toBe(true);

    act(() => leave());
    await server.release();
    await settle();
    expect(indicator).toBe(false);

    act(() => leave());
    await settle();
    expect(loadingMore()).toBe(false);
    expect(loadMoreButton()).not.toBeNull();
  });

  it('says all caught up, not "load more", when the feed ended and the Unread tab shows nothing', async () => {
    // The unread map names a conversation the feed does not return: nothing more
    // can be loaded, so there is no further-down note pointing at a missing button.
    fakeServer(10, {});
    (apiRef.current as { listInbox: unknown }).listInbox = vi.fn(async () => ({
      sessions: Array.from({ length: 10 }, (_, i) => row(i + 1)), next_cursor: null, unread_by_session: { ses_gone: 1 },
    }));
    await mount();

    expect(cards()).toEqual([]);
    expect(screen.queryByText('workbench.inbox.moreUnreadBeyond')).toBeNull();
    expect(screen.getByText('workbench.inbox.allClearTitle')).toBeTruthy();
    expect(loadMoreButton()).toBeNull();
  });

  it('stops after a bounded number of pages that show nothing, and offers to go on', async () => {
    // The unread map names a conversation the feed never returns: a click walks
    // a bounded number of pages and hands the button back, without spinning on.
    const server = fakeServer(600, { 1: 1, 999: 1 });
    (apiRef.current as { listInbox: unknown }).listInbox = vi.fn(async (args: ListInboxArgs = {}) => {
      const answer = await server.listInbox(args);
      return { ...answer, unread_by_session: { ses_001: 1, ses_gone: 1 } };
    });
    await mount();
    expect(cards()).toEqual(['ses_001']);

    await click();
    const pages = (apiRef.current as { listInbox: ReturnType<typeof vi.fn> }).listInbox.mock.calls
      .filter(([args]) => (args as ListInboxArgs | undefined)?.before).length;
    expect(pages).toBe(10);
    expect(loadingMore()).toBe(false);
    expect(loadMoreButton()).not.toBeNull();
  });
});
