/** @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WebPushControl } from './WebPushControl';

const { api, native, push, toast } = vi.hoisted(() => ({
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
