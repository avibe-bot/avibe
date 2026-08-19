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

  it('hands the burst to the owner that is on screen when it settles', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const settled: string[] = [];
    const mount = (tag: string) =>
      renderHook(() =>
        useCoalescedWrite<string>('t', send, {
          onSettled: (key, committed) => {
            settled.push(`${tag}:${key}:${committed}`);
          },
        }),
      );

    const first = mount('gone');
    act(() => {
      first.result.current.write('s1', 'route');
    });
    await settle();
    first.unmount();
    const second = mount('live');
    await settle();

    await act(async () => {
      pending.get('s1:route')!.resolve(true);
      await pending.get('s1:route')!.promise;
    });
    await settle();
    // The answer lands on the screen that is there when it arrives. Reconciling
    // into the unmounted page would leave the live one showing an optimistic
    // route nobody ever converged — and no `setState` there could fix it.
    expect(settled).toEqual(['live:s1:true']);
    // Handing the burst over is not a reason to re-send it.
    expect(started).toEqual(['s1:route']);
    expect(second.result.current.isSaving('s1')).toBe(false);
    second.unmount();
  });

  it('reports which write opened the burst', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const opened: boolean[] = [];
    const { result } = renderHook(() => useCoalescedWrite<string>('t', send, { merge: joinMerge }));

    act(() => {
      opened.push(result.current.write('s1', 'a'));
      opened.push(result.current.write('s1', 'b'));
      opened.push(result.current.write('s2', 'c'));
    });
    await settle();
    // The opening write is the one moment at which the state the burst replaces
    // is still what the owner holds, so an owner that must revert a rejected
    // burst captures its base here and accumulates into it afterwards. Per
    // resource, not per click: `s2` opens its own burst.
    expect(opened).toEqual([true, false, true]);

    await act(async () => {
      pending.get('s2:c')!.resolve(true);
      await pending.get('s2:c')!.promise;
    });
    await settle();
    act(() => {
      opened.push(result.current.write('s2', 'd'));
    });
    await settle();
    // Settled means the base is spent: the next click opens a fresh burst and
    // captures the state the server confirmed.
    expect(opened).toEqual([true, false, true, true]);
  });

  it('drops what is waiting when the request in flight fails', async () => {
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

    // The write in flight is rejected. What was clicked behind it was composed
    // against the state that request was installing — the state the server has
    // just refused — so sending it now would persist a combination nobody picked.
    // This owner declares no `standsAlone`, which is the conservative default: the
    // failure ends the burst, and the owner's reconcile takes the whole burst back.
    await act(async () => {
      pending.get('s1:title')!.resolve(false);
      await Promise.resolve();
    });
    await settle();
    expect(started).toEqual(['s1:title']);
    // One settle per burst, reporting the burst's outcome.
    expect(settled).toEqual([['s1', false]]);
    expect(result.current.isSaving('s1')).toBe(false);
  });

  // A payload that carries its whole resource was composed against nothing, so a
  // refusal says nothing about it: it is the user's newest intent and still
  // coherent. Dropping it would lose a click to protect against a mismatch that
  // cannot happen — while the burst's shape is unchanged, because `committed`
  // already means "the outcome of the last send".
  const standsAloneWhenWhole = (patch: string) => patch.startsWith('whole');

  it('sends what is waiting past a failure when that patch stands on its own', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const settled: Array<[string, boolean]> = [];
    const { result } = renderHook(() =>
      useCoalescedWrite<string>('t', send, {
        merge: joinMerge,
        standsAlone: standsAloneWhenWhole,
        onSettled: (key, committed) => {
          settled.push([key, committed]);
        },
      }),
    );

    act(() => {
      result.current.write('s1', 'partial');
    });
    await settle();
    act(() => {
      result.current.write('s1', 'whole');
    });

    await act(async () => {
      pending.get('s1:partial')!.resolve(false);
      await Promise.resolve();
    });
    await settle();
    // Sent, not dropped — and nothing has settled yet, because the burst is still
    // going: the resource is still mid-write.
    expect(started).toEqual(['s1:partial', 's1:whole']);
    expect(settled).toEqual([]);
    expect(result.current.isSaving('s1')).toBe(true);

    await act(async () => {
      pending.get('s1:whole')!.resolve(true);
      await Promise.resolve();
    });
    await settle();
    // One settle for the burst, reporting the send that ended it: the server holds
    // the user's newest pick, so the owner converges instead of reverting.
    expect(settled).toEqual([['s1', true]]);
    expect(result.current.isSaving('s1')).toBe(false);
  });

  it('ends the burst as refused when the patch that stood alone is refused too', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const settled: Array<[string, boolean]> = [];
    const { result } = renderHook(() =>
      useCoalescedWrite<string>('t', send, {
        merge: joinMerge,
        standsAlone: standsAloneWhenWhole,
        onSettled: (key, committed) => {
          settled.push([key, committed]);
        },
      }),
    );

    act(() => {
      result.current.write('s1', 'whole-a');
    });
    await settle();
    act(() => {
      result.current.write('s1', 'whole-b');
    });

    await act(async () => {
      pending.get('s1:whole-a')!.resolve(false);
      await Promise.resolve();
    });
    await settle();
    await act(async () => {
      pending.get('s1:whole-b')!.resolve(false);
      await Promise.resolve();
    });
    await settle();

    expect(started).toEqual(['s1:whole-a', 's1:whole-b']);
    // Still ONE settle for the burst, still reporting the last send — continuing
    // past a failure must not turn one burst into two reconciles, or the owner
    // would revert, re-read, and revert again.
    expect(settled).toEqual([['s1', false]]);
    expect(result.current.isSaving('s1')).toBe(false);
  });

  it('asks with both payloads, so the same pending patch can be answered either way', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const asked: Array<[string, string]> = [];
    // The identical pending payload, behind two different refusals: whether it
    // stands alone is a property of the PAIR, so an owner that is told only what
    // waits cannot answer at all.
    const standsAlone = (patch: string, refused: string) => {
      asked.push([patch, refused]);
      return refused === 'same-field';
    };
    const { result } = renderHook(() => useCoalescedWrite<string>('t', send, { merge: joinMerge, standsAlone }));

    act(() => {
      result.current.write('s1', 'same-field');
      result.current.write('s2', 'wider');
    });
    await settle();
    act(() => {
      result.current.write('s1', 'b');
      result.current.write('s2', 'b');
    });

    await act(async () => {
      pending.get('s1:same-field')!.resolve(false);
      pending.get('s2:wider')!.resolve(false);
      await Promise.resolve();
    });
    await settle();

    expect(asked).toEqual([
      ['b', 'same-field'],
      ['b', 'wider'],
    ]);
    expect(started).toEqual(['s1:same-field', 's2:wider', 's1:b']);
  });

  it('asks about the MOST RECENT send, so a burst that fails twice compares against the right one', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const asked: Array<[string, string]> = [];
    const standsAlone = (patch: string, refused: string) => {
      asked.push([patch, refused]);
      return true;
    };
    const { result } = renderHook(() => useCoalescedWrite<string>('t', send, { merge: joinMerge, standsAlone }));

    act(() => {
      result.current.write('s1', 'a');
    });
    await settle();
    act(() => {
      result.current.write('s1', 'b');
    });
    await act(async () => {
      pending.get('s1:a')!.resolve(false);
      await Promise.resolve();
    });
    await settle();
    act(() => {
      result.current.write('s1', 'c');
    });
    await act(async () => {
      pending.get('s1:b')!.resolve(false);
      await Promise.resolve();
    });
    await settle();

    // ``c`` is weighed against ``b``, never against ``a``: the first failure is
    // already accounted for by the payload the second one sent, and comparing
    // against a patch two sends back would answer a question nobody asked.
    expect(asked).toEqual([
      ['b', 'a'],
      ['c', 'b'],
    ]);
    expect(started).toEqual(['s1:a', 's1:b', 's1:c']);
  });

  it('treats a throw as a failed write', async () => {
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
    // A throw counts as a failure, so it ends the burst the same way a `false`
    // does — including the patch that was waiting behind it.
    expect(started).toEqual(['s1:boom']);
    expect(settled).toEqual([['s1', false]]);
    expect(result.current.isSaving('s1')).toBe(false);
  });

  it('reports a burst that committed in parts as uncommitted, leaving the extent to the owner', async () => {
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
      result.current.write('s1', 'agent');
    });
    await settle();
    act(() => {
      result.current.write('s1', 'effort');
    });
    await act(async () => {
      pending.get('s1:agent')!.resolve(true);
      await pending.get('s1:agent')!.promise;
    });
    await settle();
    expect(started).toEqual(['s1:agent', 's1:effort']);

    await act(async () => {
      pending.get('s1:effort')!.resolve(false);
      await pending.get('s1:effort')!.promise;
    });
    await settle();

    // A burst can commit in PARTS: the first request landed, the patch folded in
    // behind it was refused. There is still ONE settle, and `committed` reports
    // the burst's outcome — false, because the state the owner is showing is not
    // the state the server holds.
    //
    // What it deliberately does NOT say is how much of the burst survived. The
    // writer never sees the payloads' fields (they are the owner's shape, merged
    // by the owner's `merge`), so only the owner can know that the Agent pick is
    // on the server and the effort pick is not. An owner that reverts to where its
    // burst STARTED would undo the committed part; the owners here advance their
    // rollback target on each successful send instead.
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
      pending.get('p1:route')!.resolve(true);
      await Promise.resolve();
    });
    await settle();
    // The authoritative re-read is in flight: the project is still mid-write, so
    // the picker keeps its indicator and nothing starts a fresh burst against
    // state the read is still rewriting.
    expect(result.current.isSaving('p1')).toBe(true);

    act(() => {
      result.current.write('p1', 'next');
    });
    await settle();
    expect(started).toEqual(['p1:route']);

    await act(async () => {
      reconcile.resolve(true);
      await reconcile.promise;
    });
    await settle();
    // A pick made during a SUCCESSFUL burst's reconcile is still live intent: it
    // was composed against a route the server took, so it goes out.
    expect(started).toEqual(['p1:route', 'p1:next']);
    expect(result.current.isSaving('p1')).toBe(true);

    await act(async () => {
      pending.get('p1:next')!.resolve(true);
      await pending.get('p1:next')!.promise;
    });
    await settle();
    expect(result.current.isSaving('p1')).toBe(false);
  });

  it('drops a pick made while a failed burst is rolling back', async () => {
    const started: string[] = [];
    const pending = new Map<string, Deferred>();
    const send = gatedSend(started, pending);
    const rollback = deferred();
    const { result } = renderHook(() =>
      useCoalescedWrite<string>('t', send, {
        onSettled: () => rollback.promise.then(() => undefined),
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
    // The rollback read is in flight, so the row on screen is still the one the
    // server refused.
    expect(result.current.isSaving('p1')).toBe(true);

    act(() => {
      result.current.write('p1', 'during-rollback');
    });
    await act(async () => {
      rollback.resolve(true);
      await rollback.promise;
    });
    await settle();
    // This pick was composed against the refused row, so it goes back with it.
    // The rollback lands whole and the user picks again from what the server
    // actually holds.
    expect(started).toEqual(['p1:route']);
    expect(result.current.isSaving('p1')).toBe(false);
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
