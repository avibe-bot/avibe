// @vitest-environment jsdom

import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { InboxFeedResult, WorkbenchEventHandlers } from './ApiContext';
import { BotRosterContext } from './BotRosterContext';
import { NotificationPreferencesContext } from './NotificationPreferencesContext';
import { useWorkbenchInbox } from './WorkbenchInboxContext';
import { WorkbenchInboxProvider } from './WorkbenchInboxProvider';
import { DEFAULT_NOTIFICATION_PREFERENCES as preferences } from '../lib/notificationPreferences';

// In the native App the icon badge follows the Inbox total through the bridge
// (`badge.set`, docs/plans/ios-app.md section 4): WKWebView has no Badging API,
// so the PWA's setAppBadge path never runs there.

const apiRef = { current: null as unknown };
vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

/** The server's unread counts, whatever the App reads next. */
function fakeServer(unread: Record<string, number>) {
  const server = { unread };
  const handlers: WorkbenchEventHandlers[] = [];
  const listInbox = vi.fn(async (): Promise<InboxFeedResult> => ({
    sessions: [],
    next_cursor: null,
    unread_by_session: { ...server.unread },
  }));
  apiRef.current = {
    listInbox,
    markSessionRead: vi.fn(async () => ({ updated: 0 })),
    connectWorkbenchEvents: (next: WorkbenchEventHandlers) => {
      handlers.push(next);
      return () => undefined;
    },
  };
  const emit = (fire: (handler: WorkbenchEventHandlers) => void) => act(async () => {
    for (const handler of handlers) fire(handler);
  });
  return { server, listInbox, emit };
}

const settle = () => act(async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
});

const Probe = () => {
  useWorkbenchInbox();
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

const badgeCalls = (call: ReturnType<typeof installShell>) =>
  call.mock.calls.filter(([method]) => method === 'badge.set').map(([, params]) => params);

const setVisibility = async (state: 'hidden' | 'visible') => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: state });
  await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
};

beforeEach(() => {
  vi.spyOn(document, 'hasFocus').mockReturnValue(true);
});
afterEach(() => {
  cleanup();
  Reflect.deleteProperty(window, 'avibeNative');
  Reflect.deleteProperty(document, 'visibilityState');
  vi.restoreAllMocks();
});

describe('the native App icon badge', () => {
  it('follows the Inbox total through the bridge, and clears at 0', async () => {
    const call = installShell({ app: 1, badge: 1 });
    const { server, emit } = fakeServer({ ses_a: 2, ses_b: 1 });
    mount();
    await settle();
    expect(badgeCalls(call).at(-1)).toEqual({ count: 3 });

    // Everything read: the badge clears.
    server.unread = {};
    await emit((handler) => handler.onConnected?.());
    await settle();
    expect(badgeCalls(call).at(-1)).toEqual({ count: 0 });
  });

  it('reads the counts again when the App comes back to the foreground, and sets the badge from that read', async () => {
    const call = installShell({ app: 1, badge: 1 });
    const { server, listInbox } = fakeServer({ ses_a: 1 });
    mount();
    await settle();
    expect(badgeCalls(call).at(-1)).toEqual({ count: 1 });

    // Away, a push set the icon to the instance's total, and the counts changed.
    await setVisibility('hidden');
    server.unread = { ses_a: 1, ses_b: 4 };
    const reads = listInbox.mock.calls.length;
    const sets = badgeCalls(call).length;
    await setVisibility('visible');
    await settle();

    expect(listInbox.mock.calls.length).toBeGreaterThan(reads);
    expect(badgeCalls(call).length).toBeGreaterThan(sets);
    expect(badgeCalls(call).at(-1)).toEqual({ count: 5 });
  });

  it('leaves an older shell without the badge capability alone', async () => {
    const call = installShell({ app: 1, push: 1 });
    fakeServer({ ses_a: 2 });
    mount();
    await settle();
    await setVisibility('hidden');
    await setVisibility('visible');
    await settle();
    expect(badgeCalls(call)).toEqual([]);
  });
});
