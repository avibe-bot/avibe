// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import * as React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/context/ToastProvider';
import i18n from '@/i18n';
import { OAuthConnectDialog } from './OAuthConnectDialog';
import { ApiCallError, modelsApi, type OAuthResult } from './modelsApi';
import { disposeProviderTab } from './providerTab';
import {
  initialSubscriptionChannel,
  nativeSubscriptionSlotTaken,
  recommendedSubscriptionChannel,
  subscriptionOptionOrder,
} from './subscriptionOptions';
import type { Source } from './types';

afterEach(() => {
  cleanup();
  disposeProviderTab('cleanup');
  disposeProviderTab('cleanup');
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

const providerTab = () => {
  const tab = {
    closed: false,
    close: vi.fn(),
    opener: {} as unknown,
    location: { href: '' },
  };
  tab.close.mockImplementation(() => {
    tab.closed = true;
  });
  return tab;
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
};

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

  it('keeps the Retry tab across effect cleanup and navigates the replacement flow', async () => {
    const authUrl = 'https://provider.example/retry';
    const tab = providerTab();
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
    const reauth = vi
      .spyOn(modelsApi, 'reauthSource')
      .mockRejectedValueOnce(new ApiCallError('engine_down'))
      .mockResolvedValueOnce({
        flow_id: 'oaf_retry',
        intent: 'reauth',
        vendor: 'anthropic',
        channel: 'native_cli',
        state: 'awaiting_action',
        presentation: { expects: 'paste_code', auth_url: authUrl },
        expires_at: '2099-01-01T00:00:00Z',
      });
    renderDialog({ reauth: subscription() });

    await userEvent.click(await screen.findByRole('button', { name: /^Retry$|^重试$/i }));

    await waitFor(() => expect(reauth).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(tab.location.href).toBe(authUrl));
    expect(tab.close).not.toHaveBeenCalled();
  });

  it('disposes the blank tab when acquisition is refused', async () => {
    const tab = providerTab();
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
    vi.spyOn(modelsApi, 'startOAuth').mockRejectedValue(new ApiCallError('engine_down'));
    renderDialog();

    await userEvent.click(screen.getByRole('button', { name: /Sign in|去登录/i }));
    await screen.findByRole('button', { name: /^Retry$|^重试$/i });

    expect(tab.close).toHaveBeenCalledOnce();
    expect(tab.location.href).toBe('');
  });

  it('disposes without navigating an already-terminal nonce replay', async () => {
    const authUrl = 'https://provider.example/stale';
    const tab = providerTab();
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
    const terminal = {
      flow_id: 'oaf_terminal',
      client_nonce: 'ofn_terminal',
      vendor: 'anthropic',
      channel: 'native_cli' as const,
      state: 'failed' as const,
      presentation: { expects: 'paste_code' as const, auth_url: authUrl },
      error_key: 'settings.models.oauth.error.generic',
      expires_at: '2099-01-01T00:00:00Z',
    };
    vi.spyOn(modelsApi, 'startOAuth').mockResolvedValue(terminal);
    const status = vi.spyOn(modelsApi, 'getOAuthStatus').mockResolvedValue({
      flow: terminal,
      created: null,
      repaired: null,
    });
    renderDialog();

    await userEvent.click(screen.getByRole('button', { name: /Sign in|去登录/i }));
    await waitFor(() => expect(status).toHaveBeenCalledWith(terminal.flow_id));

    expect(tab.close).toHaveBeenCalledOnce();
    expect(tab.location.href).toBe('');
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

  it.each([
    {
      label: 'failure',
      finish: (pending: ReturnType<typeof deferred<OAuthResult>>, _flow: OAuthResult['flow']) =>
        pending.reject(new ApiCallError('engine_down')),
    },
    {
      label: 'terminal response',
      finish: (pending: ReturnType<typeof deferred<OAuthResult>>, flow: OAuthResult['flow']) =>
        pending.resolve({ flow: { ...flow, state: 'failed' }, created: null, repaired: null }),
    },
  ])('keeps Retry\'s tab when the timed-out flow ignores a late submit $label', async ({ finish }) => {
    vi.useFakeTimers();
    const tab = providerTab();
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
    const expiredFlow: OAuthResult['flow'] = {
      flow_id: 'oaf_late_submit',
      intent: 'reauth',
      vendor: 'anthropic',
      channel: 'native_cli',
      state: 'awaiting_action',
      presentation: { expects: 'paste_code' },
      expires_at: new Date(Date.now() - 61_000).toISOString(),
    };
    const replacementUrl = 'https://provider.example/late-submit-retry';
    const replacementFlow: OAuthResult['flow'] = {
      ...expiredFlow,
      flow_id: 'oaf_late_submit_replacement',
      presentation: { expects: 'paste_code', auth_url: replacementUrl },
      expires_at: '2099-01-01T00:00:00Z',
    };
    const reauth = vi.spyOn(modelsApi, 'reauthSource')
      .mockResolvedValueOnce(expiredFlow)
      .mockResolvedValueOnce(replacementFlow);
    const submit = deferred<OAuthResult>();
    vi.spyOn(modelsApi, 'submitOAuth').mockReturnValue(submit.promise);
    const reread = deferred<OAuthResult>();
    vi.spyOn(modelsApi, 'getOAuthStatus').mockReturnValue(reread.promise);
    vi.spyOn(modelsApi, 'cancelOAuth').mockResolvedValue(undefined);
    renderDialog({ reauth: subscription() });

    await act(async () => Promise.resolve());
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'auth-code' } });
    fireEvent.click(screen.getByRole('button', { name: /^Submit$|^提交$/i }));
    expect(modelsApi.submitOAuth).toHaveBeenCalledWith(expiredFlow.flow_id, 'auth-code');
    await act(async () => vi.advanceTimersByTime(2_000));

    fireEvent.click(screen.getByRole('button', { name: /^Retry$|^重试$/i }));
    expect(modelsApi.getOAuthStatus).toHaveBeenCalledWith(expiredFlow.flow_id);
    await act(async () => {
      finish(submit, expiredFlow);
      await Promise.resolve();
    });
    expect(tab.close).not.toHaveBeenCalled();

    await act(async () => {
      reread.resolve({ flow: expiredFlow, created: null, repaired: null });
      await Promise.resolve();
    });
    expect(reauth).toHaveBeenCalledTimes(2);
    expect(tab.location.href).toBe(replacementUrl);
    expect(tab.close).not.toHaveBeenCalled();
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
