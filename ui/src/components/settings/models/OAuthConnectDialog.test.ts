// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import * as React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/context/ToastProvider';
import i18n from '@/i18n';
import { OAuthConnectDialog } from './OAuthConnectDialog';
import { ApiCallError, modelsApi } from './modelsApi';
import {
  initialSubscriptionChannel,
  nativeSubscriptionSlotTaken,
  recommendedSubscriptionChannel,
  subscriptionOptionOrder,
} from './subscriptionOptions';
import type { Source } from './types';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const subscription = (over: Partial<Source> = {}): Source => ({
  id: 'src_subscription',
  last_discovered_at: null,
  kind: 'subscription',
  vendor: 'anthropic',
  display_name: 'Subscription',
  protocol: 'anthropic',
  supply_channel: 'native_cli',
  billing: 'monthly',
  state: { status: 'standby' },
  models: [],
  ...over,
});

describe('add-subscription channel choice', () => {
  it('flips the recommendation and visible order by vendor', () => {
    expect(recommendedSubscriptionChannel('anthropic')).toBe('native_cli');
    expect(subscriptionOptionOrder('anthropic')).toEqual(['native_cli', 'hub']);
    expect(recommendedSubscriptionChannel('openai')).toBe('hub');
    expect(subscriptionOptionOrder('openai')).toEqual(['hub', 'native_cli']);
  });

  it('uses the recommended option while the native slot is free', () => {
    expect(initialSubscriptionChannel('anthropic', [])).toBe('native_cli');
    expect(initialSubscriptionChannel('openai', [])).toBe('hub');
  });

  it('keeps an occupied native row visible but selects the gateway option', () => {
    const sources = [subscription()];
    expect(nativeSubscriptionSlotTaken('anthropic', sources)).toBe(true);
    expect(initialSubscriptionChannel('anthropic', sources)).toBe('hub');
  });

  it('does not let another vendor or a gateway-held source occupy the slot', () => {
    const sources = [
      subscription({ vendor: 'openai' }),
      subscription({ id: 'src_hub', supply_channel: 'hub' }),
    ];
    expect(nativeSubscriptionSlotTaken('anthropic', sources)).toBe(false);
  });
});

describe('add-subscription start recovery', () => {
  it('reuses the exact nonce after a start response is lost', async () => {
    const start = vi.spyOn(modelsApi, 'startOAuth')
      .mockRejectedValueOnce(new ApiCallError('bad_response', undefined, false))
      .mockResolvedValueOnce({
        flow_id: 'oaf_recovered',
        client_nonce: 'server-echo-is-not-the-authority',
        vendor: 'anthropic',
        channel: 'native_cli',
        state: 'awaiting_action',
        presentation: { expects: 'paste_code' },
        expires_at: '2099-01-01T00:00:00Z',
      });

    render(React.createElement(
      ToastProvider,
      null,
      React.createElement(
        I18nextProvider,
        { i18n },
        React.createElement(OAuthConnectDialog, {
          open: true,
          vendor: 'anthropic',
          sources: [],
          onClose: vi.fn(),
          onConnected: vi.fn(),
        }),
      ),
    ));

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Sign in|去登录/i }));
    await user.click(await screen.findByRole('button', { name: /^Retry$|^重试$/i }));

    await waitFor(() => expect(start).toHaveBeenCalledTimes(2));
    expect(start.mock.calls[0].slice(0, 2)).toEqual(['anthropic', 'native_cli']);
    expect(start.mock.calls[1]).toEqual(start.mock.calls[0]);
    expect(start.mock.calls[0][2]).toMatch(/^ofn_[a-z0-9]{16,64}$/);
  });
});
