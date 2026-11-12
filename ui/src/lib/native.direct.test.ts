/** @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';

// The `direct` closure watermark (docs/plans/ios-app.md section 4). Its state
// belongs to the document, so every case loads a fresh copy of the module,
// as a new document would.
type NativeModule = typeof import('./native');

const load = async (): Promise<NativeModule> => {
  vi.resetModules();
  return import('./native');
};

/** Lets pending promise callbacks run. */
const settle = async () => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

type DirectShell = {
  call: ReturnType<typeof vi.fn>;
  on: ReturnType<typeof vi.fn>;
  /** `direct.status` calls so far, oldest first. */
  statusCalls: () => number;
  answer: (index: number, value: unknown) => Promise<void>;
  fail: (index: number) => Promise<void>;
  emit: (payload: unknown) => void;
};

const installShell = (capabilities: Record<string, number> = { direct: 1 }): DirectShell => {
  const pending: Array<{ resolve: (value: unknown) => void; reject: (error: unknown) => void }> = [];
  const call = vi.fn(
    (method: string) =>
      new Promise((resolve, reject) => {
        if (method === 'direct.status') pending.push({ resolve, reject });
        else resolve(null);
      }),
  );
  let deliver: (payload: unknown) => void = () => {};
  const on = vi.fn((event: string, handler: (payload: unknown) => void) => {
    if (event === 'direct.transportChanged') deliver = handler;
    return () => undefined;
  });
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: { contract: 1, platform: 'ios', shellVersion: '1.0 (1)', capabilities, call, on },
  });
  return {
    call,
    on,
    statusCalls: () => pending.length,
    answer: async (index, value) => {
      pending[index].resolve(value);
      await settle();
    },
    fail: async (index) => {
      pending[index].reject({ code: 'unavailable', message: 'no' });
      await settle();
    },
    emit: (payload) => deliver(payload),
  };
};

const status = (closure: number, sequence = closure, preferred: 'tunnel' | 'direct' = 'tunnel') => ({
  preferred,
  sequence,
  closure,
});

let visibility: DocumentVisibilityState = 'visible';
Object.defineProperty(document, 'visibilityState', {
  configurable: true,
  get: () => visibility,
});

/** The App went to the background and came back: what WebKit dispatches. */
const hideAndShow = () => {
  visibility = 'hidden';
  document.dispatchEvent(new Event('visibilitychange'));
  visibility = 'visible';
  document.dispatchEvent(new Event('visibilitychange'));
};

const pageshowFromCache = () => {
  const event = new Event('pageshow');
  Object.defineProperty(event, 'persisted', { value: true });
  window.dispatchEvent(event);
};

afterEach(() => {
  Reflect.deleteProperty(window, 'avibeNative');
  visibility = 'visible';
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('native direct closure watermark', () => {
  it('does nothing at all without the direct capability', async () => {
    const shell = installShell({ app: 1 });
    const addListener = vi.spyOn(document, 'addEventListener');
    const native = await load();

    expect(native.nativeDirectBaselineState()).toBe('absent');
    const handler = vi.fn();
    const listener = vi.fn();
    native.onNativeDirectClosure(handler)();
    native.subscribeNativeDirectBaseline(listener)();
    native.retryNativeDirectBaseline();
    hideAndShow();
    await settle();

    expect(shell.call).not.toHaveBeenCalled();
    expect(shell.on).not.toHaveBeenCalled();
    expect(addListener).not.toHaveBeenCalledWith('visibilitychange', expect.anything());
    expect(handler).not.toHaveBeenCalled();
    expect(listener).not.toHaveBeenCalled();
  });

  it('takes one baseline per document, shared by every consumer, and is ready only on a valid answer', async () => {
    const shell = installShell();
    const native = await load();
    const listener = vi.fn();

    expect(native.nativeDirectBaselineState()).toBe('pending');
    native.subscribeNativeDirectBaseline(listener);
    expect(native.nativeDirectBaselineState()).toBe('pending');
    native.onNativeDirectClosure(() => {});
    native.retryNativeDirectBaseline();
    expect(shell.statusCalls()).toBe(1);

    // An event is not a baseline.
    shell.emit(status(1));
    expect(native.nativeDirectBaselineState()).toBe('pending');

    await shell.answer(0, status(3));
    expect(native.nativeDirectBaselineState()).toBe('ready');
    expect(listener).toHaveBeenCalledTimes(1);
    expect(shell.statusCalls()).toBe(1);
  });

  it('does not act on closures from before its baseline, and keeps the highest one seen', async () => {
    const shell = installShell();
    const native = await load();
    const handler = vi.fn();
    native.onNativeDirectClosure(handler);

    // An event arrives while the baseline read is still out: no stream of this
    // document is open yet, so there is nothing to reconnect.
    shell.emit(status(2));
    await shell.answer(0, status(1));
    expect(handler).not.toHaveBeenCalled();

    shell.emit(status(2));
    expect(handler).not.toHaveBeenCalled();
    shell.emit(status(3));
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('reconnects once per higher closure, never for a preference change, whatever the order', async () => {
    const shell = installShell();
    const native = await load();
    const handler = vi.fn();
    native.onNativeDirectClosure(handler);
    await shell.answer(0, status(0, 0));

    // The preferred path changes: nothing reconnects.
    shell.emit(status(0, 1, 'direct'));
    shell.emit(status(0, 2, 'tunnel'));
    expect(handler).not.toHaveBeenCalled();

    shell.emit(status(1, 3, 'tunnel'));
    expect(handler).toHaveBeenCalledTimes(1);
    // A duplicate, and a late event with an older sequence.
    shell.emit(status(1, 3, 'tunnel'));
    shell.emit(status(1, 2, 'direct'));
    expect(handler).toHaveBeenCalledTimes(1);
    // A higher closure is acted on even when it carries an older sequence:
    // closure is compared only with closure.
    shell.emit(status(2, 1, 'direct'));
    expect(handler).toHaveBeenCalledTimes(2);

    // Not the contract's shape: nothing to act on.
    for (const payload of [
      null,
      { preferred: 'direct', sequence: 9 },
      { preferred: 'relay', sequence: 9, closure: 9 },
      { preferred: 'direct', sequence: 9, closure: '9' },
      { preferred: 'direct', sequence: 9, closure: -1 },
      { transport: 'direct', sequence: 9 },
    ]) {
      shell.emit(payload);
    }
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('catches up on resume when both closure emissions were dropped', async () => {
    const shell = installShell();
    const native = await load();
    const handler = vi.fn();
    native.onNativeDirectClosure(handler);
    await shell.answer(0, status(0));

    // The App was suspended. Neither the closure event nor the state the shell
    // emits again once its proxy is ready ever reaches this document. Only the
    // document's own lifecycle runs.
    hideAndShow();
    pageshowFromCache();
    // One read for this return; the shell holds its answer until ready.
    expect(shell.statusCalls()).toBe(2);
    expect(handler).not.toHaveBeenCalled();

    await shell.answer(1, status(1));
    expect(handler).toHaveBeenCalledTimes(1);

    // A dropped event delivered late changes nothing.
    shell.emit(status(1));
    expect(handler).toHaveBeenCalledTimes(1);
    // The baseline was not taken again: the next closure is acted on.
    shell.emit(status(2));
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('reconciles on the first visible return of a document that was installed while hidden', async () => {
    // The page loaded in the background: the hidden edge happened before the
    // module existed.
    visibility = 'hidden';
    const shell = installShell();
    const native = await load();
    const handler = vi.fn();
    native.onNativeDirectClosure(handler);
    await shell.answer(0, status(0));

    // Both closure emissions are dropped; only the real lifecycle runs.
    visibility = 'visible';
    document.dispatchEvent(new Event('visibilitychange'));
    pageshowFromCache();
    expect(shell.statusCalls()).toBe(2);
    await shell.answer(1, status(1));
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('reconciles once on a back-forward-cache return reported by pagehide and pageshow alone', async () => {
    const shell = installShell();
    const native = await load();
    const handler = vi.fn();
    native.onNativeDirectClosure(handler);
    await shell.answer(0, status(0));

    window.dispatchEvent(new Event('pagehide'));
    pageshowFromCache();
    pageshowFromCache();
    expect(shell.statusCalls()).toBe(2);
    await shell.answer(1, status(1));
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('acts on a closure seen first in a status read, and not again when its event follows', async () => {
    const shell = installShell();
    const native = await load();
    const handler = vi.fn();
    native.onNativeDirectClosure(handler);
    await shell.answer(0, status(0));

    hideAndShow();
    await shell.answer(1, status(4, 7));
    expect(handler).toHaveBeenCalledTimes(1);
    shell.emit(status(4, 7));
    shell.emit(status(4, 6));
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('does not let a resume read that never answers hold up the next return', async () => {
    const shell = installShell();
    const native = await load();
    const handler = vi.fn();
    native.onNativeDirectClosure(handler);
    await shell.answer(0, status(0));

    hideAndShow();
    expect(shell.statusCalls()).toBe(2);
    // Read 1 never answers. The next return reads again.
    hideAndShow();
    expect(shell.statusCalls()).toBe(3);

    await shell.answer(2, status(1));
    expect(handler).toHaveBeenCalledTimes(1);
    // The stuck read answers after all: the same closure, nothing more.
    await shell.answer(1, status(1));
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('is unavailable after a failed or malformed read, and ready only after a valid one', async () => {
    const shell = installShell();
    const native = await load();
    const handler = vi.fn();
    native.onNativeDirectClosure(handler);

    // An event shows closure 2 before the read fails.
    shell.emit(status(2));
    await shell.fail(0);
    expect(native.nativeDirectBaselineState()).toBe('unavailable');
    // Events alone never make it ready, and nothing acts on them.
    shell.emit(status(3));
    expect(native.nativeDirectBaselineState()).toBe('unavailable');
    expect(handler).not.toHaveBeenCalled();

    native.retryNativeDirectBaseline();
    expect(native.nativeDirectBaselineState()).toBe('pending');
    await shell.answer(1, { preferred: 'direct', sequence: 4 });
    expect(native.nativeDirectBaselineState()).toBe('unavailable');

    native.retryNativeDirectBaseline();
    await shell.answer(2, status(3));
    expect(native.nativeDirectBaselineState()).toBe('ready');
    expect(handler).not.toHaveBeenCalled();
    shell.emit(status(4));
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('becomes unavailable when the read is held past the wait, and ready when its valid answer comes', async () => {
    vi.useFakeTimers();
    const shell = installShell();
    const native = await load();
    const handler = vi.fn();
    native.onNativeDirectClosure(handler);

    await vi.advanceTimersByTimeAsync(1_499);
    expect(native.nativeDirectBaselineState()).toBe('pending');
    await vi.advanceTimersByTimeAsync(1);
    expect(native.nativeDirectBaselineState()).toBe('unavailable');

    // The shell was only holding its answer. It counts, as the baseline.
    await shell.answer(0, status(2));
    expect(native.nativeDirectBaselineState()).toBe('ready');
    expect(handler).not.toHaveBeenCalled();
    shell.emit(status(2));
    expect(handler).not.toHaveBeenCalled();
    shell.emit(status(3));
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('lets a user retry start one new attempt when the first read never settles', async () => {
    vi.useFakeTimers();
    const shell = installShell();
    const native = await load();
    const handler = vi.fn();
    native.onNativeDirectClosure(handler);
    await vi.advanceTimersByTimeAsync(1_500);
    expect(native.nativeDirectBaselineState()).toBe('unavailable');

    native.retryNativeDirectBaseline();
    native.retryNativeDirectBaseline();
    expect(shell.statusCalls()).toBe(2);
    expect(native.nativeDirectBaselineState()).toBe('pending');

    await shell.answer(1, status(1));
    expect(native.nativeDirectBaselineState()).toBe('ready');
    // The first read fails late: it cannot take ready away.
    await shell.fail(0);
    expect(native.nativeDirectBaselineState()).toBe('ready');
    expect(handler).not.toHaveBeenCalled();
    native.retryNativeDirectBaseline();
    expect(shell.statusCalls()).toBe(2);
  });

  it('lets the next visible return start one new attempt when the first read never settles', async () => {
    vi.useFakeTimers();
    const shell = installShell();
    const native = await load();
    const handler = vi.fn();
    native.onNativeDirectClosure(handler);
    await vi.advanceTimersByTimeAsync(1_500);
    expect(native.nativeDirectBaselineState()).toBe('unavailable');

    hideAndShow();
    pageshowFromCache();
    expect(shell.statusCalls()).toBe(2);

    // The old read answers first, then the new one: one baseline, no action.
    await shell.answer(0, status(1));
    expect(native.nativeDirectBaselineState()).toBe('ready');
    await shell.answer(1, status(1));
    expect(handler).not.toHaveBeenCalled();
    // A newer reply with a higher closure is a real closure after the baseline.
    hideAndShow();
    await shell.answer(2, status(2));
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('does not let an old attempt end a newer one', async () => {
    vi.useFakeTimers();
    const shell = installShell();
    const native = await load();
    expect(native.nativeDirectBaselineState()).toBe('pending');
    await vi.advanceTimersByTimeAsync(1_500);
    expect(native.nativeDirectBaselineState()).toBe('unavailable');
    native.retryNativeDirectBaseline();
    expect(shell.statusCalls()).toBe(2);
    expect(native.nativeDirectBaselineState()).toBe('pending');

    // The first attempt fails late, while the second is still out.
    await shell.fail(0);
    expect(native.nativeDirectBaselineState()).toBe('pending');
    // Only the second attempt's own wait can make it unavailable.
    await vi.advanceTimersByTimeAsync(1_500);
    expect(native.nativeDirectBaselineState()).toBe('unavailable');
  });

  it('stops calling a handler once it unsubscribes', async () => {
    const shell = installShell();
    const native = await load();
    const kept = vi.fn();
    const removed = vi.fn();
    native.onNativeDirectClosure(kept);
    const unsubscribe = native.onNativeDirectClosure(removed);
    await shell.answer(0, status(0));

    unsubscribe();
    shell.emit(status(1));
    expect(kept).toHaveBeenCalledTimes(1);
    expect(removed).not.toHaveBeenCalled();
  });

  it('gives a new document its own baseline, whatever the old one handled', async () => {
    const shell = installShell();
    const before = await load();
    const oldHandler = vi.fn();
    before.onNativeDirectClosure(oldHandler);
    await shell.answer(0, status(0));
    shell.emit(status(1));
    expect(oldHandler).toHaveBeenCalledTimes(1);

    // A new runtime or child window: a new document, a new copy of the module.
    const next = installShell();
    const after = await load();
    const newHandler = vi.fn();
    after.onNativeDirectClosure(newHandler);
    expect(next.statusCalls()).toBe(1);
    await next.answer(0, status(5));
    expect(newHandler).not.toHaveBeenCalled();
    next.emit(status(6));
    expect(newHandler).toHaveBeenCalledTimes(1);
  });
});
