/** @vitest-environment jsdom */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WebPushControl } from './WebPushControl';

const { api, push } = vi.hoisted(() => ({
  api: { getWebPushStatus: vi.fn() },
  push: {
    disableWebPush: vi.fn(),
    enableWebPush: vi.fn(),
    getWebPushDeviceId: vi.fn(),
    getRememberedWebPushEndpoints: vi.fn(),
    getExistingWebPushSubscription: vi.fn(),
    getWebPushSupportState: vi.fn(),
    rememberWebPushEndpoint: vi.fn(),
    webPushSubscriptionUsesVapidKey: vi.fn(),
  },
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_read_instance: true } }),
}));
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/lib/webPush', () => push);

const oldEndpoint = 'https://push.example.test/sub/old';
const newEndpoint = 'https://push.example.test/sub/new';
const oldSubscription = {
  endpoint: oldEndpoint,
  toJSON: () => ({ endpoint: oldEndpoint }),
};
const newSubscription = {
  endpoint: newEndpoint,
  toJSON: () => ({ endpoint: newEndpoint }),
};

const status = (enabled: boolean, repairable = false) => ({
  ok: true,
  configured: true,
  public_key: 'new-key',
  subscription_count: enabled ? 1 : 0,
  current_subscription_enabled: enabled,
  current_subscription_repairable: repairable,
});

beforeEach(() => {
  vi.stubGlobal('Notification', { permission: 'granted' });
  push.getWebPushSupportState.mockReturnValue({
    supported: true,
    standalone: false,
    requiresStandalone: false,
  });
  push.getWebPushDeviceId.mockResolvedValue('device-1');
  push.getRememberedWebPushEndpoints.mockResolvedValue([oldEndpoint]);
  push.getExistingWebPushSubscription.mockResolvedValue(null);
  push.enableWebPush.mockResolvedValue(undefined);
  push.rememberWebPushEndpoint.mockResolvedValue(undefined);
  push.webPushSubscriptionUsesVapidKey.mockReturnValue(true);
  api.getWebPushStatus.mockResolvedValue(status(false));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('WebPushControl recovery', () => {
  it('recreates a missing subscription only when its remembered endpoint is still enabled', async () => {
    push.getExistingWebPushSubscription
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce(newSubscription);
    api.getWebPushStatus
      .mockResolvedValueOnce(status(true))
      .mockResolvedValueOnce(status(true));

    render(<WebPushControl />);

    await screen.findByText('workbench.inbox.notifications.enabled');
    expect(api.getWebPushStatus).toHaveBeenNthCalledWith(1, { endpoint: oldEndpoint });
    expect(push.enableWebPush).toHaveBeenCalledWith(api, {
      forceResubscribe: false,
      recoverOnly: true,
    });
    expect(api.getWebPushStatus).toHaveBeenNthCalledWith(2, {
      endpoint: newEndpoint,
      subscription: newSubscription.toJSON(),
      device_id: 'device-1',
      previous_endpoints: [oldEndpoint],
    });
  });

  it('keeps a missing subscription disabled after opt-out', async () => {
    render(<WebPushControl />);

    await screen.findByText('workbench.inbox.notifications.enable');
    expect(api.getWebPushStatus).toHaveBeenCalledWith({ endpoint: oldEndpoint });
    expect(push.enableWebPush).not.toHaveBeenCalled();
  });

  it('replaces an enabled subscription bound to an obsolete VAPID key', async () => {
    push.getExistingWebPushSubscription
      .mockResolvedValueOnce(oldSubscription)
      .mockResolvedValueOnce(newSubscription);
    push.webPushSubscriptionUsesVapidKey.mockReturnValue(false);
    api.getWebPushStatus
      .mockResolvedValueOnce(status(true))
      .mockResolvedValueOnce(status(true));

    render(<WebPushControl />);

    await screen.findByText('workbench.inbox.notifications.enabled');
    expect(push.webPushSubscriptionUsesVapidKey).toHaveBeenCalledWith(oldSubscription, 'new-key');
    expect(push.enableWebPush).toHaveBeenCalledWith(api, {
      forceResubscribe: true,
      recoverOnly: true,
    });
  });

  it('does not revive an opted-out subscription with an obsolete key', async () => {
    push.getExistingWebPushSubscription.mockResolvedValue(oldSubscription);
    push.webPushSubscriptionUsesVapidKey.mockReturnValue(false);

    render(<WebPushControl />);

    await screen.findByText('workbench.inbox.notifications.enable');
    expect(push.enableWebPush).not.toHaveBeenCalled();
  });

  it('shows disabled when a conditional repair fails after the initial status check', async () => {
    push.getExistingWebPushSubscription.mockResolvedValue(oldSubscription);
    push.webPushSubscriptionUsesVapidKey.mockReturnValue(false);
    push.enableWebPush.mockRejectedValue(new Error('recovery_not_authorized'));
    api.getWebPushStatus.mockResolvedValue(status(true));

    render(<WebPushControl />);

    await screen.findByText('workbench.inbox.notifications.enable');
    await waitFor(() => expect(push.enableWebPush).toHaveBeenCalledOnce());
  });

  it('does not report an initial enabled row after the replacement loses authorization', async () => {
    push.getExistingWebPushSubscription
      .mockResolvedValueOnce(oldSubscription)
      .mockResolvedValueOnce(newSubscription);
    push.webPushSubscriptionUsesVapidKey.mockReturnValue(false);
    api.getWebPushStatus
      .mockResolvedValueOnce(status(true))
      .mockResolvedValueOnce(status(false));

    render(<WebPushControl />);

    await screen.findByText('workbench.inbox.notifications.enable');
    expect(push.rememberWebPushEndpoint).not.toHaveBeenCalled();
  });
});
