// @vitest-environment jsdom

import { act, cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchEventHandlers } from './ApiContext';
import { useNotificationPreferences, type NotificationPreferencesState } from './NotificationPreferencesContext';
import { NotificationPreferencesProvider } from './NotificationPreferencesProvider';
import type { NotificationPreferences } from '../lib/notificationPreferences';

const apiRef = vi.hoisted(() => ({ current: null as unknown }));
vi.mock('./ApiContext', async () => {
  const actual = await vi.importActual<typeof import('./ApiContext')>('./ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const settle = () => act(async () => {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
});

function mount(api: {
  getNotificationPreferences: () => Promise<NotificationPreferences>;
  updateNotificationPreferences?: (preferences: NotificationPreferences) => Promise<NotificationPreferences>;
}) {
  const handlers: WorkbenchEventHandlers[] = [];
  apiRef.current = {
    updateNotificationPreferences: async (preferences: NotificationPreferences) => preferences,
    ...api,
    connectWorkbenchEvents: (next: WorkbenchEventHandlers) => {
      handlers.push(next);
      return () => undefined;
    },
  };
  const state = { current: null as NotificationPreferencesState | null };
  const Probe = () => {
    state.current = useNotificationPreferences();
    return null;
  };
  render(<NotificationPreferencesProvider><Probe /></NotificationPreferencesProvider>);
  return { state, handlers };
}

const ON = { enabled: true, bots: true, sessions: true };
const SESSIONS_MUTED = { enabled: true, bots: true, sessions: false };

afterEach(() => cleanup());

describe('NotificationPreferencesProvider', () => {
  it('reads the account record, and re-reads its own on the shared stream nudge', async () => {
    const getNotificationPreferences = vi.fn()
      .mockResolvedValueOnce(ON)
      .mockResolvedValueOnce(SESSIONS_MUTED);
    const { state, handlers } = mount({ getNotificationPreferences });
    expect(state.current?.loaded).toBe(false);
    await settle();
    expect(state.current).toMatchObject({ preferences: ON, loaded: true });

    // The event reaches every reader, so it carries no record: each re-reads its own.
    await act(async () => handlers[0].onNotificationPreferencesChanged?.());
    await settle();
    expect(getNotificationPreferences).toHaveBeenCalledTimes(2);
    expect(state.current?.preferences).toEqual(SESSIONS_MUTED);
  });

  it('applies a change at once and puts the saved record back when the save is refused', async () => {
    const save = deferred<NotificationPreferences>();
    const { state } = mount({
      getNotificationPreferences: async () => ON,
      updateNotificationPreferences: () => save.promise,
    });
    await settle();

    let kept: boolean | undefined;
    await act(async () => {
      void state.current!.update({ sessions: false }).then((result) => { kept = result; });
    });
    expect(state.current?.preferences).toEqual(SESSIONS_MUTED);

    await act(async () => save.reject(new Error('refused')));
    await settle();
    expect(kept).toBe(false);
    expect(state.current?.preferences).toEqual(ON);
  });

  it('builds a second quick change on the first, not on the last saved record', async () => {
    const saves: NotificationPreferences[] = [];
    const { state } = mount({
      getNotificationPreferences: async () => ON,
      updateNotificationPreferences: async (preferences) => {
        saves.push(preferences);
        return preferences;
      },
    });
    await settle();

    await act(async () => {
      void state.current!.update({ sessions: false });
      void state.current!.update({ enabled: false });
    });
    await settle();

    expect(saves.at(-1)).toEqual({ enabled: false, bots: true, sessions: false });
    expect(state.current?.preferences).toEqual({ enabled: false, bots: true, sessions: false });
  });

  it('keeps an in-flight read from putting the old switches back over a change', async () => {
    const slowRead = deferred<NotificationPreferences>();
    const getNotificationPreferences = vi.fn()
      .mockResolvedValueOnce(ON)
      .mockReturnValueOnce(slowRead.promise);
    const { state, handlers } = mount({ getNotificationPreferences });
    await settle();

    await act(async () => handlers[0].onConnected?.());
    await act(async () => { void state.current!.update({ sessions: false }); });
    await settle();
    await act(async () => slowRead.resolve(ON));
    await settle();

    expect(state.current?.preferences).toEqual(SESSIONS_MUTED);
  });
});
