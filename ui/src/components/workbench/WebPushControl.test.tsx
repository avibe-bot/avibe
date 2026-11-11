/** @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WebPushControl } from './WebPushControl';

const { api, native, page, push, toast } = vi.hoisted(() => ({
  page: { reactivate: new Set<() => void>() },
  api: {
    getWebPushVapidPublicKey: vi.fn(),
    sendWebPushTest: vi.fn(),
  },
  native: {
    hasNativeCapability: vi.fn(),
    nativePushStatus: vi.fn(),
  },
  push: {
    disableWebPush: vi.fn(),
    enableWebPush: vi.fn(),
    getExistingWebPushSubscription: vi.fn(),
    reconcileWebPush: vi.fn(),
  },
  toast: { showToast: vi.fn() },
}));

const support = {
  supported: true as const,
  standalone: false,
  requiresStandalone: false,
};

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_read_instance: true } }),
}));
vi.mock('@/context/ToastContext', () => ({ useToast: () => toast }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/lib/webPush', () => push);
vi.mock('@/lib/native', () => native);
vi.mock('@/lib/pageActivity', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/pageActivity')>(),
  onPageReactivated: (listener: () => void) => {
    page.reactivate.add(listener);
    return () => page.reactivate.delete(listener);
  },
}));

beforeEach(() => {
  vi.resetAllMocks();
  vi.stubGlobal('Notification', { permission: 'granted' });
  native.hasNativeCapability.mockReturnValue(false);
  push.reconcileWebPush.mockResolvedValue({ support, status: 'disabled' });
  push.enableWebPush.mockResolvedValue(undefined);
  push.disableWebPush.mockResolvedValue({
    accepted: true,
    status: 'disabled',
    remoteRevoked: true,
    nativeRevoked: 'confirmed',
  });
  push.getExistingWebPushSubscription.mockResolvedValue(null);
  api.sendWebPushTest.mockResolvedValue({ ok: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('WebPushControl', () => {
  it('renders the shared reconciler result', async () => {
    push.reconcileWebPush.mockResolvedValue({ support, status: 'enabled' });

    render(<WebPushControl />);

    await screen.findByText('workbench.inbox.notifications.enabled');
    expect(push.reconcileWebPush).toHaveBeenCalledOnce();
    expect(push.reconcileWebPush).toHaveBeenCalledWith(api);
  });

  it('requests native push once from explicit enable', async () => {
    native.hasNativeCapability.mockReturnValue(true);
    render(<WebPushControl />);

    const enable = await screen.findByText('workbench.inbox.notifications.enable');
    push.reconcileWebPush.mockResolvedValue({ support, status: 'enabled' });
    fireEvent.click(enable);

    await waitFor(() => expect(push.enableWebPush).toHaveBeenCalledOnce());
    expect(push.enableWebPush).toHaveBeenCalledWith(api);
    await screen.findByText('workbench.inbox.notifications.enabled');
  });

  it('shows native push registering, without a test, until the instance has accepted it', async () => {
    native.hasNativeCapability.mockReturnValue(true);
    let finishEnable: (() => void) | undefined;
    push.enableWebPush.mockImplementation(() => new Promise<void>((resolve) => {
      finishEnable = resolve;
    }));
    render(<WebPushControl />);

    fireEvent.click(await screen.findByText('workbench.inbox.notifications.enable'));

    // Permission, relay, and the instance POST are still in flight.
    await screen.findByText('workbench.inbox.notifications.registering');
    expect(screen.queryByText('workbench.inbox.notifications.enabled')).toBeNull();
    expect(screen.queryByText('workbench.inbox.notifications.test')).toBeNull();

    push.reconcileWebPush.mockResolvedValue({ support, status: 'enabled' });
    finishEnable?.();
    await screen.findByText('workbench.inbox.notifications.enabled');
    expect(screen.getByText('workbench.inbox.notifications.test')).toBeTruthy();
  });

  it('says the device is still registering when a test finds no instance subscription', async () => {
    native.hasNativeCapability.mockReturnValue(true);
    api.getWebPushVapidPublicKey.mockResolvedValue({ public_key: 'AQIDBA' });
    native.nativePushStatus.mockResolvedValue({
      state: 'on',
      endpoint: 'https://avibe.bot/api/push/apns/native-current',
      keys: { p256dh: 'key', auth: 'auth' },
    });
    api.sendWebPushTest.mockRejectedValue(
      Object.assign(new Error('no_subscription'), { status: 404, code: 'no_subscription' }),
    );
    push.reconcileWebPush.mockResolvedValue({ support, status: 'enabled' });
    render(<WebPushControl />);

    fireEvent.click(await screen.findByText('workbench.inbox.notifications.test'));

    await waitFor(() => expect(toast.showToast).toHaveBeenCalledWith(
      'workbench.inbox.notifications.testRegistering',
      'warning',
    ));
    expect(toast.showToast).not.toHaveBeenCalledWith(
      'workbench.inbox.notifications.testFailed',
      'error',
    );
    await screen.findByText('workbench.inbox.notifications.registering');
  });

  it('keeps registering a waiting native subscription until the instance has it', async () => {
    vi.useFakeTimers();
    // Each step lets the timers due in it fire and React commit what they set.
    const wait = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));
    try {
      native.hasNativeCapability.mockReturnValue(true);
      push.reconcileWebPush
        .mockResolvedValueOnce({ support, status: 'registering' })
        .mockResolvedValueOnce({ support, status: 'registering' })
        .mockResolvedValue({ support, status: 'enabled' });
      render(<WebPushControl />);
      await wait(0);
      expect(screen.getByText('workbench.inbox.notifications.registering')).toBeTruthy();
      expect(push.reconcileWebPush).toHaveBeenCalledTimes(1);

      await wait(2_000);
      expect(push.reconcileWebPush).toHaveBeenCalledTimes(2);
      // Backing off: the next attempt waits longer than the first.
      await wait(2_000);
      expect(push.reconcileWebPush).toHaveBeenCalledTimes(2);
      await wait(2_000);
      expect(push.reconcileWebPush).toHaveBeenCalledTimes(3);
      expect(screen.getByText('workbench.inbox.notifications.enabled')).toBeTruthy();

      // Registered: no further attempts.
      await wait(60_000);
      expect(push.reconcileWebPush).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('reads native push again when the App comes back to the page', async () => {
    native.hasNativeCapability.mockReturnValue(true);
    render(<WebPushControl />);
    await screen.findByText('workbench.inbox.notifications.enable');

    // The foreground reconcile finished the registration while the App was away.
    push.reconcileWebPush.mockResolvedValue({ support, status: 'enabled' });
    act(() => {
      for (const listener of page.reactivate) listener();
    });

    await screen.findByText('workbench.inbox.notifications.enabled');
    expect(push.reconcileWebPush).toHaveBeenCalledTimes(2);
  });

  it('uses the shared reconciler after disabling', async () => {
    push.reconcileWebPush
      .mockResolvedValueOnce({ support, status: 'enabled' })
      .mockResolvedValueOnce({ support, status: 'disabled' });

    render(<WebPushControl />);

    const disable = await screen.findByText('workbench.inbox.notifications.disable');
    fireEvent.click(disable);

    await waitFor(() => expect(push.disableWebPush).toHaveBeenCalledWith(api));
    expect(push.reconcileWebPush).toHaveBeenCalledTimes(1);
    await screen.findByText('workbench.inbox.notifications.enable');
  });

  it('shows pending cleanup without claiming complete revocation', async () => {
    push.reconcileWebPush.mockResolvedValue({ support, status: 'enabled' });
    push.disableWebPush.mockResolvedValue({
      accepted: true,
      status: 'pending',
      remoteRevoked: true,
      nativeRevoked: 'pending',
    });

    render(<WebPushControl />);
    fireEvent.click(await screen.findByText('workbench.inbox.notifications.disable'));

    await screen.findByText('workbench.inbox.notifications.disablePending');
    expect(screen.getByText('workbench.inbox.notifications.enable')).toBeTruthy();
    expect(screen.queryByText('workbench.inbox.notifications.enabled')).toBeNull();
    expect(toast.showToast).toHaveBeenCalledWith(
      'workbench.inbox.notifications.disablePending',
      'warning',
    );
  });

  it('shows a recoverable failure when cleanup cannot be confirmed', async () => {
    push.reconcileWebPush.mockResolvedValue({ support, status: 'enabled' });
    push.disableWebPush
      .mockResolvedValueOnce({
        accepted: true,
        status: 'failed',
        remoteRevoked: true,
        nativeRevoked: 'failed',
      })
      .mockResolvedValueOnce({
        accepted: true,
        status: 'disabled',
        remoteRevoked: true,
        nativeRevoked: 'confirmed',
      });

    render(<WebPushControl />);
    fireEvent.click(await screen.findByText('workbench.inbox.notifications.disable'));

    await screen.findByText('workbench.inbox.notifications.disableFailed');
    expect(screen.getByText('workbench.inbox.notifications.enable')).toBeTruthy();
    fireEvent.click(screen.getByText('common.retry'));
    await waitFor(() => expect(push.disableWebPush).toHaveBeenCalledTimes(2));
    await waitFor(() => {
      expect(screen.queryByText('workbench.inbox.notifications.disableFailed')).toBeNull();
    });
    expect(toast.showToast).toHaveBeenCalledWith(
      'workbench.inbox.notifications.disableFailed',
      'error',
    );
  });

  it('keeps the enabled state when the opt-out could not be saved', async () => {
    push.reconcileWebPush.mockResolvedValue({ support, status: 'enabled' });
    push.disableWebPush.mockRejectedValue(new Error('storage blocked'));

    render(<WebPushControl />);
    fireEvent.click(await screen.findByText('workbench.inbox.notifications.disable'));

    await screen.findByText('workbench.inbox.notifications.disableNotSaved');
    expect(screen.getByText('workbench.inbox.notifications.enabled')).toBeTruthy();
    expect(screen.queryByText('workbench.inbox.notifications.enable')).toBeNull();
    expect(toast.showToast).toHaveBeenCalledWith(
      'workbench.inbox.notifications.disableNotSaved',
      'error',
    );
  });
});
