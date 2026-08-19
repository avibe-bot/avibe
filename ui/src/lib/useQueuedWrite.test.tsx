// @vitest-environment jsdom

import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useQueuedWrite } from './useQueuedWrite';

type Deferred = {
  promise: Promise<boolean>;
  resolve: (value: boolean) => void;
  reject: (reason: unknown) => void;
};

function deferred(): Deferred {
  let resolve!: (value: boolean) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<boolean>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

function settle() {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('useQueuedWrite', () => {
  afterEach(cleanup);

  it('sends one write at a time, in the order they were queued', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = vi.fn(async (patch: string) => {
      started.push(patch);
      const gate = deferred();
      pending.set(patch, gate);
      return gate.promise;
    });
    const { result } = renderHook(() => useQueuedWrite(send));

    // Three picks inside one burst: the first is in flight, the other two are
    // only queued — nothing about the second click may reach the network while
    // the first request is still deciding what the server holds.
    act(() => {
      result.current.write('first');
      result.current.write('second');
      result.current.write('third');
    });
    await settle();
    expect(started).toEqual(['first']);

    await act(async () => {
      pending.get('first')!.resolve(true);
      await pending.get('first')!.promise;
    });
    await settle();
    expect(started).toEqual(['first', 'second']);

    await act(async () => {
      pending.get('second')!.resolve(true);
      await pending.get('second')!.promise;
    });
    await settle();
    expect(started).toEqual(['first', 'second', 'third']);
  });

  it('reports saving from the first queued write until the queue drains', async () => {
    const gates = [deferred(), deferred()];
    let call = 0;
    const send = vi.fn(() => gates[call++].promise);
    const { result } = renderHook(() => useQueuedWrite(send));

    expect(result.current.saving).toBe(false);
    act(() => {
      result.current.write('a');
      result.current.write('b');
    });
    expect(result.current.saving).toBe(true);

    await act(async () => {
      gates[0].resolve(true);
      await gates[0].promise;
    });
    // The queue is not empty yet, so the surface is still saving.
    expect(result.current.saving).toBe(true);

    await act(async () => {
      gates[1].resolve(true);
      await gates[1].promise;
    });
    await settle();
    expect(result.current.saving).toBe(false);
  });

  it('settles once per burst, not once per write', async () => {
    const gates = [deferred(), deferred()];
    let call = 0;
    const send = vi.fn(() => gates[call++].promise);
    const onSettled = vi.fn();
    const { result } = renderHook(() => useQueuedWrite(send, onSettled));

    act(() => {
      result.current.write('a');
      result.current.write('b');
    });
    await act(async () => {
      gates[0].resolve(true);
      await gates[0].promise;
    });
    expect(onSettled).not.toHaveBeenCalled();

    await act(async () => {
      gates[1].resolve(true);
      await gates[1].promise;
    });
    await settle();
    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(onSettled).toHaveBeenCalledWith(true);
  });

  it('drops the rest of the queue when a write is rejected by the server', async () => {
    const gates = [deferred(), deferred()];
    let call = 0;
    const send = vi.fn(() => gates[call++].promise);
    const onSettled = vi.fn();
    const { result } = renderHook(() => useQueuedWrite(send, onSettled));

    act(() => {
      result.current.write('a');
      result.current.write('b');
    });
    // The queued writes were built on state the server never took, so replaying
    // them would push a route the user's own state was never derived from.
    await act(async () => {
      gates[0].resolve(false);
      await gates[0].promise;
    });
    await settle();
    expect(send).toHaveBeenCalledTimes(1);
    expect(result.current.saving).toBe(false);
    expect(onSettled).toHaveBeenCalledExactlyOnceWith(false);
  });

  it('counts a thrown write as a failure instead of stalling the queue', async () => {
    const gate = deferred();
    const send = vi.fn(() => gate.promise);
    const onSettled = vi.fn();
    const { result } = renderHook(() => useQueuedWrite(send, onSettled));

    act(() => {
      result.current.write('a');
      result.current.write('b');
    });
    await act(async () => {
      gate.reject(new Error('offline'));
      await gate.promise.catch(() => undefined);
    });
    await settle();
    expect(send).toHaveBeenCalledTimes(1);
    expect(result.current.saving).toBe(false);
    expect(onSettled).toHaveBeenCalledExactlyOnceWith(false);
  });

  it('starts a new burst after the previous one settled', async () => {
    const gates = [deferred(), deferred()];
    let call = 0;
    const send = vi.fn(() => gates[call++].promise);
    const onSettled = vi.fn();
    const { result } = renderHook(() => useQueuedWrite(send, onSettled));

    act(() => {
      result.current.write('a');
    });
    await act(async () => {
      gates[0].resolve(true);
      await gates[0].promise;
    });
    await settle();
    expect(result.current.saving).toBe(false);

    act(() => {
      result.current.write('b');
    });
    expect(result.current.saving).toBe(true);
    await act(async () => {
      gates[1].resolve(true);
      await gates[1].promise;
    });
    await settle();
    expect(send).toHaveBeenNthCalledWith(2, 'b');
    expect(onSettled).toHaveBeenCalledTimes(2);
  });

  it('sends through the latest send, so a stale closure cannot be replayed', async () => {
    const calls: string[] = [];
    const { result, rerender } = renderHook(
      ({ label }: { label: string }) =>
        useQueuedWrite(async (patch: string) => {
          calls.push(`${label}:${patch}`);
          return true;
        }),
      { initialProps: { label: 'first' } },
    );

    rerender({ label: 'second' });
    await act(async () => {
      result.current.write('a');
    });
    await settle();
    expect(calls).toEqual(['second:a']);
  });
});
