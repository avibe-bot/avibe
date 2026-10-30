// @vitest-environment jsdom
// WF-2V: active-read gating and independent consent/order writes at the UI owner.
// Search-only tests cannot catch fetch becoming a saved search override or a
// catalog badge invented for a search-only Source.
import * as React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import i18n from '@/i18n';
import { ToastProvider } from '@/context/ToastProvider';
import { SourceDetailPanel } from './SourceDetailPanel';
import type { TrackSourceMutation } from './mutationSettlement';
import { ToolsTab } from './ToolsTab';
import { AddSearchServiceDialog } from './AddSearchServiceDialog';
import { createSourceCollectionReadAuthority } from './collectionReadAuthority';
import { modelsApi } from './modelsApi';
import { readyRegion } from './regionRead';
import { KEYLESS_ACTIVE_SEARCH, SEARCH_SOURCES } from './searchServices.fixtures';
import fixture from './webFetch.fixture.json';
import type { HostedWebFetchToolStatus } from './types';

const fetch = fixture as unknown as HostedWebFetchToolStatus;
const wrap = (node: React.ReactNode) => <I18nextProvider i18n={i18n.cloneInstance({ lng: 'zh' })}><ToastProvider>{node}</ToastProvider></I18nextProvider>;
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('shows the numbered automatic order after search and saves only the field changed', async () => {
  const save = vi.fn();
  render(wrap(<ToolsTab read={readyRegion({ tools: [KEYLESS_ACTIVE_SEARCH, fetch] })} sources={SEARCH_SOURCES} pending={new Set()} onSave={save} onClear={vi.fn()} onRetry={vi.fn()} />));
  const card = screen.getByRole('region', { name: '网页读取' });
  const rows = within(card).getAllByRole('listitem');
  expect(rows.map((row) => row.getAttribute('data-executor'))).toEqual(['src_localfetch', 'src_tavily01', 'src_exakeyless', 'src_jinakeyless']);
  expect(card.textContent).toContain('最近一次由 本机');
  expect(card.textContent).toContain('可渲染 JS');
  expect(card.textContent).toContain('密钥只保存在网关');
  // One line per price, in the row's price column; a list price opens the vendor's page.
  expect(within(rows[1]).getByRole('link', { name: '$1.60 / 千页' })).toBeTruthy();
  // The default order and where a read goes are the heading's ⓘ, not text under the list.
  expect(card.textContent).not.toContain('出口 IP');
  // Hovered and then clicked, the hint stays open.
  const about = within(card).getByRole('button', { name: '关于读取顺序' });
  await userEvent.click(about);
  expect(screen.getByText(/出口 IP/)).toBeTruthy();
  await userEvent.unhover(about);
  expect(within(card).queryByText('优先使用模型自身的搜索')).toBeNull();
  // The order is automatic, so there is nothing to reset.
  expect(within(card).queryByRole('button', { name: '恢复自动' })).toBeNull();
  await userEvent.click(within(rows[0]).getByRole('switch'));
  await userEvent.click(within(rows[3]).getByRole('button', { name: /上移/ }));
  expect(save.mock.calls).toEqual([
    ['web_fetch', { services: { src_localfetch: { enabled: false } } }],
    ['web_fetch', { executor_order: ['src_localfetch', 'src_tavily01', 'src_jinakeyless', 'src_exakeyless'] }],
  ]);
});

it('offers a reset only once the fetch order is customized', async () => {
  const save = vi.fn();
  const custom = { ...fetch, executor_order: ['src_tavily01', 'src_localfetch'] };
  render(wrap(<ToolsTab read={readyRegion({ tools: [custom] })} sources={SEARCH_SOURCES} pending={new Set()} onSave={save} onClear={vi.fn()} onRetry={vi.fn()} />));
  await userEvent.click(within(screen.getByRole('region', { name: '网页读取' })).getByRole('button', { name: '恢复自动' }));
  expect(save).toHaveBeenCalledWith('web_fetch', { executor_order: null });
});

it('keeps disabled rows visible and immovable, with no key/remove or global local retry', async () => {
  const tool = structuredClone(fetch);
  tool.executor_candidates[3].settings.enabled = false;
  render(wrap(<ToolsTab read={readyRegion({ tools: [tool] })} sources={SEARCH_SOURCES} pending={new Set()} onSave={vi.fn()} onClear={vi.fn()} onRetry={vi.fn()} />));
  const rows = within(screen.getByRole('list', { name: '读取顺序' })).getAllByRole('listitem');
  expect(within(rows[3]).getByRole('switch').getAttribute('aria-checked')).toBe('false');
  expect((within(rows[3]).getByRole('button', { name: /上移/ }) as HTMLButtonElement).disabled).toBe(true);
  await userEvent.click(within(rows[0]).getByRole('button', { name: '本机' }));
  const detail = screen.getByRole('dialog');
  expect(detail.textContent).toContain('不读取用户 Cookie');
  expect(detail.textContent).toContain('12,000');
  expect(within(detail).queryByRole('button', { name: /立即重试|移除|密钥/ })).toBeNull();
});

it('configured Source detail keeps Search and Fetch consent independent', async () => {
  vi.spyOn(modelsApi, 'getUsageSummary').mockRejectedValue(new Error('offline usage fixture'));
  const saveSearch = vi.fn();
  const saveFetch = vi.fn();
  const source = { ...SEARCH_SOURCES[0], id: 'src_tavily01' };
  const search = { ...KEYLESS_ACTIVE_SEARCH, service_vendors: KEYLESS_ACTIVE_SEARCH.service_vendors!.map((vendor) => vendor.id === 'tavily' ? { ...vendor, source_ids: [source.id] } : vendor) };
  render(wrap(<SourceDetailPanel source={source} trackMutation={vi.fn() as TrackSourceMutation} onReauth={vi.fn()} onMutationCommitted={vi.fn()} serviceView
    hostedTools={readyRegion({ tools: [search, fetch] })} onSaveSearch={saveSearch} onSaveFetch={saveFetch} onRetryService={vi.fn()} onRetryFetch={vi.fn()} />));
  await userEvent.click(screen.getByRole('switch', { name: '用于网页读取' }));
  expect(saveFetch).toHaveBeenCalledWith({ services: { src_tavily01: { enabled: false } } });
  expect(saveSearch).not.toHaveBeenCalled();
  expect(screen.getByText('https://api.tavily.com/extract', { exact: false })).toBeTruthy();
});

it.each([false, true])('adds only catalog-backed Search/Fetch badges, active=%s', (active) => {
  render(wrap(<AddSearchServiceDialog tool={KEYLESS_ACTIVE_SEARCH} fetchTool={active ? fetch : undefined} pending={false} sourceReads={createSourceCollectionReadAuthority(modelsApi)} onClose={vi.fn()} onAdded={vi.fn()} onOpenSource={vi.fn()} onViewOrder={vi.fn()} />));
  const tavily = screen.getByRole('region', { name: 'Tavily' });
  expect(within(tavily).queryByText('读取') !== null).toBe(active);
  if (active) {
    const builtin = screen.getByRole('region', { name: 'Jina Reader（免费）' });
    expect(within(builtin).getByRole('button', { name: /查看读取顺序/ })).toBeTruthy();
    expect(within(builtin).queryByRole('button', { name: /添加/ })).toBeNull();
  }
});
