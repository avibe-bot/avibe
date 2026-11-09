/** @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WebPushControl } from './WebPushControl';

const { api, push } = vi.hoisted(() => ({
  api: {
    getWebPushVapidPublicKey: vi.fn(),
    sendWebPushTest: vi.fn(),
  },
  push: {
    disableWebPush: vi.fn(),
    enableWebPush: vi.fn(),
    getExistingWebPushSubscription: vi.fn(),
    reconcileWebPush: vi.fn(),
  },
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
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/lib/webPush', () => push);
vi.mock('@/lib/native', () => ({
  hasNativeCapability: vi.fn(() => false),
  nativePushStatus: vi.fn(),
}));

beforeEach(() => {
  vi.stubGlobal('Notification', { permission: 'granted' });
  push.reconcileWebPush.mockResolvedValue({ support, status: 'disabled' });
  push.enableWebPush.mockResolvedValue(undefined);
  push.disableWebPush.mockResolvedValue(true);
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

  it('keeps a disabled subscription available for explicit enable', async () => {
    render(<WebPushControl />);

    const enable = await screen.findByText('workbench.inbox.notifications.enable');
    push.reconcileWebPush.mockResolvedValue({ support, status: 'enabled' });
    fireEvent.click(enable);

    await waitFor(() => expect(push.enableWebPush).toHaveBeenCalledWith(api));
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
});
