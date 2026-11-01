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
  updateNotificationPreferences?: (changes: Partial<NotificationPreferences>) => Promise<NotificationPreferences>;
}) {
  const handlers: WorkbenchEventHandlers[] = [];
  apiRef.current = {
    updateNotificationPreferences: async (changes: Partial<NotificationPreferences>) => ({ ...ON, ...changes }),
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
  it('reads the account record, and re-reads it when the stream says it changed', async () => {
    const getNotificationPreferences = vi.fn()
      .mockResolvedValueOnce(ON)
      .mockResolvedValueOnce(SESSIONS_MUTED);
    const { state, handlers } = mount({ getNotificationPreferences });
    expect(state.current?.loaded).toBe(false);
    await settle();
    expect(state.current).toMatchObject({ preferences: ON, loaded: true });

    // Another device changed a switch: the read path adopts the record.
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

  it('sends only the switch a change names, and shows a second quick change on top of the first', async () => {
    // The server merges each write into the stored record, as the real route does.
    let stored = ON;
    const saves: Partial<NotificationPreferences>[] = [];
    const { state } = mount({
      getNotificationPreferences: async () => stored,
      updateNotificationPreferences: async (changes) => {
        saves.push(changes);
        stored = { ...stored, ...changes };
        return stored;
      },
    });
    await settle();

    await act(async () => {
      void state.current!.update({ sessions: false });
      void state.current!.update({ enabled: false });
    });
    await settle();

    // A whole record would carry this device's stale copy of the other switches
    // over a change another device made to them in the meantime.
    expect(saves).toEqual([{ sessions: false }, { enabled: false }]);
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
