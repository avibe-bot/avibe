import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const platform = vi.hoisted(() => ({
  isIosDevice: vi.fn(),
  isStandalonePwa: vi.fn(),
}));

vi.mock('./platform', () => platform);

import {
  deferRemoteAuthRedirect,
  REMOTE_AUTH_REQUIRED_EVENT,
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
