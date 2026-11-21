/** @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  hasNativeCapability,
  hasNativeShare,
  hasNativeShareExport,
  hasNativeVoiceHold,
  isNativeShell,
  nativeAppInfo,
  nativeHapticImpact,
  nativePushStatusOutcome,
  nativePushSubscribe,
  nativePushUnsubscribeOutcome,
  nativeSharePresent,
  nativeShowAccount,
  nativeSystemScheduleKinds,
  nativeVoiceHoldAdopt,
  nativeVoiceHoldReject,
  nativeVoiceHoldSurfaces,
  nativeVoicePermission,
  onNativePushOpened,
  onNativeVoiceHold,
  openNativeExternal,
  NATIVE_RECONCILE_MAX_SESSIONS,
  reconcileNativeNotifications,
  setNativeAccountActionAvailable,
  setNativeBadge,
  setNativeOuterScrollLocked,
} from './native';

const installBridge = (
  overrides: Partial<NonNullable<Window['avibeNative']>> = {},
) => {
  const call = vi.fn(async (method: string) => {
    if (method === 'app.info') {
      return {
        contract: 1,
        platform: 'ios',
        shellVersion: '1.0 (1)',
        capabilities: {
          accountPresentation: 2,
          app: 1,
          haptics: 1,
          keyboardResizesViewport: 1,
          viewport: 1,
          voice: 1,
        },
      };
    }
    if (method === 'app.openExternal') return { opened: true };
    if (method === 'voice.permission') return { state: 'granted' };
    return null;
  });
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: {
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
      capabilities: {
        accountPresentation: 2,
        app: 1,
        haptics: 1,
        keyboardResizesViewport: 1,
        viewport: 1,
        voice: 1,
      },
      call,
      on: vi.fn(() => () => undefined),
      ...overrides,
    },
  });
  return call;
};

afterEach(() => {
  Reflect.deleteProperty(window, 'avibeNative');
  vi.restoreAllMocks();
});

describe('native shell bridge', () => {
  it('accepts only the versioned native contract', () => {
    expect(isNativeShell()).toBe(false);
    installBridge();
    expect(isNativeShell()).toBe(true);
    expect(hasNativeCapability('accountPresentation')).toBe(true);
    expect(hasNativeCapability('haptics')).toBe(true);
    expect(hasNativeCapability('keyboardResizesViewport')).toBe(true);
    expect(hasNativeCapability('haptics', 2)).toBe(false);

    installBridge({ contract: 2 as 1 });
    expect(isNativeShell()).toBe(false);
  });

  it('keeps all bridge calls behind typed helpers', async () => {
    const call = installBridge();

    await expect(nativeAppInfo()).resolves.toMatchObject({
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
    });
    await expect(openNativeExternal('https://avibe.bot')).resolves.toBe(true);
    await expect(nativeShowAccount()).resolves.toBe(true);
    await expect(setNativeAccountActionAvailable(true)).resolves.toBe(true);
    await expect(nativeHapticImpact('medium')).resolves.toBe(true);
    await expect(setNativeOuterScrollLocked(true)).resolves.toBe(true);
    await expect(nativeVoicePermission()).resolves.toBe('granted');
    expect(call.mock.calls.map(([method]) => method)).toEqual([
      'app.info',
      'app.openExternal',
      'app.showAccount',
      'app.setAccountActionAvailable',
      'haptics.impact',
      'viewport.setOuterScrollLocked',
      'voice.permission',
    ]);
  });

  it('uses harmless fallbacks when a capability is missing or a call fails', async () => {
    const missingCall = installBridge({
      capabilities: { app: 1 },
    });
    await expect(nativeShowAccount()).resolves.toBe(false);
    await expect(setNativeAccountActionAvailable(true)).resolves.toBe(false);
    expect(missingCall).not.toHaveBeenCalledWith('app.showAccount', {});

    installBridge({
      capabilities: { accountPresentation: 1, app: 1 },
      call: vi.fn(async () => {
        throw { code: 'failed', message: 'unavailable' };
      }),
    });

    await expect(nativeHapticImpact('light')).resolves.toBe(false);
    await expect(setNativeOuterScrollLocked(true)).resolves.toBe(false);
    await expect(nativeVoicePermission()).resolves.toBeNull();
    await expect(openNativeExternal('https://avibe.bot')).resolves.toBe(false);
    await expect(nativeShowAccount()).resolves.toBe(false);
    await expect(setNativeAccountActionAvailable(true)).resolves.toBe(false);
  });

  it('opens the account sheet on its Account page only where the shell presents instances', async () => {
    const b = installBridge({ capabilities: { accountPresentation: 2, app: 1, instancePresentation: 1 } });
    await expect(nativeShowAccount()).resolves.toBe(true);
    expect(b.mock.calls).toEqual([['app.showAccount', { start: 'account' }]]);

    // An older shell opens its account sheet as before; it would refuse the parameter.
    const older = installBridge({ capabilities: { accountPresentation: 2, app: 1 } });
    await expect(nativeShowAccount()).resolves.toBe(true);
    expect(older.mock.calls).toEqual([['app.showAccount', {}]]);

    const none = installBridge({ capabilities: { app: 1, instancePresentation: 1 } });
    await expect(nativeShowAccount()).resolves.toBe(false);
    expect(none).not.toHaveBeenCalled();

    installBridge({
      capabilities: { accountPresentation: 2, instancePresentation: 1 },
      call: vi.fn(async () => {
        throw { code: 'invalid_params', message: 'start' };
      }),
    });
    await expect(nativeShowAccount()).resolves.toBe(false);
  });

  it('sets the App badge to the count it is given, 0 included, and only where the shell has the badge capability', async () => {
    const call = installBridge({ capabilities: { app: 1, badge: 1 } });

    expect(setNativeBadge(3)).toBe(true);
    expect(setNativeBadge(0)).toBe(true);
    // A count the bridge would refuse is sent as the count it stands for.
    expect(setNativeBadge(2.7)).toBe(true);
    expect(setNativeBadge(-1)).toBe(true);
    expect(call.mock.calls).toEqual([
      ['badge.set', { count: 3 }],
      ['badge.set', { count: 0 }],
      ['badge.set', { count: 2 }],
      ['badge.set', { count: 0 }],
    ]);

    // A failing call is the shell's business, never the page's.
    installBridge({
      capabilities: { badge: 1 },
      call: vi.fn(async () => {
        throw { code: 'forbidden', message: 'not the selected instance' };
      }),
    });
    expect(setNativeBadge(1)).toBe(true);
    await Promise.resolve();

    // An older shell, and a plain browser, have no native badge.
    const older = installBridge({ capabilities: { app: 1, push: 1 } });
    expect(setNativeBadge(3)).toBe(false);
    expect(older).not.toHaveBeenCalled();
    Reflect.deleteProperty(window, 'avibeNative');
    expect(setNativeBadge(3)).toBe(false);
  });

  it('names the unread Sessions of a whole map to the App, or nothing when it cannot name them all', () => {
    const call = installBridge({ capabilities: { app: 1, notifications: 1 } });

    // Only counts above zero are unread.
    expect(reconcileNativeNotifications({ ses_a: 2, ses_b: 0, ses_c: 1 }, 1_000)).toBe(true);
    expect(reconcileNativeNotifications({}, 2_000)).toBe(true);
    expect(call.mock.calls).toEqual([
      ['notifications.reconcile', { unreadSessions: ['ses_a', 'ses_c'], asOf: 1_000 }],
      ['notifications.reconcile', { unreadSessions: [], asOf: 2_000 }],
    ]);

    // A Session the call could not name would read as read: nothing is sent.
    const tooMany = Object.fromEntries(
      Array.from({ length: NATIVE_RECONCILE_MAX_SESSIONS + 1 }, (_, i) => [`ses_${i}`, 1]),
    );
    const atLimit = Object.fromEntries(Object.entries(tooMany).slice(0, NATIVE_RECONCILE_MAX_SESSIONS));
    for (const unread of [
      tooMany,
      { ['s'.repeat(201)]: 1 },
      { ses_a: Number.NaN },
      { ses_a: '1' },
      null,
      undefined,
      ['ses_a'],
    ]) {
      expect(reconcileNativeNotifications(unread, 3_000)).toBe(false);
    }
    expect(reconcileNativeNotifications({ ses_a: 1 }, Number.NaN)).toBe(false);
    expect(call).toHaveBeenCalledTimes(2);
    expect(reconcileNativeNotifications(atLimit, 4_000)).toBe(true);
    expect(call).toHaveBeenCalledTimes(3);

    // An older shell, and a plain browser, have no native reconcile.
    const older = installBridge({ capabilities: { app: 1, badge: 1 } });
    expect(reconcileNativeNotifications({ ses_a: 1 }, 1_000)).toBe(false);
    expect(older).not.toHaveBeenCalled();
    Reflect.deleteProperty(window, 'avibeNative');
    expect(reconcileNativeNotifications({ ses_a: 1 }, 1_000)).toBe(false);
  });

  it.each([
    ['legacy confirmed', { revoked: true }, 'confirmed'],
    ['legacy deferred', { revoked: false }, 'pending'],
    ['confirmed', { revoked: true, revoke: 'confirmed' }, 'confirmed'],
    ['deferred', { revoked: false, revoke: 'pending' }, 'pending'],
    ['absent', { revoked: true, revoke: 'not_needed' }, 'not_needed'],
  ])(
    'keeps the %s native push revoke outcome distinct',
    async (_label, result, expected) => {
      installBridge({
        capabilities: { push: 1 },
        call: vi.fn(async () => result),
      });

      await expect(nativePushUnsubscribeOutcome()).resolves.toEqual({
        status: expected,
      });
    },
  );

  it('reports a native push bridge failure separately from deferred cleanup', async () => {
    installBridge({
      capabilities: { push: 1 },
      call: vi.fn(async () => {
        throw new Error('bridge unavailable');
      }),
    });

    await expect(nativePushUnsubscribeOutcome()).resolves.toEqual({
      status: 'failed',
    });
    await expect(nativePushStatusOutcome('AQIDBA')).resolves.toEqual({
      status: 'failed',
    });
  });

  it('preserves pending-revoke status facts from the native bridge', async () => {
    installBridge({
      capabilities: { push: 1 },
      call: vi.fn(async () => ({
        state: 'off',
        revokeState: 'pending',
        endpoint: 'https://avibe.bot/api/push/apns/pending',
      })),
    });

    await expect(nativePushStatusOutcome('AQIDBA')).resolves.toEqual({
      status: 'ok',
      value: {
        state: 'off',
        revokeState: 'pending',
        endpoint: 'https://avibe.bot/api/push/apns/pending',
      },
    });
  });

  it('sends force replacement as an additive subscribe option', async () => {
    const call = vi.fn(async (_method: string, params?: Record<string, unknown>) => ({
      endpoint: 'https://avibe.bot/api/push/apns/registration',
      keys: { p256dh: 'key', auth: 'auth' },
      params,
    }));
    installBridge({
      capabilities: { push: 1 },
      call,
    });

    await expect(nativePushSubscribe('AQIDBA')).resolves.toMatchObject({
      endpoint: 'https://avibe.bot/api/push/apns/registration',
    });
    await expect(nativePushSubscribe('AQIDBA', true)).resolves.toMatchObject({
      endpoint: 'https://avibe.bot/api/push/apns/registration',
    });

    expect(call.mock.calls).toEqual([
      [
        'push.subscribe',
        { vapidPublicKey: 'AQIDBA' },
      ],
      [
        'push.subscribe',
        { vapidPublicKey: 'AQIDBA', forceResubscribe: true },
      ],
    ]);
  });

  it('normalizes same-origin native notification paths and rejects foreign origins', () => {
    const on = vi.fn((event: string, handler: (payload: unknown) => void) => {
      if (event === 'push.opened') {
        handler({ url: '/chat/ses_1?from=push#latest' });
        handler({ url: 'https://avibe.bot/chat/ses_2' });
        handler({ url: '//evil.example/phish' });
        handler({ url: '/\\evil.example/phish' });
      }
      return () => undefined;
    });
    installBridge({
      capabilities: { push: 1 },
      on,
    });

    const handler = vi.fn();
    onNativePushOpened(handler);

    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith('/chat/ses_1?from=push#latest');
  });
});

// The App recognises a hold on the surfaces the page names (ios/Avibe/NativeHoldCapture.swift).
describe("the iOS App's own hold recognition", () => {
  const capabilities = { voice: 1, voiceCapture: 1, voiceHold: 1 };
  const surface = { id: 'composer', x: 0, y: 700, width: 390, height: 60 };

  it('is there only in an App that both recognises holds and records them', () => {
    expect(hasNativeVoiceHold()).toBe(false);
    installBridge({ capabilities: { voice: 1, voiceHold: 1 } });
    expect(hasNativeVoiceHold()).toBe(false);
    installBridge({ capabilities: { voice: 1, voiceCapture: 1 } });
    expect(hasNativeVoiceHold()).toBe(false);
    installBridge({ capabilities });
    expect(hasNativeVoiceHold()).toBe(true);
  });

  it('names surfaces with the threshold, takes them back with an empty list, and reads why the App does not follow', async () => {
    const call = installBridge({ capabilities });
    await expect(nativeVoiceHoldSurfaces([surface], 200)).resolves.toBe('ok');
    expect(call).toHaveBeenLastCalledWith('voice.holdSurfaces', { surfaces: [surface], holdMs: 200 });
    await expect(nativeVoiceHoldSurfaces([], 0)).resolves.toBe('ok');
    // No threshold with no surface: the App takes `holdMs` from 100 to 500 only.
    expect(call).toHaveBeenLastCalledWith('voice.holdSurfaces', { surfaces: [] });

    for (const [error, answer] of [
      [{ code: 'failed', message: 'consent' }, 'consent'],
      [{ code: 'failed', message: 'unwatched' }, 'unwatched'],
      [{ code: 'failed', message: 'superseded' }, 'superseded'],
      [{ code: 'failed', message: 'anything' }, 'failed'],
      [{ code: 'invalid_argument', message: 'consent' }, 'failed'],
      [new Error('gone'), 'failed'],
    ] as const) {
      call.mockRejectedValueOnce(error);
      await expect(nativeVoiceHoldSurfaces([surface], 200)).resolves.toBe(answer);
    }
  });

  it('asks an App without it for nothing', async () => {
    const call = installBridge();
    await expect(nativeVoiceHoldSurfaces([surface], 200)).resolves.toBe('failed');
    await nativeVoiceHoldReject('n1');
    await expect(nativeVoiceHoldAdopt('n1')).rejects.toMatchObject({ code: 'unavailable' });
    expect(call).not.toHaveBeenCalled();
  });

  it('takes a take, refuses one, and passes on a refusal to take', async () => {
    const call = installBridge({ capabilities });
    call.mockResolvedValueOnce({ sampleRate: 16_000, timings: { variant: 'early', liveAt: 61 } });
    await expect(nativeVoiceHoldAdopt('n1')).resolves.toEqual({ sampleRate: 16_000, timings: { variant: 'early', liveAt: 61 } });
    expect(call).toHaveBeenLastCalledWith('voice.holdAdopt', { take: 'n1' });
    call.mockRejectedValueOnce({ code: 'failed', message: 'gone' });
    await expect(nativeVoiceHoldAdopt('n2')).rejects.toEqual({ code: 'failed', message: 'gone' });
    call.mockRejectedValueOnce(new Error('bridge'));
    await expect(nativeVoiceHoldReject('n3')).resolves.toBeUndefined();
    expect(call).toHaveBeenLastCalledWith('voice.holdReject', { take: 'n3' });
  });

  it("hears the App's word about a press, and nothing that is not one", () => {
    let emit!: (payload: unknown) => void;
    const on = vi.fn((_event: string, handler: (payload: unknown) => void) => {
      emit = handler;
      return () => undefined;
    });
    installBridge({ capabilities, on });
    const handler = vi.fn();
    expect(onNativeVoiceHold(handler)).toBeTypeOf('function');
    expect(on).toHaveBeenCalledWith('voice.hold', expect.any(Function));

    emit({ phase: 'recognised', take: 'n1', surface: 'composer' });
    emit({ phase: 'failed', take: 'n2', surface: 'composer', step: 'engine:1' });
    emit({ phase: 'timings', take: 'n1', timings: { liveAt: 61 }, extra: true });
    for (const malformed of [null, 'recognised', { phase: 'recognised' }, { phase: 'other', take: 'n1' }, { phase: 'recognised', take: 7 }]) {
      emit(malformed);
    }
    expect(handler.mock.calls.map(([event]) => event)).toEqual([
      { phase: 'recognised', take: 'n1', surface: 'composer' },
      { phase: 'failed', take: 'n2', surface: 'composer', step: 'engine:1' },
      { phase: 'timings', take: 'n1', timings: { liveAt: 61 } },
    ]);
  });
});

describe('native Show sharing', () => {
  const capabilities = {
    version: 1,
    inbound_kinds: [],
    outbound_kinds: ['link', 'pdf', 'image'],
    max_bytes: { pdf: 52428800, image: 52428800, link: 32768 },
    max_total_bytes: 52428800,
    max_attachments: 1,
    supports_pending: false,
  };
  const shareBridge = (
    present: (params: Record<string, unknown>) => unknown,
    probe: () => unknown = () => capabilities,
    shareDraft = 1,
  ) => installBridge({
    capabilities: { app: 1, 'c1.share_draft': shareDraft },
    call: vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'share.capabilities') return probe();
      if (method === 'share.present') return present(params ?? {});
      return null;
    }) as NonNullable<Window['avibeNative']>['call'],
  });

  it('accepts only an answer to the same operation and kind', async () => {
    const echo = (extra: Record<string, unknown>) => (params: Record<string, unknown>) => ({ ...params, ...extra });
    const cases: [(params: Record<string, unknown>) => unknown, object][] = [
      [echo({ status: 'completed' }), { kind: 'pdf', status: 'completed' }],
      [echo({ status: 'cancelled', error: { code: 'ignored' } }), { kind: 'pdf', status: 'cancelled' }],
      [echo({ status: 'expired' }), { status: 'expired', error: { code: 'expired' } }],
      [echo({ status: 'failed', error: { code: 'not_visible' } }), { status: 'failed', error: { code: 'not_visible' } }],
      [echo({ operationId: 'another', status: 'completed' }), { status: 'failed', error: { code: 'invalid_response' } }],
      [echo({ kind: 'image', status: 'completed' }), { status: 'failed', error: { code: 'invalid_response' } }],
      [() => null, { status: 'failed', error: { code: 'invalid_response' } }],
      [() => { throw { code: 'busy', message: 'busy' }; }, { status: 'failed', error: { code: 'busy' } }],
    ];
    for (const [present, expected] of cases) {
      shareBridge(present);
      const result = await nativeSharePresent('pdf', 'ses-1');
      expect(result).toMatchObject(expected);
      if (result.status === 'completed' || result.status === 'cancelled') expect(result.error).toBeUndefined();
    }
  });

  it('never presents without the capability, a matching probe, or a bounded Session', async () => {
    const refusals: [() => ReturnType<typeof installBridge>, string, string][] = [
      [() => installBridge(), 'ses-1', 'capability_mismatch'],
      [() => shareBridge(() => null, () => capabilities, 0), 'ses-1', 'capability_mismatch'],
      [() => shareBridge(() => null, () => ({ ...capabilities, version: 2 })), 'ses-1', 'capability_mismatch'],
      [() => shareBridge(() => null, () => ({ ...capabilities, outbound_kinds: ['link'] })), 'ses-1', 'capability_mismatch'],
      [() => shareBridge(() => null, () => { throw new Error('gone'); }), 'ses-1', 'unavailable'],
      [() => shareBridge(() => null), '', 'invalid_request'],
      [() => shareBridge(() => null), 'x'.repeat(201), 'invalid_request'],
    ];
    for (const [install, sessionId, code] of refusals) {
      const call = install();
      await expect(nativeSharePresent('image', sessionId)).resolves.toMatchObject({
        kind: 'image',
        status: 'failed',
        error: { code },
      });
      expect(call).not.toHaveBeenCalledWith('share.present', expect.anything());
    }
  });
});


describe('system schedule supported kinds', () => {
  it.each([['alarm'], ['reminder'], []])('returns the native kinds %j', async (...kinds) => {
    installBridge({ capabilities: { systemSchedule: 1 }, call: vi.fn().mockResolvedValue({ contract: 1, supportedKinds: kinds }) });
    expect(await nativeSystemScheduleKinds()).toEqual(kinds);
  });

  it.each([null, { contract: 1 }, { contract: 2, supportedKinds: ['alarm'] }, { contract: 1, supportedKinds: ['calendar'] }])(
    'allows only bound recovery when capabilities are unknown: %j', async (result) => {
      installBridge({ capabilities: { systemSchedule: 1 }, call: vi.fn().mockResolvedValue(result) });
      expect(await nativeSystemScheduleKinds()).toEqual([]);
    },
  );

  it('preserves bound recovery when the capability probe is unavailable', async () => {
    installBridge({ capabilities: { systemSchedule: 1 }, call: vi.fn().mockRejectedValue(new Error('bridge unavailable')) });
    expect(await nativeSystemScheduleKinds()).toEqual([]);
  });
});

describe('desktop Show sharing', () => {
  const capabilities = {
    version: 1,
    inbound_kinds: [],
    outbound_kinds: ['link', 'pdf', 'image'],
    max_bytes: { pdf: 52428800, image: 52428800, link: 32768 },
    max_total_bytes: 52428800,
    max_attachments: 1,
    supports_pending: false,
  };
  const define = (name: string, value: unknown) =>
    Object.defineProperty(window, name, { configurable: true, value });
  const installDesktop = (marker: unknown = 1, present = (params: Record<string, unknown>) => ({ ...params, status: 'cancelled' })) => {
    const invoke = vi.fn(async (command: string, args?: { request?: Record<string, unknown> }) => {
      if (command !== 'desktop_show_share') throw new Error('unexpected command');
      const { method, ...params } = args?.request ?? {};
      if (method === 'share.capabilities') return capabilities;
      if (method === 'share.present') return present(params);
      return null;
    });
    define('__AVIBE_DESKTOP_SHELL__', true);
    define('__AVIBE_DESKTOP_SHOW_SHARE__', marker);
    define('__TAURI_INTERNALS__', { invoke });
    return invoke;
  };
  afterEach(() => {
    for (const name of ['__AVIBE_DESKTOP_SHELL__', '__AVIBE_DESKTOP_SHOW_SHARE__', '__TAURI_INTERNALS__']) {
      Reflect.deleteProperty(window, name);
    }
  });

  it('sends the same requests through the one desktop share command', async () => {
    const invoke = installDesktop();
    expect(hasNativeShare()).toBe(true);
    const result = await nativeSharePresent('image', 'ses-1');
    expect(result).toMatchObject({ kind: 'image', status: 'cancelled' });
    expect(invoke).toHaveBeenNthCalledWith(1, 'desktop_show_share', { request: { method: 'share.capabilities' } });
    expect(invoke).toHaveBeenNthCalledWith(2, 'desktop_show_share', {
      request: { method: 'share.present', operationId: result.operationId, kind: 'image', sessionId: 'ses-1' },
    });
  });

  it('offers nothing without the shell marker, and fails without its command channel', async () => {
    for (const marker of [null, true, 2]) {
      const invoke = installDesktop(marker);
      expect(hasNativeShare()).toBe(false);
      await expect(nativeSharePresent('link', 'ses-1')).resolves.toMatchObject({ status: 'failed', error: { code: 'capability_mismatch' } });
      expect(invoke).not.toHaveBeenCalled();
    }
    // The marker is what the page shows; a share without the command channel fails, untried.
    installDesktop();
    define('__TAURI_INTERNALS__', {});
    expect(hasNativeShare()).toBe(true);
    await expect(nativeSharePresent('link', 'ses-1')).resolves.toMatchObject({ status: 'failed', error: { code: 'unavailable' } });
  });

  it('recognises a native Show window, which carries the app-window marker instead of the shell marker', async () => {
    const invoke = installDesktop();
    Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
    expect(hasNativeShare()).toBe(false);
    define('__AVIBE_NATIVE_APP_WINDOW__', true);
    expect(hasNativeShare()).toBe(true);
    await expect(nativeSharePresent('link', 'ses-1')).resolves.toMatchObject({ status: 'cancelled' });
    expect(invoke).toHaveBeenCalledWith('desktop_show_share', expect.anything());
    Reflect.deleteProperty(window, '__AVIBE_NATIVE_APP_WINDOW__');
  });

  it('exports only from a native Show window, never from the Workbench', () => {
    installDesktop();
    expect(hasNativeShareExport()).toBe(false);
    define('__AVIBE_DESKTOP_SHOW_EXPORT__', 1);
    expect(hasNativeShareExport()).toBe(true);
    Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHOW_EXPORT__');
  });

  it('keeps the phone bridge first when both are present', async () => {
    const invoke = installDesktop();
    const call = vi.fn(async (method: string, params?: Record<string, unknown>) =>
      method === 'share.capabilities' ? capabilities : { ...params, status: 'completed' });
    installBridge({
      capabilities: { app: 1, 'c1.share_draft': 1 },
      call: call as NonNullable<Window['avibeNative']>['call'],
    });
    await expect(nativeSharePresent('link', 'ses-1')).resolves.toMatchObject({ status: 'completed' });
    expect(call).toHaveBeenCalledWith('share.present', expect.objectContaining({ kind: 'link' }));
    expect(invoke).not.toHaveBeenCalled();
  });
});
