// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  vi.useRealTimers();
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

const dialog = (props: Partial<React.ComponentProps<typeof OAuthConnectDialog>> = {}) =>
  React.createElement(
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
        ...props,
      }),
    ),
  );

const renderDialog = (props: Partial<React.ComponentProps<typeof OAuthConnectDialog>> = {}) =>
  render(dialog(props));

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

    renderDialog();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Sign in|去登录/i }));
    await user.click(await screen.findByRole('button', { name: /^Retry$|^重试$/i }));

    await waitFor(() => expect(start).toHaveBeenCalledTimes(2));
    expect(start.mock.calls[0].slice(0, 2)).toEqual(['anthropic', 'native_cli']);
    expect(start.mock.calls[1]).toEqual(start.mock.calls[0]);
    expect(start.mock.calls[0][2]).toMatch(/^ofn_[a-z0-9]{16,64}$/);
  });
});

describe('OAuth failure class behavior', () => {
  it('keeps a held flow when its timeout reread is inconclusive', async () => {
    vi.useFakeTimers();
    const providerTab = {
      closed: false,
      close: vi.fn(),
      opener: {},
    };
    vi.spyOn(window, 'open').mockReturnValue(providerTab as unknown as Window);
    const reauth = vi.spyOn(modelsApi, 'reauthSource').mockResolvedValue({
      flow_id: 'oaf_timeout',
      intent: 'reauth',
      vendor: 'anthropic',
      channel: 'native_cli',
      state: 'awaiting_action',
      presentation: { expects: 'none' },
      expires_at: new Date(Date.now() - 61_000).toISOString(),
    });
    const status = vi
      .spyOn(modelsApi, 'getOAuthStatus')
      .mockRejectedValue(new TypeError('Failed to fetch'));
    renderDialog({ reauth: subscription() });

    await act(async () => Promise.resolve());
    expect(reauth).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTime(2_000));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^Retry$|^重试$/i }));
      await Promise.resolve();
    });

    expect(status).toHaveBeenCalledWith('oaf_timeout');
    expect(reauth).toHaveBeenCalledTimes(1);
    expect(providerTab.close).toHaveBeenCalledOnce();
  });

  it('ignores a held-flow reread rejection after its journey is retired', async () => {
    vi.useFakeTimers();
    let rejectStatus!: (reason: unknown) => void;
    const pendingStatus = new Promise<never>((_resolve, reject) => {
      rejectStatus = reject;
    });
    const reauth = vi
      .spyOn(modelsApi, 'reauthSource')
      .mockResolvedValueOnce({
        flow_id: 'oaf_retired',
        intent: 'reauth',
        vendor: 'anthropic',
        channel: 'native_cli',
        state: 'awaiting_action',
        presentation: { expects: 'none' },
        expires_at: new Date(Date.now() - 61_000).toISOString(),
      })
      .mockResolvedValueOnce({
        flow_id: 'oaf_replacement',
        intent: 'reauth',
        vendor: 'anthropic',
        channel: 'native_cli',
        state: 'awaiting_action',
        presentation: { expects: 'none' },
        expires_at: '2099-01-01T00:00:00Z',
      });
    vi.spyOn(modelsApi, 'getOAuthStatus').mockReturnValue(pendingStatus);
    vi.spyOn(modelsApi, 'cancelOAuth').mockResolvedValue(undefined);
    const retiredSource = subscription({ id: 'src_retired' });
    const replacementSource = subscription({ id: 'src_replacement' });
    const rendered = renderDialog({ reauth: retiredSource });

    await act(async () => Promise.resolve());
    await act(async () => vi.advanceTimersByTime(2_000));
    fireEvent.click(screen.getByRole('button', { name: /^Retry$|^重试$/i }));
    expect(modelsApi.getOAuthStatus).toHaveBeenCalledWith('oaf_retired');

    rendered.rerender(dialog({ open: false, reauth: retiredSource }));
    rendered.rerender(dialog({ reauth: replacementSource }));
    await act(async () => Promise.resolve());
    expect(reauth).toHaveBeenCalledTimes(2);

    await act(async () => {
      rejectStatus(new ApiCallError('source_not_found'));
      await Promise.resolve();
    });

    expect(screen.getByRole('button', { name: /^Cancel$|^取消$/i })).toBeTruthy();
  });

  it('does not offer Retry for an authoritative terminal failure', async () => {
    vi.spyOn(modelsApi, 'reauthSource').mockRejectedValue(new ApiCallError('source_not_found'));
    renderDialog({ reauth: subscription() });

    await screen.findByRole('button', { name: /^Close$|^关闭$/i });
    expect(screen.queryByRole('button', { name: /^Retry$|^重试$/i })).toBeNull();
  });

  it('offers Retry when the engine is unavailable', async () => {
    vi.spyOn(modelsApi, 'reauthSource').mockRejectedValue(new ApiCallError('engine_down'));
    renderDialog({ reauth: subscription() });

    expect(await screen.findByRole('button', { name: /^Retry$|^重试$/i })).toBeTruthy();
  });
});
