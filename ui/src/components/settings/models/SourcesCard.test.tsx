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

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe('SourcesCard footer', () => {
  it('fills a narrow overview column without preserving an intrinsic panel width', () => {
    render(
      <I18nextProvider i18n={i18n}>
        <SourcesCard read={readyRegion([])} onRetry={vi.fn()} onOpenSource={vi.fn()} onAddApiKey={vi.fn()} onAddSubscription={vi.fn()} />
      </I18nextProvider>,
    );

    const panel = screen.getByRole('heading', { name: /Upstream sources|模型供应商/i }).closest('section');
    expect(panel?.className).toContain('w-full');
    expect(panel?.className).toContain('min-w-0');
    expect(panel?.className).not.toContain('max-h-full');
    expect(panel?.children.item(1)?.className).not.toContain('overflow-y-auto');
  });

  it('keeps the source title on its own line above the kind and protocol tags', () => {
    render(
      <I18nextProvider i18n={i18n}>
        <SourcesCard read={readyRegion([retained])} onRetry={vi.fn()} onOpenSource={vi.fn()} onAddApiKey={vi.fn()} onAddSubscription={vi.fn()} />
      </I18nextProvider>,
    );

    const title = screen.getByText('Retained source');
    expect(title.className).toContain('block');
    expect(title.nextElementSibling?.className).toContain('flex');
    expect(screen.getByRole('button', { name: /^Retained source/ }).className).toContain('min-h-[96px]');
  });

  it.each(['en', 'zh'])('uses one header toggle for all account, URL and key details in %s', async (lng) => {
    const locale = i18n.cloneInstance({ lng });
    const onOpenSource = vi.fn();
    const rows: Source[] = [
      { ...retained, base_url: 'https://private-relay.example/v1', masked_credential: 'sk-…7890' },
      { ...retained, id: 'src_account', kind: 'subscription', account_label: 'owner@example.com' },
    ];
    const tree = <I18nextProvider i18n={locale}>
      <SourcesCard read={readyRegion(rows)} onRetry={vi.fn()} onOpenSource={onOpenSource} onAddApiKey={vi.fn()} onAddSubscription={vi.fn()} />
    </I18nextProvider>;
    const view = render(tree);
    expect(screen.getByRole('button', { name: /private-relay\.example\/v1.*sk-…7890/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /owner@example\.com/ })).toBeTruthy();
    const toggle = screen.getByRole('button', { name: locale.t('settings.models.upstream.hidePrivateDetails') });
    expect(toggle.closest('[data-source-id]')).toBeNull();
    expect(view.container.querySelector('button button')).toBeNull();
    expect(view.container.querySelector('[data-source-id] button')).toBeNull();
    await userEvent.click(toggle);
    expect(onOpenSource).not.toHaveBeenCalled();
    for (const value of ['private-relay.example', 'sk-…7890', 'owner@example.com']) {
      expect(view.container.innerHTML).not.toContain(value);
    }
    expect(screen.queryByRole('button', { name: /private-relay\.example|sk-…7890|owner@example\.com/ })).toBeNull();
    expect(screen.getByRole('button', { name: (name) => [
      locale.t('settings.models.upstream.kind.apiKey'),
      'Anthropic Messages',
      locale.t('settings.models.upstream.hiddenDetails'),
    ].every((part) => name.includes(part)) })).toBeTruthy();
    const placeholders = screen.getAllByText(lng === 'zh' ? '已隐藏' : 'Hidden', { exact: true });
    expect(placeholders).toHaveLength(2);
    for (const placeholder of placeholders) {
      expect(placeholder.previousElementSibling?.classList.contains('lucide-eye-off')).toBe(true);
      expect(placeholder.previousElementSibling?.getAttribute('aria-hidden')).toBe('true');
    }
    view.unmount();
    const remounted = render(tree);
    expect(remounted.container.innerHTML).not.toContain('private-relay.example');
    const reveal = screen.getByRole('button', { name: locale.t('settings.models.upstream.showPrivateDetails') });
    reveal.focus();
    await userEvent.keyboard('{Enter}');
    expect(screen.getByText('private-relay.example/v1 · sk-…7890')).toBeTruthy();
    expect(screen.getByText('owner@example.com')).toBeTruthy();
    expect(onOpenSource).not.toHaveBeenCalled();
  });

  it('exposes the upstream info note to keyboard activation and Escape dismissal', async () => {
    const user = userEvent.setup();
    render(
      <I18nextProvider i18n={i18n}>
        <SourcesCard read={readyRegion([])} onRetry={vi.fn()} onOpenSource={vi.fn()} onAddApiKey={vi.fn()} onAddSubscription={vi.fn()} />
      </I18nextProvider>,
    );

    const info = screen.getByRole('button', { name: /What upstream sources are|什么是模型供应商/i });
    await user.tab();
    expect(document.activeElement).toBe(info);
    await user.keyboard('[Enter]');
    expect(await screen.findByText(/account or API key|账号或 API Key/i)).toBeTruthy();
    await user.keyboard('[Escape]');
    await waitFor(() => expect(screen.queryByText(/account or API key|账号或 API Key/i)).toBeNull());
  });

  it('draws the two Frame 01 commands and dispatches each action', async () => {
    const onAddApiKey = vi.fn();
    const onAddSubscription = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <SourcesCard read={readyRegion([])} onRetry={vi.fn()} onOpenSource={vi.fn()} onAddApiKey={onAddApiKey} onAddSubscription={onAddSubscription} />
      </I18nextProvider>,
    );

    const subscription = screen.getByRole('button', { name: /Add subscription|添加订阅/i });
    expect((subscription as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByRole('button', { name: /^Add source$|^添加供应商$/i })).toBeNull();

    const apiKey = screen.getByRole('button', { name: /Add API key|添加 API Key/i });
    await userEvent.click(apiKey);
    expect(onAddApiKey).toHaveBeenCalledWith(apiKey);
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

  it('disables the page retry while route reconciliation is pending', () => {
    render(
      <I18nextProvider i18n={i18n}>
        <SourcesCard
          read={failRegionRead(readyRegion([retained]))}
          retryDisabled
          onRetry={vi.fn()}
          onOpenSource={vi.fn()}
          onAddApiKey={vi.fn()}
          onAddSubscription={vi.fn()}
        />
      </I18nextProvider>,
    );

    expect((screen.getByRole('button', { name: /^Retry$|^重试$/i }) as HTMLButtonElement).disabled).toBe(true);
  });
});
