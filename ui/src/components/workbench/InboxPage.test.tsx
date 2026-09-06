/* @vitest-environment jsdom */

import { StrictMode } from 'react';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import type { InboxSession } from '../../context/ApiContext';
import { INBOX_REVERT_AFTER_MS, writeInboxFilter } from '../../lib/inboxFilterMemory';
import { APP_SHELL_SCROLL_ID } from '../../lib/mobileProjectsListMemory';
import { InboxPage } from './InboxPage';

const feed = vi.hoisted(() => ({
  inboxSessions: [] as InboxSession[],
  unreadBySession: {} as Record<string, number>,
  loading: false,
  nextCursor: 'next-page',
  refresh: vi.fn(),
  loadMore: vi.fn(),
  markRead: vi.fn(),
}));
vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: () => ({
    ...feed,
    totalUnread: Object.keys(feed.unreadBySession).length,
    unreadSessions: Object.keys(feed.unreadBySession).length,
    loadingMore: false,
  }),
}));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_chat: true, can_read_instance: false } }),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../ui/markdown', () => ({ Markdown: ({ content }: { content: string }) => <p>{content}</p> }));

const row = (id: number): InboxSession => ({
  session_id: `session-${id}`, title: `Session ${id}`, scope_id: null,
  project_id: null, project_name: null, last_activity_at: '2026-09-06T00:00:00Z',
  last_message_author: 'agent', replied: false, preview_text: `Reply ${id}`,
  preview_at: null, unread_count: 1, unread: true,
});

let shell: HTMLDivElement;
let rowHeight: number;
let entry = 0;
let resizeCallbacks: Set<() => void>;

function Chat() {
  const navigate = useNavigate();
  return <>
    <button onClick={() => navigate(-1)}>Back</button>
    <button onClick={() => navigate('/inbox')}>New Inbox entry</button>
  </>;
}

function mount() {
  const key = `inbox-${++entry}`;
  const app = () => <StrictMode><MemoryRouter initialEntries={[{ pathname: '/inbox', key }]}>
    <Routes>
      <Route path="/inbox" element={<InboxPage />} />
      <Route path="/chat/:sessionId" element={<Chat />} />
      <Route path="/search" element={<Chat />} />
    </Routes>
  </MemoryRouter></StrictMode>;
  const view = render(app(), { container: shell });
  return { ...view, update: () => view.rerender(app()) };
}

const article = (id: number) => shell.querySelector<HTMLElement>(`[data-inbox-session-id="session-${id}"]`)!;
const offset = (id: number) => article(id).getBoundingClientRect().top - shell.getBoundingClientRect().top;
const open = (id: number) => {
  fireEvent.click(within(article(id)).getByRole('button', { name: /workbench.inbox.openSession/ }));
  expect(shell.scrollTop).toBe(0); // Chat's shorter layout clamps the shared scroll owner.
};
const back = () => fireEvent.click(screen.getByText('Back'));

beforeEach(() => {
  writeInboxFilter('unread', 0);
  feed.inboxSessions = Array.from({ length: 70 }, (_, index) => row(index + 1));
  feed.unreadBySession = Object.fromEntries(feed.inboxSessions.map((s) => [s.session_id, 1]));
  feed.loading = false;
  feed.refresh.mockClear();
  feed.loadMore.mockClear();
  rowHeight = 160;
  shell = document.createElement('div');
  shell.id = APP_SHELL_SCROLL_ID;
  shell.style.overflowY = 'auto';
  document.body.appendChild(shell);
  let top = 0;
  const max = () => Math.max(0, shell.querySelectorAll('article').length * rowHeight + 200 - 600);
  Object.defineProperties(shell, {
    scrollTop: { configurable: true, get: () => { top = Math.min(top, max()); return top; },
      set: (next: number) => { top = Math.max(0, Math.min(next, max())); } },
    clientHeight: { configurable: true, value: 600 },
    scrollHeight: { configurable: true, get: () => max() + 600 },
  });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const index = Array.from(shell.querySelectorAll('article')).indexOf(this);
    const y = this === shell ? 64 : 64 + 200 + Math.max(0, index) * rowHeight - shell.scrollTop;
    const height = this === shell ? 600 : rowHeight;
    return { x: 0, y, top: y, bottom: y + height, left: 0, right: 390, width: 390, height, toJSON: () => ({}) };
  });
  resizeCallbacks = new Set();
  vi.stubGlobal('ResizeObserver', class {
    callback: () => void;
    constructor(callback: () => void) { this.callback = callback; }
    observe() { resizeCallbacks.add(this.callback); }
    disconnect() { resizeCallbacks.delete(this.callback); }
  });
});

afterEach(() => {
  cleanup();
  shell.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('Inbox return position', () => {
  it('keeps the loaded window and reading position on repeated history returns', () => {
    mount();
    for (const id of [65, 64, 63]) {
      shell.scrollTop = 9900;
      const before = offset(id);
      open(id);
      back();
      expect(offset(id)).toBe(before);
      expect(shell.querySelectorAll('article')).toHaveLength(70);
    }
    expect(feed.refresh).not.toHaveBeenCalled();
    expect(feed.loadMore).not.toHaveBeenCalled();
  });

  it('anchors a surviving visible conversation when read rows disappear', () => {
    mount();
    shell.scrollTop = 650;
    const before = offset(4);
    open(3);
    delete feed.unreadBySession['session-3'];
    back();
    expect(article(3)).toBeNull();
    expect(offset(4)).toBe(before);
    expect(shell.scrollTop).toBe(490);
  });

  it('does not replay a consumed Chat snapshot after a later Search return', () => {
    mount();
    shell.scrollTop = 650;
    open(3);
    back();
    expect(shell.scrollTop).toBe(650);
    // A successful restore must consume the shared snapshot even without an
    // input event. Its local copy still handles layout corrections in this visit.
    shell.scrollTop = 300;
    fireEvent.click(screen.getByRole('button', { name: /workbench.search.entry/ }));
    expect(shell.scrollTop).toBe(0);
    back();
    expect(shell.scrollTop).toBe(0);
  });

  it('uses the nearest surviving neighbor when every visible row disappears', () => {
    mount();
    shell.scrollTop = 650;
    open(3);
    for (let id = 3; id <= 7; id++) delete feed.unreadBySession[`session-${id}`];
    back();
    expect(offset(8)).toBe(shell.clientHeight - rowHeight);
    expect(shell.scrollTop).toBeGreaterThan(0);
  });

  it('clamps to the new bottom when the last unread conversation is removed', () => {
    mount();
    shell.scrollTop = shell.scrollHeight;
    open(70);
    delete feed.unreadBySession['session-70'];
    back();
    expect(shell.scrollTop).toBe(shell.scrollHeight - shell.clientHeight);
    expect(shell.scrollTop).toBeGreaterThan(0);
  });

  it('preserves the anchor when new activity inserts conversations above it', () => {
    mount();
    shell.scrollTop = 650;
    const before = offset(3);
    open(3);
    feed.inboxSessions = [row(90), ...feed.inboxSessions];
    feed.unreadBySession['session-90'] = 1;
    back();
    expect(offset(3)).toBe(before);
    expect(shell.scrollTop).toBe(810);
  });

  it('waits for rows and corrects late layout changes without overriding user input', () => {
    const view = mount();
    shell.scrollTop = 650;
    const before = offset(3);
    open(3);
    const sessions = feed.inboxSessions;
    feed.inboxSessions = [];
    feed.loading = true;
    back();
    expect(shell.scrollTop).toBe(0);
    feed.inboxSessions = sessions;
    feed.loading = false;
    // A parent context update renders the page even though the route is unchanged.
    view.update();
    expect(offset(3)).toBe(before);
    rowHeight = 190;
    act(() => resizeCallbacks.forEach((callback) => callback()));
    expect(offset(3)).toBe(before);
    fireEvent.wheel(shell);
    shell.scrollTop = 300;
    rowHeight = 210;
    act(() => resizeCallbacks.forEach((callback) => callback()));
    expect(shell.scrollTop).toBe(300);
  });

  it.each(['wheel', 'touchstart', 'pointerdown', 'keydown'])('respects %s input before delayed rows return', (event) => {
    const view = mount();
    shell.scrollTop = 650;
    open(3);
    const sessions = feed.inboxSessions;
    feed.inboxSessions = [];
    back();
    fireEvent(shell, new Event(event, { bubbles: true }));
    // Resize can occur before the next React commit; it must not reclaim scrolling.
    act(() => resizeCallbacks.forEach((callback) => callback()));
    expect(resizeCallbacks.size).toBe(0);
    feed.inboxSessions = sessions;
    view.update();
    expect(shell.scrollTop).toBe(0);
    fireEvent.click(screen.getByRole('button', { name: /workbench.search.entry/ }));
    back();
    expect(shell.scrollTop).toBe(0);
  });

  it('does not restore another history entry, even with the same filter', () => {
    mount();
    shell.scrollTop = 650;
    open(3);
    fireEvent.click(screen.getByText('New Inbox entry'));
    expect(shell.scrollTop).toBe(0);
  });

  it('does not restore an expired return even when the filter is unchanged', () => {
    mount();
    shell.scrollTop = 650;
    open(3);
    const now = Date.now();
    vi.spyOn(Date, 'now').mockReturnValue(now + INBOX_REVERT_AFTER_MS + 1);
    back();
    expect(shell.scrollTop).toBe(0);
  });

  it('does not apply the previous filter position to a different filter', () => {
    mount();
    shell.scrollTop = 650;
    open(3);
    writeInboxFilter('all', 0);
    back();
    expect(shell.scrollTop).toBe(0);
  });

  it('starts at the top when the entire old reading neighborhood is gone', () => {
    mount();
    shell.scrollTop = 650;
    open(3);
    feed.inboxSessions = Array.from({ length: 30 }, (_, index) => row(index + 100));
    feed.unreadBySession = Object.fromEntries(feed.inboxSessions.map((s) => [s.session_id, 1]));
    back();
    expect(shell.scrollTop).toBe(0);
  });

  it('leaves an Inbox opened from the top at the top when new rows arrive', () => {
    mount();
    open(1);
    feed.inboxSessions = [row(90), ...feed.inboxSessions];
    feed.unreadBySession['session-90'] = 1;
    back();
    expect(shell.scrollTop).toBe(0);
  });
});
