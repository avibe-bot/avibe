// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { failRegionRead, readyRegion } from './regionRead';
import { SourcesCard } from './SourcesCard';
import type { Source } from './types';

const retained: Source = {
  id: 'src_retained',
  last_discovered_at: null,
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: 'Retained source',
  protocol: 'anthropic',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [],
};

afterEach(cleanup);

describe('SourcesCard footer', () => {
  it('exposes the upstream info note to keyboard activation and Escape dismissal', async () => {
    const user = userEvent.setup();
    render(
      <I18nextProvider i18n={i18n}>
        <SourcesCard read={readyRegion([])} onRetry={vi.fn()} onOpenSource={vi.fn()} onAddApiKey={vi.fn()} onAddSubscription={vi.fn()} />
      </I18nextProvider>,
    );

    const info = screen.getByRole('button', { name: /What the gateway is|什么是网关/i });
    await user.tab();
    expect(document.activeElement).toBe(info);
    await user.keyboard('[Enter]');
    expect(await screen.findByText(/dispatch layer|调度/i)).toBeTruthy();
    await user.keyboard('[Escape]');
    await waitFor(() => expect(screen.queryByText(/dispatch layer|调度/i)).toBeNull());
  });

  it('draws the two Frame 01 commands without restoring the retired vendor menu', async () => {
    const onAddApiKey = vi.fn();
    const onAddSubscription = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <SourcesCard read={readyRegion([])} onRetry={vi.fn()} onOpenSource={vi.fn()} onAddApiKey={onAddApiKey} onAddSubscription={onAddSubscription} />
      </I18nextProvider>,
    );

    const subscription = screen.getByRole('button', { name: /Add subscription|添加订阅/i });
    expect((subscription as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByRole('button', { name: /^Add source$|^添加来源$/i })).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: /Add API key|添加 API Key/i }));
    expect(onAddApiKey).toHaveBeenCalledOnce();
    await userEvent.click(subscription);
    expect(onAddSubscription).toHaveBeenCalledOnce();
  });

  it('keeps the last good source rows visible with an F2 retry after a later read fails', async () => {
    const onRetry = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <SourcesCard read={failRegionRead(readyRegion([retained]))} onRetry={onRetry} onOpenSource={vi.fn()} onAddApiKey={vi.fn()} onAddSubscription={vi.fn()} />
      </I18nextProvider>,
    );

    expect(screen.getByText('Retained source')).toBeTruthy();
    expect(screen.getByText(/Could not read the source list|没有读到来源列表/i)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: /^Retry$|^重试$/i }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});
