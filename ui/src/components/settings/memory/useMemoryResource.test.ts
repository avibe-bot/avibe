import { describe, expect, it } from 'vitest';

import {
  INITIAL_MEMORY_RESOURCE_STATE,
  memoryRequestSettled,
  memoryRequestStarted,
  type MemoryResourceState,
  type MemoryRetryPolicy,
} from './useMemoryResource';

type Payload = { items: string[] };

const POLLED: MemoryRetryPolicy = { clearErrorOnReload: false, resetDataOnError: false };
const MANUAL: MemoryRetryPolicy = { clearErrorOnReload: true, resetDataOnError: true };

const failed = (
  policy: MemoryRetryPolicy,
  prev: MemoryResourceState<Payload> = INITIAL_MEMORY_RESOURCE_STATE,
): MemoryResourceState<Payload> =>
  memoryRequestSettled<Payload>(
    prev,
    { kind: 'error', message: 'sidecar unavailable', forbidden: false },
    policy,
  );

const succeeded = (
  policy: MemoryRetryPolicy,
  prev: MemoryResourceState<Payload> = INITIAL_MEMORY_RESOURCE_STATE,
  items = ['a'],
): MemoryResourceState<Payload> =>
  memoryRequestSettled<Payload>(prev, { kind: 'ok', value: { items } }, policy);

describe('memoryRequestStarted', () => {
  it('drops the previous failure while an explicit refresh or search runs', () => {
    const retrying = memoryRequestStarted(failed(MANUAL), MANUAL);

    expect(retrying).toMatchObject({ error: null, loading: true });
  });

  it('keeps a polled failure banner up so it does not blink on every tick', () => {
    const polling = memoryRequestStarted(failed(POLLED), POLLED);

    expect(polling).toMatchObject({ error: 'sidecar unavailable', loading: true });
  });

  it('keeps the last payload rendered while the next request runs', () => {
    const refreshing = memoryRequestStarted(succeeded(MANUAL), MANUAL);

    expect(refreshing.data).toEqual({ items: ['a'] });
    expect(refreshing.loaded).toBe(true);
  });
});

describe('memoryRequestSettled', () => {
  it('reports success as data without an error and marks the resource loaded', () => {
    expect(succeeded(POLLED, failed(POLLED))).toMatchObject({
      data: { items: ['a'] },
      error: null,
      loading: false,
      loaded: true,
    });
  });

  it('keeps the last good payload behind a polled failure', () => {
    expect(failed(POLLED, succeeded(POLLED)).data).toEqual({ items: ['a'] });
  });

  it('drops the stale payload when an explicit attempt fails', () => {
    expect(failed(MANUAL, succeeded(MANUAL)).data).toBeNull();
  });

  it('keeps the forbidden verdict sticky across a later success', () => {
    const forbidden = memoryRequestSettled<Payload>(
      INITIAL_MEMORY_RESOURCE_STATE,
      { kind: 'error', message: 'available on this device only', forbidden: true },
      POLLED,
    );

    expect(forbidden.forbidden).toBe(true);
    expect(succeeded(POLLED, forbidden).forbidden).toBe(true);
  });
});
