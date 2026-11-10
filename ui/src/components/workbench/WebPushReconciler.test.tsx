/** @vitest-environment jsdom */
import { render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WebPushReconciler } from './WebPushReconciler';

const { api, native, activity, reconcile } = vi.hoisted(() => ({
  api: {},
  native: { hasNativeCapability: vi.fn() },
  activity: { onPageReactivated: vi.fn() },
  reconcile: { reconcileWebPush: vi.fn() },
}));

const state = vi.hoisted(() => ({
  canReadInstance: true,
  loaded: true,
  enabled: true,
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({
    capabilities: { can_read_instance: state.canReadInstance },
  }),
}));
vi.mock('@/context/NotificationPreferencesContext', () => ({
  useNotificationPreferences: () => ({
    preferences: { enabled: state.enabled, bots: true, sessions: true },
    loaded: state.loaded,
  }),
}));
vi.mock('@/lib/native', () => native);
vi.mock('@/lib/pageActivity', () => activity);
vi.mock('@/lib/webPush', () => reconcile);

beforeEach(() => {
  state.canReadInstance = true;
  state.loaded = true;
  state.enabled = true;
  native.hasNativeCapability.mockReturnValue(true);
  activity.onPageReactivated.mockReturnValue(() => undefined);
  reconcile.reconcileWebPush.mockResolvedValue({
    support: { supported: true, standalone: true, requiresStandalone: false },
    status: 'enabled',
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('WebPushReconciler', () => {
  it('reconciles on instance load and every page reactivation', async () => {
    let reactivate: (() => void) | undefined;
    activity.onPageReactivated.mockImplementation((callback: () => void) => {
      reactivate = callback;
      return () => undefined;
    });

    render(<WebPushReconciler />);
    await waitFor(() => expect(reconcile.reconcileWebPush).toHaveBeenCalledOnce());

    reactivate?.();
    await waitFor(() => expect(reconcile.reconcileWebPush).toHaveBeenCalledTimes(2));
    expect(reconcile.reconcileWebPush).toHaveBeenCalledWith(api);
  });

  it('serializes load and foreground when the first reconciliation is pending', async () => {
    let release: (() => void) | undefined;
    const underlying = vi.fn();
    let pending: Promise<void> | null = null;
    reconcile.reconcileWebPush.mockImplementation(
      () => {
        if (!pending) {
          underlying();
          pending = new Promise<void>((resolve) => {
            release = resolve;
          });
        }
        return pending;
      },
    );
    let reactivate: (() => void) | undefined;
    activity.onPageReactivated.mockImplementation((callback: () => void) => {
      reactivate = callback;
      return () => undefined;
    });

    render(<WebPushReconciler />);
    reactivate?.();

    expect(reconcile.reconcileWebPush).toHaveBeenCalledTimes(2);
    expect(underlying).toHaveBeenCalledOnce();
    release?.();
  });

  it.each([
    ['preference disabled', 'enabled', false],
    ['native capability absent', 'disabled', true],
  ])('does not call native reconcile when %s', async (_label, mode, preferenceEnabled) => {
    if (mode === 'disabled') {
      native.hasNativeCapability.mockReturnValue(false);
    } else {
      state.enabled = preferenceEnabled;
    }

    render(<WebPushReconciler />);
    await Promise.resolve();

    expect(reconcile.reconcileWebPush).not.toHaveBeenCalled();
  });
});
