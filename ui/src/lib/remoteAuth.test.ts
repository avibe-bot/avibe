import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const platform = vi.hoisted(() => ({
  isIosDevice: vi.fn(),
  isStandalonePwa: vi.fn(),
}));

vi.mock('./platform', () => platform);

import {
  checkRemoteAuthForPath,
  deferRemoteAuthRedirect,
  reportRemoteAuthorizationState,
  remoteLoginPath,
  shouldBypassSetupForRemoteOwner,
  REMOTE_AUTH_REQUIRED_EVENT,
  REMOTE_AUTH_STATE_EVENT,
  shouldDeferRemoteAuthRedirect,
} from './remoteAuth';

describe('remote auth navigation', () => {
  beforeEach(() => {
    platform.isIosDevice.mockReturnValue(false);
    platform.isStandalonePwa.mockReturnValue(false);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('requires an explicit action in an iOS standalone PWA', () => {
    expect(shouldDeferRemoteAuthRedirect({ ios: true, standalone: true })).toBe(true);
  });

  it.each([
    { ios: true, standalone: false },
    { ios: false, standalone: true },
    { ios: false, standalone: false },
  ])('keeps automatic login outside an iOS standalone PWA: %o', (context) => {
    expect(shouldDeferRemoteAuthRedirect(context)).toBe(false);
  });

  it('starts login through the dedicated endpoint while preserving the requested route', () => {
    expect(remoteLoginPath('/chat/session-1?view=activity')).toBe(
      '/auth/login?next=%2Fchat%2Fsession-1%3Fview%3Dactivity',
    );
  });

  it.each(['https://evil.example/path', '//evil.example/path', 'chat/session-1'])(
    'falls back to the app root for an unsafe login target: %s',
    (target) => {
      expect(remoteLoginPath(target)).toBe('/auth/login?next=%2F');
    },
  );

  it.each([
    '/settings/diagnostics',
    '/settings/diagnostics/logs',
    '/admin/logs',
    '/admin/settings/diagnostics',
  ])(
    'keeps remote session authentication enabled while bypassing setup checks for %s',
    async (path) => {
      const getSession = vi.fn(async () => ({ remote: true, authenticated: false }));

      await expect(checkRemoteAuthForPath(path, getSession)).resolves.toEqual({
        session: { remote: true, authenticated: false },
        loginRequired: true,
        checkSetup: false,
      });
      expect(getSession).toHaveBeenCalledOnce();
    },
  );

  it('checks setup after an authenticated remote session on regular app routes', async () => {
    await expect(checkRemoteAuthForPath(
      '/inbox',
      async () => ({ remote: true, authenticated: true }),
    )).resolves.toEqual({
      session: { remote: true, authenticated: true },
      loginRequired: false,
      checkSetup: true,
    });
  });

  it('signals AuthGuard instead of navigating automatically', () => {
    platform.isIosDevice.mockReturnValue(true);
    platform.isStandalonePwa.mockReturnValue(true);
    const dispatchEvent = vi.fn();
    vi.stubGlobal('window', { dispatchEvent });

    expect(deferRemoteAuthRedirect()).toBe(true);
    expect(dispatchEvent).toHaveBeenCalledOnce();
    expect(dispatchEvent.mock.calls[0]?.[0]).toBeInstanceOf(Event);
    expect(dispatchEvent.mock.calls[0]?.[0].type).toBe(REMOTE_AUTH_REQUIRED_EVENT);
  });
});

describe('remote authorization recovery', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    reportRemoteAuthorizationState('current');
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('keeps probing with bounded backoff until authorization recovers', async () => {
    const dispatchEvent = vi.fn();
    vi.stubGlobal('window', {
      clearTimeout: globalThis.clearTimeout,
      dispatchEvent,
      setTimeout: globalThis.setTimeout,
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        json: async () => ({
          remote: true,
          authenticated: true,
          authorization_state: 'unavailable',
        }),
      })
      .mockResolvedValueOnce({
        json: async () => ({
          remote: true,
          authenticated: true,
          authorization_state: 'current',
        }),
      });
    vi.stubGlobal('fetch', fetchMock);

    reportRemoteAuthorizationState('unavailable');
    await vi.advanceTimersByTimeAsync(1_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1_999);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(dispatchEvent.mock.calls.some(([event]) => (
      event.type === REMOTE_AUTH_STATE_EVENT && event.detail?.state === 'current'
    ))).toBe(true);
  });

  it('replaces a pending backoff with an immediate manual retry', async () => {
    const dispatchEvent = vi.fn();
    vi.stubGlobal('window', {
      clearTimeout: globalThis.clearTimeout,
      dispatchEvent,
      setTimeout: globalThis.setTimeout,
    });
    const fetchMock = vi.fn().mockResolvedValue({
      json: async () => ({
        remote: true,
        authenticated: true,
        authorization_state: 'current',
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    reportRemoteAuthorizationState('unavailable');
    reportRemoteAuthorizationState('changed');
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock).toHaveBeenCalledOnce();
  });
});

describe('setup bypass for remote runtime access', () => {
  it('bypasses setup only for an authenticated Instance Owner', () => {
    expect(
      shouldBypassSetupForRemoteOwner({
        remote: true,
        authenticated: true,
        capabilities: { can_manage_instance: true },
      }),
    ).toBe(true);
    expect(
      shouldBypassSetupForRemoteOwner({
        remote: true,
        authenticated: true,
        capabilities: { can_manage_instance: false },
      }),
    ).toBe(false);
    expect(
      shouldBypassSetupForRemoteOwner({
        remote: false,
        authenticated: true,
        capabilities: { can_manage_instance: true },
      }),
    ).toBe(false);
  });
});
