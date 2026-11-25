/** @vitest-environment jsdom */

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';

// The desktop app's `direct` bridge, as its windows of another computer's
// instance get it: the script the shell injects
// (desktop/src-tauri/src/direct/page_bridge.js) under this page's own
// `native.ts`. The shell is played here: it reads the request out of the link
// the script clicks, and answers by the script's delivery function, as
// `desktop/src-tauri/src/direct/bridge.rs` does.
type NativeModule = typeof import('./native');

const script = new Function(`return (${readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../../../desktop/src-tauri/src/direct/page_bridge.js'),
  'utf8',
)})`)() as (token: string, version: string, asksBeforeLeaving: boolean) => void;

// What the shell evaluates in the window to deliver
// (desktop/src-tauri/src/direct/page_delivery.js), with the arguments
// `bridge.rs` gives it: the instance's origin, the mark of the document that
// asked (none for a state), the message, and whether it may wait for a bridge.
const delivery = new Function(`return (${readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../../../desktop/src-tauri/src/direct/page_delivery.js'),
  'utf8',
)})`)() as (origin: string, asker: string | null, message: unknown, keep: boolean) => void;

/**
 * Runs the script as a new document would. Its two properties cannot be
 * redefined, which a real document never needs; here every case is a new
 * document in the same window, so for the length of this call alone they are
 * made removable.
 */
const install = (token: string, version: string, asksBeforeLeaving: boolean) => {
  const page = window;
  const defineProperty = Object.defineProperty;
  Object.defineProperty = ((target: object, key: PropertyKey, descriptor: PropertyDescriptor) =>
    defineProperty(target, key, target === page ? { ...descriptor, configurable: true } : descriptor)
  ) as typeof Object.defineProperty;
  try {
    script(token, version, asksBeforeLeaving);
  } finally {
    Object.defineProperty = defineProperty;
  }
};

type Request = { token: string; id: string; document: string; method: string; params: Record<string, unknown> };

const requests: Request[] = [];
let leaveQuestions = 0;
/** What the WebView does with the link: `beforeunload` first where it asks, then the shell refuses. */
let asksBeforeLeaving = true;
const onClick = (event: Event) => {
  const link = event.target as HTMLAnchorElement;
  if (!(link instanceof HTMLAnchorElement) || !link.href.startsWith('avibe-app://direct?')) return;
  event.preventDefault();
  if (asksBeforeLeaving) window.dispatchEvent(new Event('beforeunload', { cancelable: true }));
  requests.push(JSON.parse(new URL(link.href).searchParams.get('request') ?? 'null') as Request);
};
const onLeave = () => {
  leaveQuestions += 1;
};

const deliver = (message: unknown) => {
  (window as unknown as { __AVIBE_DIRECT_DELIVER__: (message: unknown) => void }).__AVIBE_DIRECT_DELIVER__(message);
};

const settle = async () => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

/** `webView2`: the system whose WebView asks the page before the shell can refuse a link. */
const load = async (early?: unknown, webView2 = true): Promise<NativeModule> => {
  vi.resetModules();
  asksBeforeLeaving = webView2;
  if (early) (window as unknown as Record<string, unknown>).__AVIBE_DIRECT_EARLY__ = early;
  install('window-token', '3.2.7', webView2);
  // The page's own guard, registered after the script as any page listener is.
  window.addEventListener('beforeunload', onLeave);
  document.addEventListener('click', onClick);
  return import('./native');
};

const state = (
  sequence: number,
  path: 'relay' | 'ipv4' | 'ipv6' | null,
  closure = 0,
  scope?: 'local' | 'global',
) => ({
  status: {
    preferred: path ? 'direct' : 'tunnel',
    sequence,
    closure,
    path,
    ...(scope ? { path_scope: scope } : {}),
  },
  preference: { mode: 'automatic', pendingRelaunch: false, reverted: null },
});

afterEach(() => {
  window.removeEventListener('beforeunload', onLeave);
  document.removeEventListener('click', onClick);
  for (const name of ['avibeNative', '__AVIBE_DIRECT_DELIVER__', '__AVIBE_DIRECT_EARLY__', '__AVIBE_DIRECT_DOCUMENT__']) {
    Reflect.deleteProperty(window, name);
  }
  requests.length = 0;
  leaveQuestions = 0;
  asksBeforeLeaving = true;
  vi.useRealTimers();
});

describe('the desktop direct bridge', () => {
  it('answers direct, and nothing else of the phone App', async () => {
    const native = await load();

    expect(native.isNativeShell()).toBe(false);
    expect(native.hasNativeCapability('direct')).toBe(false);
    expect(native.nativeDirectAvailable()).toBe(true);
    expect(native.nativeDirectHandoverAvailable()).toBe(true);
    expect(native.nativeDirectPathReported()).toBe(true);
    await expect(native.nativeAppLockStatus()).resolves.toBeNull();
    await expect(window.avibeNative?.call('push.status')).rejects.toMatchObject({ code: 'unsupported' });
  });

  it('answers the baseline at once with the tunnel when the shell has said nothing', async () => {
    const native = await load();

    expect(native.nativeDirectBaselineState()).toBe('pending');
    await settle();
    expect(native.nativeDirectBaselineState()).toBe('ready');
    expect(native.nativeDirectPreference()).toBe('tunnel');
    expect(requests).toHaveLength(0);
  });

  it('takes what the shell delivered before the script ran', async () => {
    const native = await load(state(7, 'ipv4', 4, 'local'));
    native.nativeDirectBaselineState();
    await settle();

    expect(native.nativeDirectPreference()).toBe('direct');
    expect(native.nativeDirectPathKind()).toBe('ipv4');
    await expect(native.readNativeDirectPath()).resolves.toMatchObject({
      preferred: 'direct',
      path: 'ipv4',
      scope: 'local',
      rttMs: null,
      lastConnection: null,
    });
  });

  it('tells each change of path once, in order, and names the relay as the relay', async () => {
    const native = await load();
    const seen: Array<[string, string | null]> = [];
    native.onNativeDirectPreference((preferred, path) => seen.push([preferred, path]));
    await settle();
    seen.length = 0;

    deliver(state(1, 'relay'));
    deliver(state(2, 'ipv6', 0, 'global'));
    // A delivery that is not newer changes nothing, in what is read either.
    deliver(state(2, 'ipv4'));
    deliver(state(1, null));
    await expect(native.readNativeDirectPath()).resolves.toMatchObject({ path: 'ipv6', scope: 'global' });
    deliver(state(3, null));

    expect(seen).toEqual([['direct', 'relay'], ['direct', 'ipv6'], ['tunnel', null]]);
    await expect(native.readNativeDirectPath()).resolves.toMatchObject({ preferred: 'tunnel', path: null, scope: null });
  });

  it('counts closures from the document\'s own first delivery', async () => {
    const native = await load();
    const closures = vi.fn();
    native.onNativeDirectClosure(closures);
    await settle();

    // The window lost its path five times before this document loaded.
    deliver(state(9, 'ipv4', 5));
    expect(closures).not.toHaveBeenCalled();
    deliver(state(10, null, 6));
    expect(closures).toHaveBeenCalledTimes(1);
  });

  it('asks the shell for a hand-over with the window\'s token, and reads its answer', async () => {
    const native = await load(state(1, 'ipv4'));
    await settle();

    const answer = native.nativeDirectHandover();
    expect(requests).toHaveLength(1);
    expect(requests[0]).toMatchObject({ token: 'window-token', method: 'direct.handover' });
    deliver({ reply: { id: requests[0].id, result: { closed: 2, kept: 1 } } });
    await expect(answer).resolves.toEqual({ closed: 2, kept: 1 });

    const skipped = native.nativeDirectHandover();
    deliver({ reply: { id: 'not-this-document', result: { closed: 9, kept: 9 } } });
    deliver({ reply: { id: requests[1].id, result: { closed: 0, kept: 0, skipped: 'other_documents' } } });
    await expect(skipped).resolves.toEqual({ closed: 0, kept: 0, skipped: 'other_documents' });
  });

  it('does not let its request put the page\'s leave question to the user', async () => {
    const native = await load(state(1, 'ipv4'));
    await settle();

    void native.nativeDirectHandover();
    expect(requests).toHaveLength(1);
    expect(leaveQuestions).toBe(0);

    // A real attempt to leave still reaches the page's guard.
    window.dispatchEvent(new Event('beforeunload', { cancelable: true }));
    expect(leaveQuestions).toBe(1);
  });

  it('keeps the page\'s guard for a leave that comes after a request the WebView did not ask about', async () => {
    vi.useFakeTimers();
    const native = await load(state(1, 'ipv4'));
    asksBeforeLeaving = false;

    void native.nativeDirectHandover();
    await vi.advanceTimersByTimeAsync(50);
    window.dispatchEvent(new Event('beforeunload', { cancelable: true }));
    expect(leaveQuestions).toBe(1);
  });

  it('keeps nothing from the page where the WebView never asks before the shell refuses', async () => {
    const native = await load(state(1, 'ipv4'), false);

    void native.nativeDirectHandover();
    expect(requests).toHaveLength(1);
    // A real leave at the very next moment is the page's to answer.
    window.dispatchEvent(new Event('beforeunload', { cancelable: true }));
    expect(leaveQuestions).toBe(1);
  });

  it('gives up on a request the shell never answers', async () => {
    vi.useFakeTimers();
    const native = await load(state(1, 'ipv4'));

    const answer = native.nativeDirectHandover();
    await vi.advanceTimersByTimeAsync(6000);
    await expect(answer).resolves.toBeNull();
    const raw = window.avibeNative!.call('direct.handover');
    const failed = expect(raw).rejects.toMatchObject({ code: 'unavailable' });
    await vi.advanceTimersByTimeAsync(10_000);
    await failed;
  });

  it('reads the choice the shell delivered, and asks the shell to change it', async () => {
    await load(state(1, null));
    const bridge = window.avibeNative!;

    await expect(bridge.call('direct.preference')).resolves.toEqual({ mode: 'automatic', pendingRelaunch: false, reverted: null });
    expect(requests).toHaveLength(0);

    // Not a choice the app has.
    await expect(bridge.call('direct.setPreference', { mode: 'relay' })).rejects.toMatchObject({
      code: 'invalid_argument',
    });
    expect(requests).toHaveLength(0);
    const changed = bridge.call('direct.setPreference', { mode: 'tunnel' });
    expect(requests[0]).toMatchObject({ method: 'direct.setPreference', params: { mode: 'tunnel' } });
    deliver({
      preference: { mode: 'tunnel', pendingRelaunch: false, reverted: null },
      reply: { id: requests[0].id, result: { mode: 'tunnel', pendingRelaunch: false, reverted: null } },
    });
    await expect(changed).resolves.toEqual({ mode: 'tunnel', pendingRelaunch: false, reverted: null });
    await expect(bridge.call('direct.preference')).resolves.toEqual({ mode: 'tunnel', pendingRelaunch: false, reverted: null });
  });

  it('keeps the newest choice of path, whichever delivery arrives first', async () => {
    await load({ ...state(1, null), preference: { mode: 'automatic', pendingRelaunch: false, reverted: null }, preferenceOrder: 3 });
    const bridge = window.avibeNative!;
    const choice = () => bridge.call('direct.preference');

    // The user chose the tunnel: the answer to that carries the newer order.
    deliver({ preference: { mode: 'tunnel', pendingRelaunch: false, reverted: null }, preferenceOrder: 4 });
    await expect(choice()).resolves.toEqual({ mode: 'tunnel', pendingRelaunch: false, reverted: null });
    // A state that was read before the choice arrives after it: its status
    // is taken, its older choice is not.
    deliver({ ...state(2, 'relay'), preference: { mode: 'automatic', pendingRelaunch: false, reverted: null }, preferenceOrder: 3 });
    await expect(choice()).resolves.toEqual({ mode: 'tunnel', pendingRelaunch: false, reverted: null });
    await expect(bridge.call('direct.status')).resolves.toMatchObject({ sequence: 2, path: 'relay' });
    // The same order again is the same choice; a newer one replaces it.
    deliver({ preference: { mode: 'tunnel', pendingRelaunch: false, reverted: null }, preferenceOrder: 4 });
    deliver({ preference: { mode: 'automatic', pendingRelaunch: false, reverted: null }, preferenceOrder: 5 });
    await expect(choice()).resolves.toEqual({ mode: 'automatic', pendingRelaunch: false, reverted: null });
    expect(requests).toHaveLength(0);
  });

  it('offers the third choice, and tells the page of a choice that changes while it is open', async () => {
    await load({ ...state(1, null), preference: { mode: 'automatic', pendingRelaunch: false, reverted: null }, preferenceOrder: 3 });
    const bridge = window.avibeNative!;
    // `directPreference` 2: "direct and relay only" can be chosen.
    expect(bridge.capabilities).toMatchObject({ direct: 3, directPreference: 2 });
    const heard: unknown[] = [];
    const told: unknown[] = [];
    const listen = (event: Event) => heard.push((event as CustomEvent).detail);
    window.addEventListener('avibe:native:direct.preferenceChanged', listen);
    const stop = bridge.on('direct.preferenceChanged', (choice: unknown) => told.push(choice));
    try {
      // The first choice a document is given is read, not announced.
      expect(heard).toEqual([]);

      const chosen = bridge.call('direct.setPreference', { mode: 'direct' });
      expect(requests[0]).toMatchObject({ method: 'direct.setPreference', params: { mode: 'direct' } });
      const direct = { mode: 'direct', pendingRelaunch: false, reverted: null };
      deliver({ preference: direct, preferenceOrder: 4, reply: { id: requests[0].id, result: direct } });
      await expect(chosen).resolves.toEqual(direct);
      expect(heard).toEqual([direct]);

      // The app set it back itself: when, why and from which choice.
      const reverted = { mode: 'automatic', pendingRelaunch: false, reverted: { at: 1795000000000, reason: 'unreachable', from: 'direct' } };
      deliver({ preference: reverted, preferenceOrder: 5 });
      expect(heard).toEqual([direct, reverted]);
      await expect(bridge.call('direct.preference')).resolves.toEqual(reverted);
      // The same choice again, and an older one arriving late, announce nothing.
      deliver({ preference: reverted, preferenceOrder: 6 });
      deliver({ preference: direct, preferenceOrder: 4 });
      expect(heard).toEqual([direct, reverted]);
      expect(told).toEqual(heard);
    } finally {
      window.removeEventListener('avibe:native:direct.preferenceChanged', listen);
      stop();
    }
  });

  it('asks the shell for the choice when none was delivered', async () => {
    await load();
    const asked = window.avibeNative!.call('direct.preference');
    expect(requests[0]).toMatchObject({ method: 'direct.preference' });
    deliver({ reply: { id: requests[0].id, result: { mode: 'tunnel', pendingRelaunch: false, reverted: null } } });
    await expect(asked).resolves.toEqual({ mode: 'tunnel', pendingRelaunch: false, reverted: null });
  });

  it('asks the shell for a measurement, with nothing but the window\'s token and the document\'s mark', async () => {
    await load(state(1, 'ipv4'));
    const bridge = window.avibeNative!;
    expect(bridge.capabilities).toMatchObject({ direct: 3, directPreference: 2, directMeasure: 1 });

    // Whatever the page passes, nothing of it goes to the shell: what is
    // measured is the instance the window was made for.
    const measured = bridge.call('direct.measure', { host: 'other.example', url: 'https://other.example/health' });
    expect(requests).toHaveLength(1);
    expect(requests[0]).toEqual({
      token: 'window-token',
      id: requests[0].id,
      document: (window as unknown as { __AVIBE_DIRECT_DOCUMENT__: string }).__AVIBE_DIRECT_DOCUMENT__,
      method: 'direct.measure',
      params: {},
    });
    expect(requests[0].document).toMatch(/^[0-9a-f-]{36}$/);
    const result = {
      measuredAt: 1795000000000,
      basis: 'warm-https-health',
      samples: 5,
      current: { kind: 'ipv4', scope: 'local', relayRegion: null, ok: 5, medianMs: 3.2, maxMs: 4.1, error: null },
      tunnel: { kind: 'tunnel', scope: null, relayRegion: null, ok: 4, medianMs: 48.5, maxMs: 61, error: 'timeout' },
    };
    delivery(window.location.origin, requests[0].document, { reply: { id: requests[0].id, result } }, false);
    await expect(measured).resolves.toEqual(result);
  });

  it('rejects a measurement the shell refuses with the shell\'s word, and waits 35 s for one it does not answer', async () => {
    vi.useFakeTimers();
    await load(state(1, 'ipv4'));
    const bridge = window.avibeNative!;

    // Another measurement is running, in this window or another.
    const second = bridge.call('direct.measure');
    delivery(window.location.origin, requests[0].document, { reply: { id: requests[0].id, error: { code: 'busy' } } }, false);
    await expect(second).rejects.toMatchObject({ code: 'busy' });

    // The shell bounds a measurement at 30 s: the page waits past that, and
    // past the 10 s every other request is given.
    let settled: string | null = null;
    const slow = bridge.call('direct.measure').then(
      () => { settled = 'resolved'; },
      (error: { code: string }) => { settled = error.code; },
    );
    await vi.advanceTimersByTimeAsync(34_900);
    expect(settled).toBeNull();
    await vi.advanceTimersByTimeAsync(200);
    await slow;
    expect(settled).toBe('unavailable');
    // An answer that comes after that is no one's.
    delivery(window.location.origin, requests[1].document, { reply: { id: requests[1].id, result: {} } }, false);
    expect(settled).toBe('unavailable');
  });

  it('hands an answer to the document that asked and to no document that has taken its place', async () => {
    await load(state(1, 'ipv4'));
    const asked = window.avibeNative!.call('direct.measure');
    let outcome: unknown = 'pending';
    void asked.then((value) => { outcome = value; }, (error) => { outcome = error; });
    const request = requests[0];
    const answer = { reply: { id: request.id, result: { basis: 'warm-https-health' } } };
    type Page = { __AVIBE_DIRECT_DELIVER__: (message: unknown) => void; __AVIBE_DIRECT_DOCUMENT__: string };
    const page = window as unknown as Page;

    // Another origin's document in the window: nothing, as before.
    const first = vi.fn(page.__AVIBE_DIRECT_DELIVER__);
    Object.defineProperty(window, '__AVIBE_DIRECT_DELIVER__', { value: first, configurable: true });
    delivery('https://elsewhere.example', request.document, answer, false);
    // Another document's mark: nothing.
    delivery(window.location.origin, crypto.randomUUID(), answer, false);
    expect(first).not.toHaveBeenCalled();
    await settle();
    expect(outcome).toBe('pending');

    // The window loads the instance again (a reload, a navigation within
    // it): a new document, with its own bridge and its own mark, at the same
    // origin. The old document's answer arrives now.
    for (const name of ['avibeNative', '__AVIBE_DIRECT_DELIVER__', '__AVIBE_DIRECT_DOCUMENT__']) {
      Reflect.deleteProperty(window, name);
    }
    install('window-token', '3.2.7', true);
    expect(page.__AVIBE_DIRECT_DOCUMENT__).not.toBe(request.document);
    const second = vi.fn(page.__AVIBE_DIRECT_DELIVER__);
    Object.defineProperty(window, '__AVIBE_DIRECT_DELIVER__', { value: second, configurable: true });
    delivery(window.location.origin, request.document, answer, false);
    // Not handed to the new document at all: its receiver is never called
    // with it, whatever it would have made of the id.
    expect(second).not.toHaveBeenCalled();
    expect((window as unknown as Record<string, unknown>).__AVIBE_DIRECT_EARLY__).toBeUndefined();

    // A document with no bridge (it has not started, or is not the
    // instance's page): an answer is not kept for it either.
    Reflect.deleteProperty(window, '__AVIBE_DIRECT_DELIVER__');
    Reflect.deleteProperty(window, '__AVIBE_DIRECT_DOCUMENT__');
    delivery(window.location.origin, request.document, answer, false);
    expect((window as unknown as Record<string, unknown>).__AVIBE_DIRECT_EARLY__).toBeUndefined();
    // A state is: it is for whatever document of the instance is there.
    delivery(window.location.origin, null, state(9, 'relay'), true);
    expect((window as unknown as Record<string, unknown>).__AVIBE_DIRECT_EARLY__).toEqual(state(9, 'relay'));

    // And the new document's own request is answered as ever.
    Reflect.deleteProperty(window, 'avibeNative');
    install('window-token', '3.2.7', true);
    const again = window.avibeNative!.call('direct.handover');
    const own = requests[requests.length - 1];
    expect(own.document).toBe(page.__AVIBE_DIRECT_DOCUMENT__);
    delivery(window.location.origin, own.document, { reply: { id: own.id, result: { closed: 0, kept: 0 } } }, false);
    await expect(again).resolves.toEqual({ closed: 0, kept: 0 });
  });

  it('is not installed over a bridge that is already there', () => {
    const existing = { contract: 1, platform: 'ios' };
    Object.defineProperty(window, 'avibeNative', { configurable: true, value: existing });
    install('window-token', '3.2.7', true);
    expect(window.avibeNative).toBe(existing);
  });
});
