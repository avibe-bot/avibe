/** @vitest-environment jsdom */
import { render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WebPushReconciler } from './WebPushReconciler';

const { api, native, activity, reconcile } = vi.hoisted(() => ({
  api: {},
  native: { hasNativeCapability: vi.fn() },
  activity: { onPageReactivated: vi.fn(), readPageActivity: vi.fn() },
  reconcile: { reconcileWebPush: vi.fn(), restoreWebPushAfterBinding: vi.fn() },
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
  activity.readPageActivity.mockReturnValue(true);
  reconcile.restoreWebPushAfterBinding.mockResolvedValue(false);
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

  describe('in a browser', () => {
    beforeEach(() => {
      native.hasNativeCapability.mockReturnValue(false);
    });

    it('takes back what binding stopped when the page opens in front and each time it comes back', async () => {
      let reactivate: (() => void) | undefined;
      activity.onPageReactivated.mockImplementation((callback: () => void) => {
        reactivate = callback;
        return () => undefined;
      });

      render(<WebPushReconciler />);
      await waitFor(() => expect(reconcile.restoreWebPushAfterBinding).toHaveBeenCalledOnce());
      expect(reconcile.restoreWebPushAfterBinding).toHaveBeenCalledWith(api);

      reactivate?.();
      expect(reconcile.restoreWebPushAfterBinding).toHaveBeenCalledTimes(2);
      // The full reconcile stays the notification control's: nothing else changes here.
      expect(reconcile.reconcileWebPush).not.toHaveBeenCalled();
    });

    it('waits for the foreground when the page was loaded in a background tab', async () => {
      activity.readPageActivity.mockReturnValue(false);
      let reactivate: (() => void) | undefined;
      activity.onPageReactivated.mockImplementation((callback: () => void) => {
        reactivate = callback;
        return () => undefined;
      });

      render(<WebPushReconciler />);
      await Promise.resolve();
      expect(reconcile.restoreWebPushAfterBinding).not.toHaveBeenCalled();

      reactivate?.();
      expect(reconcile.restoreWebPushAfterBinding).toHaveBeenCalledOnce();
    });

    it.each([
      ['notifications are switched off in preferences', () => { state.enabled = false; }],
      ['the viewer may not read the instance', () => { state.canReadInstance = false; }],
      ['preferences are not read yet', () => { state.loaded = false; }],
    ])('does nothing while %s', async (_label, arrange) => {
      arrange();

      render(<WebPushReconciler />);
      await Promise.resolve();

      expect(reconcile.restoreWebPushAfterBinding).not.toHaveBeenCalled();
    });
  });
});
