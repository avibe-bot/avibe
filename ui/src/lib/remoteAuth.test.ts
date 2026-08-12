import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const platform = vi.hoisted(() => ({
  isIosDevice: vi.fn(),
  isStandalonePwa: vi.fn(),
}));

vi.mock('./platform', () => platform);

import {
  canAdministerMemory,
  canArchiveProjects,
  canEditAgentDefinitions,
  canEditProjectDefaultAgent,
  canEditProjectInstructions,
  canManageSkills,
  canManageVaultSecrets,
  canRegisterWebPush,
  canUseHarness,
  checkRemoteAuthForPath,
  deferRemoteAuthRedirect,
  remoteLoginPath,
  shouldBypassSetupForRemoteOwner,
  REMOTE_AUTH_REQUIRED_EVENT,
  shouldDeferRemoteAuthRedirect,
} from './remoteAuth';

describe('agent definition editing', () => {
  it('keeps the Agent editor available on a local instance', () => {
    expect(canEditAgentDefinitions({ remote: false })).toBe(true);
  });

  it('renders a read-only catalog on a remote instance', () => {
    expect(canEditAgentDefinitions({ remote: true })).toBe(false);
  });
});

// Every Workbench control whose endpoint the remote HTTP policy classifies
// local-only needs this locality check on top of its capability, because
// `can_manage_instance` / `can_manage_agents` / `can_manage_projects` stay true
// for a remote Instance owner.
describe('local-only workbench controls', () => {
  const predicates = {
    canManageSkills,
    canManageVaultSecrets,
    canRegisterWebPush,
    canUseHarness,
    canArchiveProjects,
    canEditProjectInstructions,
    canEditProjectDefaultAgent,
    canAdministerMemory,
  };

  it.each(Object.entries(predicates))('keeps %s available on a local instance', (_name, predicate) => {
    expect(predicate({ remote: false })).toBe(true);
  });

  it.each(Object.entries(predicates))('withholds %s on a remote instance', (_name, predicate) => {
    expect(predicate({ remote: true })).toBe(false);
  });
});

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

  it.each(['/admin/logs', '/admin/settings/diagnostics'])(
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

describe('setup bypass for remote owners', () => {
  it('bypasses the setup wizard only for an authenticated remote owner', () => {
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
