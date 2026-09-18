/* @vitest-environment jsdom */

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { shouldPollVaultRequests, useVaultRequestRefresh } from './useVaultRequestRefresh';

type Handlers = {
  onConnected?: () => void;
  onEventBridgeStatus?: (data: { connected: boolean }) => void;
  onError?: () => void;
  onVaultsUpdated?: (data: unknown) => void;
};

const handlers: Handlers[] = [];

const connectWorkbenchEvents = vi.fn((next: Handlers) => {
  handlers.push(next);
  return () => {
    const at = handlers.indexOf(next);
    if (at >= 0) handlers.splice(at, 1);
  };
});

vi.mock('@/context/ApiContext', () => ({
  useApi: () => ({ connectWorkbenchEvents }),
}));

const POLL_INTERVAL_MS = 5000;

const settle = async () => {
  await act(async () => {
    await Promise.resolve();
  });
};

const advance = async (ms: number) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

const bridgeReports = async (connected: boolean) => {
  await act(async () => {
    for (const handler of [...handlers]) handler.onEventBridgeStatus?.({ connected });
  });
};

/** The frame an editor actually receives: the event type, and nothing else. */
const bareVaultsUpdated = async () => {
  await act(async () => {
    for (const handler of [...handlers]) handler.onVaultsUpdated?.({});
  });
};

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  handlers.length = 0;
  connectWorkbenchEvents.mockClear();
});

describe('Vault request refresh mode', () => {
  it('polls only while the controller event bridge is unavailable', () => {
    expect(shouldPollVaultRequests(false)).toBe(true);
    expect(shouldPollVaultRequests(true)).toBe(false);
  });
});

// PERMISSIONS-016 — `vaults.updated` is admitted at the Editor tier that owns
// the endpoint it announces, and the frame an Editor receives carries no secret
// name or session id. So the refresh the page depends on has to survive on the
// signal alone. The hook is driven here, not its predicate: a stream that never
// emits would satisfy the assertion above and still leave the page asleep.
describe('PERMISSIONS-016 Vault request refresh consumes the bare event', () => {
  it('stops polling once the bridge is healthy and refetches on the event alone', async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    renderHook(() => useVaultRequestRefresh(refresh));

    // The immediate fallback tick is the initial snapshot.
    await settle();
    expect(refresh).toHaveBeenCalledTimes(1);

    await bridgeReports(true);
    await advance(POLL_INTERVAL_MS * 3);
    expect(refresh).toHaveBeenCalledTimes(1);

    await bareVaultsUpdated();
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it('resumes the degraded poll when the bridge reports itself down', async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    renderHook(() => useVaultRequestRefresh(refresh));

    await settle();
    await bridgeReports(true);
    await advance(POLL_INTERVAL_MS * 2);
    const quiet = refresh.mock.calls.length;

    await bridgeReports(false);
    await settle();
    expect(refresh.mock.calls.length).toBe(quiet + 1);

    await advance(POLL_INTERVAL_MS);
    expect(refresh.mock.calls.length).toBe(quiet + 2);
  });

  it('catches up on reconnect for whatever the stream could not deliver', async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    renderHook(() => useVaultRequestRefresh(refresh));

    await settle();
    const before = refresh.mock.calls.length;

    await act(async () => {
      for (const handler of [...handlers]) handler.onConnected?.();
    });
    expect(refresh.mock.calls.length).toBe(before + 1);
  });
});
