// @vitest-environment jsdom

import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { resetCoalescedWrites, useCoalescedWrite } from './useCoalescedWrite';

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
// call, so a write can be observed as "not sent yet" rather than merely "not
// resolved yet".
function gatedSend(started: string[], pending: Map<string, Deferred>) {
  return vi.fn(async (patch: string, key: string) => {
    const label = `${key}:${patch}`;
    started.push(label);
    const gate = deferred();
    pending.set(label, gate);
    return gate.promise;
  });
}

// Merges string patches as `a+b`, standing in for the session row's field union.
const joinMerge = (prev: string, next: string) => `${prev}+${next}`;

describe('useCoalescedWrite', () => {
  // The store is module state on purpose (it outlives the component); each case
  // starts from an empty one.
  beforeEach(resetCoalescedWrites);
  afterEach(cleanup);

  it('keeps one request per resource in flight and folds the clicks made during it into one follow-up', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const { result } = renderHook(() => useCoalescedWrite<string>('t', send, { merge: joinMerge }));

    act(() => {
      result.current.write('s1', 'first');
      result.current.write('s1', 'second');
      result.current.write('s1', 'third');
    });
    await settle();
    // Nothing about the later clicks may reach the network while the first
    // request is still deciding what the server holds.
    expect(started).toEqual(['s1:first']);

    await act(async () => {
      pending.get('s1:first')!.resolve(true);
      await pending.get('s1:first')!.promise;
    });
    await settle();
    // Two clicks, one follow-up request: the intermediate state was transit.
    expect(started).toEqual(['s1:first', 's1:second+third']);
  });

  it('does not make one resource wait behind another', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const { result } = renderHook(() => useCoalescedWrite<string>('t', send));

    act(() => {
      result.current.write('s1', 'a');
      result.current.write('s2', 'b');
    });
    await settle();
    expect(started).toEqual(['s1:a', 's2:b']);
    expect(result.current.isSaving('s1')).toBe(true);
    expect(result.current.isSaving('s2')).toBe(true);
  });

  it('does not let two owners of the same resource race after a remount', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const first = renderHook(() => useCoalescedWrite<string>('t', send, { merge: joinMerge }));

    act(() => {
      first.result.current.write('s1', 'from-first-mount');
    });
    await settle();
    expect(started).toEqual(['s1:from-first-mount']);

    // The user navigates away mid-request and comes back: a hook-local queue
    // would start a second request beside the first and let the older one commit
    // last.
    first.unmount();
    const second = renderHook(() => useCoalescedWrite<string>('t', send, { merge: joinMerge }));
    expect(second.result.current.isSaving('s1')).toBe(true);
    act(() => {
      second.result.current.write('s1', 'from-second-mount');
    });
    await settle();
    expect(started).toEqual(['s1:from-first-mount']);

    await act(async () => {
      pending.get('s1:from-first-mount')!.resolve(true);
      await pending.get('s1:from-first-mount')!.promise;
    });
    await settle();
    expect(started).toEqual(['s1:from-first-mount', 's1:from-second-mount']);
    second.unmount();
  });

  it('still sends what is waiting when the request in flight fails', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const settled: Array<[string, boolean]> = [];
    const { result } = renderHook(() =>
      useCoalescedWrite<string>('t', send, {
        merge: joinMerge,
        onSettled: (key, committed) => {
          settled.push([key, committed]);
        },
      }),
    );

    act(() => {
      result.current.write('s1', 'title');
    });
    await settle();
    act(() => {
      result.current.write('s1', 'route');
    });

    // The independent title save is rejected. The route pick behind it was never
    // derived from that request's success, so dropping it would lose an edit the
    // user can still see in the UI.
    await act(async () => {
      pending.get('s1:title')!.resolve(false);
      await Promise.resolve();
    });
    await settle();
    expect(started).toEqual(['s1:title', 's1:route']);
    expect(settled).toEqual([]);

    await act(async () => {
      pending.get('s1:route')!.resolve(true);
      await pending.get('s1:route')!.promise;
    });
    await settle();
    // One settle per burst, reporting the burst's outcome, not the last write's.
    expect(settled).toEqual([['s1', false]]);
  });

  it('treats a throw as a failed write and keeps draining', async () => {
    const started: string[] = [];
    const boom = deferred();
    const send = vi.fn(async (patch: string, key: string) => {
      started.push(`${key}:${patch}`);
      if (patch === 'boom') return boom.promise;
      return true;
    });
    const settled: Array<[string, boolean]> = [];
    const { result } = renderHook(() =>
      useCoalescedWrite<string>('t', send, {
        onSettled: (key, committed) => {
          settled.push([key, committed]);
        },
      }),
    );

    act(() => {
      result.current.write('s1', 'boom');
    });
    await settle();
    act(() => {
      result.current.write('s1', 'after');
    });
    await act(async () => {
      boom.reject(new Error('network'));
      await boom.promise.catch(() => undefined);
    });
    await settle();
    expect(started).toEqual(['s1:boom', 's1:after']);
    expect(settled).toEqual([['s1', false]]);
    expect(result.current.isSaving('s1')).toBe(false);
  });

  it('stays mid-write until the reconcile finishes and folds a pick made during it into the same writer', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const reconcile = deferred();
    const { result } = renderHook(() =>
      useCoalescedWrite<string>('t', send, {
        onSettled: () => reconcile.promise.then(() => undefined),
      }),
    );

    act(() => {
      result.current.write('p1', 'route');
    });
    await settle();
    await act(async () => {
      pending.get('p1:route')!.resolve(false);
      await Promise.resolve();
    });
    await settle();
    // The rollback read is in flight: the project is still mid-write, so the
    // picker keeps its indicator and nothing starts a fresh burst against state
    // the read is still rewriting.
    expect(result.current.isSaving('p1')).toBe(true);

    let drained = false;
    void result.current.whenDrained('p1').then(() => {
      drained = true;
    });
    act(() => {
      result.current.write('p1', 'retry');
    });
    await settle();
    expect(started).toEqual(['p1:route']);
    expect(drained).toBe(false);

    await act(async () => {
      reconcile.resolve(true);
      await reconcile.promise;
    });
    await settle();
    expect(started).toEqual(['p1:route', 'p1:retry']);

    await act(async () => {
      pending.get('p1:retry')!.resolve(true);
      await pending.get('p1:retry')!.promise;
    });
    await settle();
    expect(drained).toBe(true);
    expect(result.current.isSaving('p1')).toBe(false);
  });

  it('resolves whenDrained immediately for an idle resource and per resource on failure', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const { result } = renderHook(() => useCoalescedWrite<string>('t', send));

    await expect(result.current.whenDrained('idle')).resolves.toBeUndefined();

    act(() => {
      result.current.write('s1', 'a');
      result.current.write('s2', 'b');
    });
    let s1Drained = false;
    let s2Drained = false;
    void result.current.whenDrained('s1').then(() => {
      s1Drained = true;
    });
    void result.current.whenDrained('s2').then(() => {
      s2Drained = true;
    });
    await settle();

    await act(async () => {
      pending.get('s1:a')!.resolve(false);
      await Promise.resolve();
    });
    await settle();
    // A failure must not hold a waiter hostage — "settled" means landed or failed
    // loudly — and one resource's failure says nothing about another's.
    expect(s1Drained).toBe(true);
    expect(s2Drained).toBe(false);
  });

  it('keeps two scopes writing the same id apart', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const rows = renderHook(() => useCoalescedWrite<string>('rows', send));
    const routes = renderHook(() => useCoalescedWrite<string>('routes', send));

    act(() => {
      rows.result.current.write('x', 'row');
      routes.result.current.write('x', 'route');
    });
    await settle();
    expect(started).toEqual(['x:row', 'x:route']);
    expect(rows.result.current.isSaving('x')).toBe(true);
    expect(routes.result.current.isSaving('x')).toBe(true);

    await act(async () => {
      pending.get('x:row')!.resolve(true);
      await pending.get('x:row')!.promise;
    });
    await settle();
    expect(rows.result.current.isSaving('x')).toBe(false);
    expect(routes.result.current.isSaving('x')).toBe(true);
  });

  it('uses the newest send closure', async () => {
    const calls: string[] = [];
    const { result, rerender } = renderHook(
      ({ tag }: { tag: string }) =>
        useCoalescedWrite<string>('t', async (patch: string) => {
          calls.push(`${tag}:${patch}`);
          return true;
        }),
      { initialProps: { tag: 'first' } },
    );

    rerender({ tag: 'second' });
    act(() => {
      result.current.write('s1', 'a');
    });
    await settle();
    expect(calls).toEqual(['second:a']);
  });
});
