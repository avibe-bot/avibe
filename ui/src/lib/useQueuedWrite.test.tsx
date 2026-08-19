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

// Records what reached the network as `<key>:<patch>` and hands back a gate per
// call, so a queued write can be observed as "not sent yet" rather than merely
// "not resolved yet".
function gatedSend(started: string[], pending: Map<string, Deferred>) {
  return vi.fn(async (patch: string, key: string) => {
    const label = `${key}:${patch}`;
    started.push(label);
    const gate = deferred();
    pending.set(label, gate);
    return gate.promise;
  });
}

describe('useQueuedWrite', () => {
  afterEach(cleanup);

  it('sends one write at a time per resource, in the order they were queued', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const { result } = renderHook(() => useQueuedWrite(send));

    // Three picks inside one burst: the first is in flight, the other two are
    // only queued — nothing about the second click may reach the network while
    // the first request is still deciding what the server holds.
    act(() => {
      result.current.write('s1', 'first');
      result.current.write('s1', 'second');
      result.current.write('s1', 'third');
    });
    await settle();
    expect(started).toEqual(['s1:first']);

    await act(async () => {
      pending.get('s1:first')!.resolve(true);
      await pending.get('s1:first')!.promise;
    });
    await settle();
    expect(started).toEqual(['s1:first', 's1:second']);

    await act(async () => {
      pending.get('s1:second')!.resolve(true);
      await pending.get('s1:second')!.promise;
    });
    await settle();
    expect(started).toEqual(['s1:first', 's1:second', 's1:third']);
  });

  it('does not make one resource wait behind another', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const { result } = renderHook(() => useQueuedWrite(send));

    // Ordering is a property of a single resource. Two sessions (or two
    // projects) have nothing to overwrite in each other, so a slow write to one
    // must not delay the other.
    act(() => {
      result.current.write('s1', 'a');
      result.current.write('s2', 'b');
    });
    await settle();
    expect(started).toEqual(['s1:a', 's2:b']);
    expect(result.current.isSaving('s1')).toBe(true);
    expect(result.current.isSaving('s2')).toBe(true);
  });

  it('reports saving per resource, from the first queued write until that queue drains', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const { result } = renderHook(() => useQueuedWrite(send));

    expect(result.current.isSaving('s1')).toBe(false);
    act(() => {
      result.current.write('s1', 'a');
      result.current.write('s1', 'b');
    });
    expect(result.current.isSaving('s1')).toBe(true);
    // The chat the user navigated to has nothing pending, so its header must not
    // spin for the one they left.
    expect(result.current.isSaving('s2')).toBe(false);

    await act(async () => {
      pending.get('s1:a')!.resolve(true);
      await pending.get('s1:a')!.promise;
    });
    // The queue is not empty yet, so the surface is still saving.
    expect(result.current.isSaving('s1')).toBe(true);

    await act(async () => {
      pending.get('s1:b')!.resolve(true);
      await pending.get('s1:b')!.promise;
    });
    await settle();
    expect(result.current.isSaving('s1')).toBe(false);
  });

  it('settles once per burst per resource, naming the resource that settled', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const onSettled = vi.fn();
    const { result } = renderHook(() => useQueuedWrite(send, onSettled));

    act(() => {
      result.current.write('s1', 'a');
      result.current.write('s1', 'b');
    });
    await act(async () => {
      pending.get('s1:a')!.resolve(true);
      await pending.get('s1:a')!.promise;
    });
    expect(onSettled).not.toHaveBeenCalled();

    await act(async () => {
      pending.get('s1:b')!.resolve(true);
      await pending.get('s1:b')!.promise;
    });
    await settle();
    expect(onSettled).toHaveBeenCalledExactlyOnceWith('s1', true);
  });

  it('drops the rest of the failed resource queue and leaves other resources alone', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const onSettled = vi.fn();
    const { result } = renderHook(() => useQueuedWrite(send, onSettled));

    act(() => {
      result.current.write('s1', 'a');
      result.current.write('s1', 'b');
      result.current.write('s2', 'c');
      result.current.write('s2', 'd');
    });
    // The writes queued behind a rejected one were built on state the server
    // never took, so replaying them would push a route the user's own state was
    // never derived from. That reasoning is scoped to the failed resource: the
    // other session's queued pick has no relationship to this failure.
    await act(async () => {
      pending.get('s1:a')!.resolve(false);
      await pending.get('s1:a')!.promise;
    });
    await settle();
    expect(result.current.isSaving('s1')).toBe(false);
    expect(onSettled).toHaveBeenCalledExactlyOnceWith('s1', false);
    expect(started).toEqual(['s1:a', 's2:c']);

    await act(async () => {
      pending.get('s2:c')!.resolve(true);
      await pending.get('s2:c')!.promise;
    });
    await settle();
    expect(started).toEqual(['s1:a', 's2:c', 's2:d']);
    expect(result.current.isSaving('s2')).toBe(true);
  });

  it('counts a thrown write as a failure instead of stalling the queue', async () => {
    const gate = deferred();
    const send = vi.fn(() => gate.promise);
    const onSettled = vi.fn();
    const { result } = renderHook(() => useQueuedWrite(send, onSettled));

    act(() => {
      result.current.write('s1', 'a');
      result.current.write('s1', 'b');
    });
    await act(async () => {
      gate.reject(new Error('offline'));
      await gate.promise.catch(() => undefined);
    });
    await settle();
    expect(send).toHaveBeenCalledTimes(1);
    expect(result.current.isSaving('s1')).toBe(false);
    expect(onSettled).toHaveBeenCalledExactlyOnceWith('s1', false);
  });

  it('starts a new burst after the previous one settled', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const onSettled = vi.fn();
    const { result } = renderHook(() => useQueuedWrite(send, onSettled));

    act(() => {
      result.current.write('s1', 'a');
    });
    await act(async () => {
      pending.get('s1:a')!.resolve(true);
      await pending.get('s1:a')!.promise;
    });
    await settle();
    expect(result.current.isSaving('s1')).toBe(false);

    act(() => {
      result.current.write('s1', 'b');
    });
    expect(result.current.isSaving('s1')).toBe(true);
    await act(async () => {
      pending.get('s1:b')!.resolve(true);
      await pending.get('s1:b')!.promise;
    });
    await settle();
    expect(send).toHaveBeenNthCalledWith(2, 'b', 's1');
    expect(onSettled).toHaveBeenCalledTimes(2);
  });

  it('resolves whenDrained per resource, on failure as well as success', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const { result } = renderHook(() => useQueuedWrite(send));

    // An idle resource has nothing to wait for, so a caller that gates on it
    // (the composer waiting for the route the header already shows) is not
    // delayed at all in the common case.
    const idle: string[] = [];
    await act(async () => {
      await result.current.whenDrained('s1').then(() => idle.push('s1'));
    });
    expect(idle).toEqual(['s1']);

    const drained: string[] = [];
    act(() => {
      result.current.write('s1', 'a');
      result.current.write('s1', 'b');
      result.current.write('s2', 'c');
      void result.current.whenDrained('s1').then(() => drained.push('s1'));
      void result.current.whenDrained('s2').then(() => drained.push('s2'));
    });
    await settle();
    expect(drained).toEqual([]);

    // A rejected write must still release the waiter: settle means "landed or
    // failed loudly", so a failing route cannot hold a send hostage forever.
    await act(async () => {
      pending.get('s1:a')!.resolve(false);
      await pending.get('s1:a')!.promise;
    });
    await settle();
    expect(drained).toEqual(['s1']);

    await act(async () => {
      pending.get('s2:c')!.resolve(true);
      await pending.get('s2:c')!.promise;
    });
    await settle();
    expect(drained).toEqual(['s1', 's2']);
  });

  it('sends through the latest send, so a stale closure cannot be replayed', async () => {
    const calls: string[] = [];
    const { result, rerender } = renderHook(
      ({ label }: { label: string }) =>
        useQueuedWrite(async (patch: string, key: string) => {
          calls.push(`${label}:${key}:${patch}`);
          return true;
        }),
      { initialProps: { label: 'first' } },
    );

    rerender({ label: 'second' });
    await act(async () => {
      result.current.write('s1', 'a');
    });
    await settle();
    expect(calls).toEqual(['second:s1:a']);
  });
});
