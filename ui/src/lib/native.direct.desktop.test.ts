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

type Request = { token: string; id: string; method: string; params: Record<string, unknown> };

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
  preference: { mode: 'automatic', reverted: null },
});

afterEach(() => {
  window.removeEventListener('beforeunload', onLeave);
  document.removeEventListener('click', onClick);
  for (const name of ['avibeNative', '__AVIBE_DIRECT_DELIVER__', '__AVIBE_DIRECT_EARLY__']) {
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

    await expect(bridge.call('direct.preference')).resolves.toEqual({ mode: 'automatic', reverted: null });
    expect(requests).toHaveLength(0);

    await expect(bridge.call('direct.setPreference', { mode: 'direct' })).rejects.toMatchObject({
      code: 'invalid_argument',
    });
    expect(requests).toHaveLength(0);
    const changed = bridge.call('direct.setPreference', { mode: 'tunnel' });
    expect(requests[0]).toMatchObject({ method: 'direct.setPreference', params: { mode: 'tunnel' } });
    deliver({
      preference: { mode: 'tunnel', reverted: null },
      reply: { id: requests[0].id, result: { mode: 'tunnel', reverted: null } },
    });
    await expect(changed).resolves.toEqual({ mode: 'tunnel', reverted: null });
    await expect(bridge.call('direct.preference')).resolves.toEqual({ mode: 'tunnel', reverted: null });
  });

  it('asks the shell for the choice when none was delivered', async () => {
    await load();
    const asked = window.avibeNative!.call('direct.preference');
    expect(requests[0]).toMatchObject({ method: 'direct.preference' });
    deliver({ reply: { id: requests[0].id, result: { mode: 'tunnel', reverted: null } } });
    await expect(asked).resolves.toEqual({ mode: 'tunnel', reverted: null });
  });

  it('is not installed over a bridge that is already there', () => {
    const existing = { contract: 1, platform: 'ios' };
    Object.defineProperty(window, 'avibeNative', { configurable: true, value: existing });
    install('window-token', '3.2.7', true);
    expect(window.avibeNative).toBe(existing);
  });
});
