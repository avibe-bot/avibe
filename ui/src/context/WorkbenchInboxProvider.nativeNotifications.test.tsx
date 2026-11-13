// @vitest-environment jsdom

import { useEffect } from 'react';
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { INBOX_READ_CACHE_TTL_MS } from './ApiContext';
import type { InboxFeedResult, WorkbenchEventHandlers } from './ApiContext';
import { BotRosterContext } from './BotRosterContext';
import { NotificationPreferencesContext } from './NotificationPreferencesContext';
import { useWorkbenchInbox } from './WorkbenchInboxContext';
import { WorkbenchInboxProvider } from './WorkbenchInboxProvider';
import { DEFAULT_NOTIFICATION_PREFERENCES as preferences } from '../lib/notificationPreferences';

// In the native App the Web UI names the unread Sessions of each whole unread
// map it reads (`notifications.reconcile`, docs/plans/ios-app.md section 4), so
// the App retires the notifications of the Sessions that were read, with the
// time the read went out.

const apiRef = { current: null as unknown };
vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

/** A clock each request advances while it is out, so "before it went out" is visible. */
const clock = { now: 1_000_000 };
const REQUEST_TAKES_MS = 700;

/** The server's unread counts, whatever the App reads next. */
function fakeServer(unread: Record<string, number>) {
  const server = { unread };
  const handlers: WorkbenchEventHandlers[] = [];
  const listInbox = vi.fn(async (): Promise<InboxFeedResult> => {
    clock.now += REQUEST_TAKES_MS;
    return { sessions: [], next_cursor: null, unread_by_session: { ...server.unread } };
  });
  const markSessionRead = vi.fn(async (sessionId: string) => {
    clock.now += REQUEST_TAKES_MS;
    const next = { ...server.unread };
    delete next[sessionId];
    server.unread = next;
    return { updated: 1, unread_counts: {}, unread_by_session: { ...next } };
  });
  apiRef.current = {
    listInbox,
    markSessionRead,
    connectWorkbenchEvents: (next: WorkbenchEventHandlers) => {
      handlers.push(next);
      return () => undefined;
    },
  };
  return { server, listInbox, markSessionRead };
}

const settle = () => act(async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
});

const inbox: { current: ReturnType<typeof useWorkbenchInbox> | null } = { current: null };
const Probe = () => {
  const value = useWorkbenchInbox();
  useEffect(() => {
    inbox.current = value;
  });
  return null;
};

const mount = () => render(
  <BotRosterContext.Provider value={{ roster: { sessionIds: new Set(), known: true }, publish: () => {} }}>
    <NotificationPreferencesContext.Provider value={{ preferences, loaded: true, update: async () => true }}>
      <WorkbenchInboxProvider><Probe /></WorkbenchInboxProvider>
    </NotificationPreferencesContext.Provider>
  </BotRosterContext.Provider>,
);

const installShell = (capabilities: Record<string, number>) => {
  const call = vi.fn(async () => null);
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: { contract: 1, platform: 'ios', shellVersion: '1.0 (1)', capabilities, call, on: vi.fn(() => () => undefined) },
  });
  return call;
};

type Reconcile = { unreadSessions: string[]; asOf: number };
const reconcileCalls = (call: ReturnType<typeof installShell>): Reconcile[] =>
  (call.mock.calls as unknown as [string, Reconcile][])
    .filter(([method]) => method === 'notifications.reconcile')
    .map(([, params]) => params);

const setVisibility = async (state: 'hidden' | 'visible') => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: state });
  await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
};

beforeEach(() => {
  clock.now = 1_000_000;
  vi.spyOn(Date, 'now').mockImplementation(() => clock.now);
  vi.spyOn(document, 'hasFocus').mockReturnValue(true);
});
afterEach(() => {
  cleanup();
  inbox.current = null;
  Reflect.deleteProperty(window, 'avibeNative');
  Reflect.deleteProperty(document, 'visibilityState');
  vi.restoreAllMocks();
});

describe('naming the unread Sessions to the native App', () => {
  it('sends each whole map it reads with a time no later than the read went out', async () => {
    const call = installShell({ app: 1, badge: 1, notifications: 1 });
    const { server } = fakeServer({ ses_a: 2, ses_b: 1 });
    mount();
    await settle();

    // The first load may be a shared cached read: its time is the cache's
    // lifetime before the call, at or before when that read went out.
    expect(reconcileCalls(call)[0]).toEqual({
      unreadSessions: ['ses_a', 'ses_b'],
      asOf: 1_000_000 - INBOX_READ_CACHE_TTL_MS,
    });

    // Back in the foreground, after `ses_a` was read on another device: the
    // counts read names only `ses_b`, timed before its request went out.
    await setVisibility('hidden');
    server.unread = { ses_b: 1 };
    const resumedAt = clock.now;
    await setVisibility('visible');
    await settle();
    expect(reconcileCalls(call).at(-1)).toEqual({ unreadSessions: ['ses_b'], asOf: resumedAt });
  });

  it('sends the map a mark-read answers with, timed before the write went out', async () => {
    const call = installShell({ app: 1, notifications: 1 });
    fakeServer({ ses_a: 2, ses_b: 1 });
    mount();
    await settle();

    const readAt = clock.now;
    await act(async () => {
      await inbox.current?.markRead('ses_a');
    });
    await settle();
    expect(reconcileCalls(call)).toContainEqual({ unreadSessions: ['ses_b'], asOf: readAt });
  });

  it('sends nothing for a read that failed or an answer without a map', async () => {
    const call = installShell({ app: 1, notifications: 1 });
    const { markSessionRead } = fakeServer({ ses_a: 2 });
    mount();
    await settle();
    const before = reconcileCalls(call).length;

    markSessionRead.mockResolvedValueOnce({ ok: false, error: 'forbidden' } as never);
    await act(async () => {
      await inbox.current?.markRead('ses_a');
    });
    await settle();
    expect(reconcileCalls(call)).toHaveLength(before);
  });

  it('leaves an older shell without the notifications capability alone', async () => {
    const call = installShell({ app: 1, badge: 1 });
    fakeServer({ ses_a: 2 });
    mount();
    await settle();
    await setVisibility('hidden');
    await setVisibility('visible');
    await act(async () => {
      await inbox.current?.markRead('ses_a');
    });
    await settle();
    expect(reconcileCalls(call)).toEqual([]);
  });
});
