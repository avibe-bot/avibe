// @vitest-environment jsdom
// Search service UI boundaries: active reads expose the service controls; mixed
// order and Source settings must write only the intended field; the shared add
// dialog must use read membership (not a vendor-name guess) and Source create
// settlement. Released Tools/Source tests only cover model executors.
import * as React from 'react';
import { cleanup, render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { ToastProvider } from '@/context/ToastProvider';
import { AddSearchServiceDialog } from './AddSearchServiceDialog';
import { createSourceCollectionReadAuthority } from './collectionReadAuthority';
import { ApiCallError, modelsApi } from './modelsApi';
import type { SourceMutationSettlement, TrackSourceMutation } from './mutationSettlement';
import { readyRegion } from './regionRead';
import { ACTIVE_SEARCH, KEYLESS_ACTIVE_SEARCH, SEARCH_SOURCES } from './searchServices.fixtures';
import { SourceDetailPanel } from './SourceDetailPanel';
import { SourcesCard } from './SourcesCard';
import { ToolsTab } from './ToolsTab';
import type { HostedServiceExecutorCandidate, HostedWebSearchToolStatus, Source, UsageReport } from './types';

const wrap = (element: React.ReactElement, lng = 'en') => <I18nextProvider i18n={i18n.cloneInstance({ lng })}><ToastProvider>{element}</ToastProvider></I18nextProvider>;
const tab = (tool = ACTIVE_SEARCH) => {
  const onSave = vi.fn();
  const onOpenService = vi.fn();
  const onRetryService = vi.fn();
  const onAddService = vi.fn();
  const view = render(wrap(<ToolsTab read={readyRegion({ tools: [tool] })} sources={SEARCH_SOURCES} pending={new Set()} onSave={onSave} onClear={vi.fn()} onRetry={vi.fn()} onOpenService={onOpenService} onAddService={onAddService} onRetryService={onRetryService} />));
  return { ...view, onSave, onOpenService, onRetryService, onAddService };
};
// A daily report whose host month (`to_day`) holds one Exa day; the previous month's last day stays out.
const MONTH_REPORT: UsageReport = {
  window_days: 60, from_day: '2026-08-23', to_day: '2026-10-21', window_key: '60d', granularity: 'day',
  from_at: '2026-08-23T00:00:00+08:00', to_at: '2026-10-21T12:00:00+08:00',
  totals: { requests: 0, token_reports: 0, input_tokens: 0, cached_input_tokens: 0, output_tokens: 0 },
  sources: [], services: [], days: [], pricing: { currency: 'USD', price_table_date: '2026-10-19', service_price_date: '2026-10-19' },
  buckets: ['2026-09-30', '2026-10-05'].map((key) => ({
    key, start_at: `${key}T00:00:00+08:00`, end_at: `${key}T23:59:59+08:00`, history_complete: true,
    rows: [{
      kind: 'service', source_id: 'src_exa000001', service_id: 'exa/search', requests: 3, billable_requests: 2, uncertain_requests: 1,
      reported_requests: 2, reported_usd: 0.0135, api_cost_usd: 0.014, api_cost_lower_bound: true,
    }],
  })),
};
const detail = (source: Source, tool = ACTIVE_SEARCH, serviceView = true) => {
  const onSaveSearch = vi.fn();
  const onRetryService = vi.fn();
  const onOpenUsage = vi.fn();
  const readUsage = vi.spyOn(modelsApi, 'getUsageSummary').mockResolvedValue(MONTH_REPORT);
  const settle: SourceMutationSettlement = {
    source: vi.fn().mockResolvedValue(null), gone: vi.fn().mockResolvedValue(null), unread: vi.fn().mockResolvedValue(null), release: vi.fn(),
    readInventory: vi.fn().mockResolvedValue({ snapshot: 1, sources: [source] }),
  };
  const trackMutation: TrackSourceMutation = (work) => work(source, settle);
  const view = render(wrap(<SourceDetailPanel source={source} hostedTools={readyRegion({ tools: [tool] })} onSaveSearch={onSaveSearch} onRetryService={onRetryService} serviceView={serviceView} onManageSource={vi.fn()} onOpenUsage={onOpenUsage} trackMutation={trackMutation} onReauth={vi.fn()} onMutationCommitted={async (commit) => { await commit.settle(); }} />));
  return { ...view, onSaveSearch, onRetryService, onOpenUsage, readUsage, settle };
};
const addDialog = (tool = ACTIVE_SEARCH) => {
  const onAdded = vi.fn();
  const onOpenSource = vi.fn();
  render(wrap(<AddSearchServiceDialog tool={tool} pending={false} sourceReads={createSourceCollectionReadAuthority(modelsApi)} onClose={vi.fn()} onAdded={onAdded} onOpenSource={onOpenSource} />));
  return { onAdded, onOpenSource };
};
const newVendors = (): HostedWebSearchToolStatus => ({
  ...ACTIVE_SEARCH, services: {}, executor_candidates: [],
  service_vendors: ACTIVE_SEARCH.service_vendors!.map((vendor) => ({ ...vendor, source_ids: [] })),
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); });

describe('search service activation and mixed order', () => {
  it.each(['en', 'zh'])('exposes service order, native preference and add entry points on the active read in %s', async (lng) => {
    const locale = i18n.cloneInstance({ lng });
    const view = render(wrap(<>
      <ToolsTab read={readyRegion({ tools: [ACTIVE_SEARCH] })} sources={SEARCH_SOURCES} pending={new Set()} onSave={vi.fn()} onClear={vi.fn()} onRetry={vi.fn()} />
      <SourcesCard read={readyRegion(SEARCH_SOURCES)} onRetry={vi.fn()} onOpenSource={vi.fn()} onAddApiKey={vi.fn()} onAddSubscription={vi.fn()} searchTool={ACTIVE_SEARCH} onAddSearchService={vi.fn()} />
    </>, lng));
    expect(screen.getByText(locale.t('settings.models.searchServices.subtitle'))).toBeTruthy();
    expect(screen.getAllByRole('switch')).toHaveLength(2);
    expect(screen.getByRole('heading', { name: locale.t('settings.models.searchServices.order') })).toBeTruthy();
    expect(screen.getByRole('button', { name: locale.t('settings.models.searchServices.addProvider') })).toBeTruthy();
    expect(screen.getByRole('button', { name: locale.t('settings.models.searchServices.add') })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: new RegExp(`^${locale.t('settings.models.tools.advanced.label')}`) }));
    expect(screen.getAllByRole('list', { name: locale.t('settings.models.searchServices.order') })).toHaveLength(1);
    expect(view.container.querySelectorAll('[data-executor]')).toHaveLength(4);
  });

  it('shows mixed rows above Advanced, disables opted-out reordering, and saves preference/order independently', async () => {
    const { onSave, onOpenService, onAddService } = tab({ ...ACTIVE_SEARCH, executor_order: ['src_tavily001', 'src_exa000001', 'src_claude001'] });
    const order = screen.getByRole('list', { name: 'Search order' });
    const rows = within(order).getAllByRole('listitem');
    expect(rows.map((row) => row.getAttribute('data-executor'))).toEqual(['src_tavily001', 'src_exa000001', 'src_claude001', 'src_deepseek1']);
    expect(rows[0].textContent).toContain('skipped');
    expect(rows[0].textContent).toContain('$8.00 / 1k searches');
    expect(rows[1].textContent).toContain('Saved');
    expect(rows[2].textContent).toContain('Model executor');
    expect(rows[2].textContent).toContain('OK · 10-19');
    expect(rows[3].textContent).toContain('Not used for search');
    expect((within(rows[3]).getByRole('button', { name: 'Move DeepSeek up' }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(rows[2]).getByRole('button', { name: 'Move Anthropic API key down' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('button', { name: 'Retry now' })).toBeNull();
    await userEvent.click(screen.getByRole('switch', { name: "Prefer the model's own search" }));
    await userEvent.click(screen.getByRole('button', { name: 'Move Exa up' }));
    await userEvent.click(screen.getByRole('button', { name: 'Reset to automatic' }));
    expect(onSave.mock.calls).toEqual([
      ['web_search', { prefer_native: false }],
      ['web_search', { executor_order: ['src_exa000001', 'src_tavily001', 'src_claude001'] }],
      ['web_search', { executor_order: null }],
    ]);
    const opener = screen.getByRole('button', { name: 'Tavily' });
    await userEvent.click(opener);
    expect(onOpenService).toHaveBeenCalledWith('src_tavily001', opener);
    await userEvent.click(screen.getByRole('button', { name: 'Add search service' }));
    expect(onAddService).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByRole('button', { name: /^Advanced/ }));
    expect(screen.getAllByRole('list', { name: 'Search order' })).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Status by Source' })).toBeTruthy();
  });

  it('renders the built-in keyless Exa row as free and non-addable', async () => {
    tab(KEYLESS_ACTIVE_SEARCH);
    expect(screen.getByText('Exa (free)')).toBeTruthy();
    expect(screen.getByText('Free')).toBeTruthy();
    const { onOpenSource } = addDialog(KEYLESS_ACTIVE_SEARCH);
    expect(screen.getByText('Free; IP rate-limited')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Add Exa (free)' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'View search order' }));
    expect(onOpenSource).toHaveBeenCalledWith('src_exakeyless');
  });

  it('keeps keyboard focus on a usable arrow when the last movable row is followed by disabled Sources', async () => {
    const view = tab();
    await userEvent.click(screen.getByRole('button', { name: 'Move Exa down' }));
    const reordered = { ...ACTIVE_SEARCH, executor_candidates: [ACTIVE_SEARCH.executor_candidates[0], ACTIVE_SEARCH.executor_candidates[2], ACTIVE_SEARCH.executor_candidates[1], ACTIVE_SEARCH.executor_candidates[3]] };
    view.rerender(wrap(<ToolsTab read={readyRegion({ tools: [reordered] })} sources={SEARCH_SOURCES} pending={new Set()} onSave={view.onSave} onClear={vi.fn()} onRetry={vi.fn()} />));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Move Exa up' })));
  });

  it.each([true, false])('explains the native preference %s and compares live catalog prices with no service added', (prefer_native) => {
    tab({ ...newVendors(), prefer_native });
    expect(screen.getByText(prefer_native ? /When Claude, GPT or Grok/ : /Every model uses the search order below/)).toBeTruthy();
    expect(screen.getByText(/List prices: DeepSeek \$0.75.*Tavily \$8.*Exa \$7/)).toBeTruthy();
    expect(screen.queryByText(/full page|page content|\$1/)).toBeNull();
  });

  it('adds the shared dialog entry to Sources only on the active read, and renders search Sources without model claims', async () => {
    const onAddSearchService = vi.fn();
    render(wrap(<SourcesCard read={readyRegion(SEARCH_SOURCES)} onRetry={vi.fn()} onOpenSource={vi.fn()} onAddApiKey={vi.fn()} onAddSubscription={vi.fn()} searchTool={ACTIVE_SEARCH} onAddSearchService={onAddSearchService} />));
    const row = screen.getByRole('button', { name: /^Tavily/ });
    expect(row.textContent).toContain('does not provide models');
    expect(row.textContent).toContain('skipped');
    const trigger = screen.getByRole('button', { name: 'Add provider' });
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole('menuitem', { name: 'Search services (Tavily, Exa…)' }));
    expect(onAddSearchService).toHaveBeenCalledWith(trigger);
  });
});

describe('search Source detail', () => {
  it.each([false, true])('edits only the changed service setting with model Source serviceView=%s, preserving its models', async (serviceView) => {
    const { onSaveSearch } = detail(SEARCH_SOURCES[3], ACTIVE_SEARCH, serviceView);
    const toggle = screen.getByRole('switch', { name: 'Use for web search' });
    expect(toggle.getAttribute('aria-checked')).toBe('false');
    await userEvent.click(toggle);
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Results per search' }), '10');
    expect(onSaveSearch.mock.calls).toEqual([
      [{ services: { src_deepseek1: { enabled: true } } }],
      [{ services: { src_deepseek1: { results: 10 } } }],
    ]);
    if (serviceView) {
      expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Manage DeepSeek' })).toBeNull();
      await userEvent.click(screen.getByRole('button', { name: 'Manage provider key' }));
      expect(screen.getByRole('dialog', { name: 'Replace the API key for “DeepSeek”' })).toBeTruthy();
    } else expect(screen.getByText('deepseek-chat')).toBeTruthy();
    expect(SEARCH_SOURCES[3].models.map((model) => model.id)).toEqual(['deepseek-chat']);
  });

  it('offers key replacement and removal, never model controls, for an active tool-only detail', async () => {
    detail(SEARCH_SOURCES[0]);
    expect(screen.getByText('Search service · does not provide models')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Refetch|Test|Add model|Re-enable/ })).toBeNull();
    expect(screen.queryByText(/page content|full page/i)).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Replace key' }));
    expect(screen.getByRole('dialog', { name: 'Replace the API key for “Tavily”' })).toBeTruthy();
    await userEvent.click(within(screen.getByRole('dialog')).getAllByRole('button', { name: 'Cancel' }).at(-1)!);
    const remove = vi.spyOn(modelsApi, 'deleteSource').mockResolvedValue({ removed_hops: [], interrupted: [] });
    await userEvent.click(screen.getByRole('button', { name: 'Remove' }));
    await userEvent.click(screen.getByRole('button', { name: 'Remove source' }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith('src_tavily001', undefined));
  });

  it.each([
    ['listed', ACTIVE_SEARCH, true],
    ['unlisted', { ...ACTIVE_SEARCH, executor_candidates: ACTIVE_SEARCH.executor_candidates.filter((row) => row.source_id !== 'src_exa000001') }, false],
  ] as const)('MH-USAGE-038: reads this month only for a %s search service, apart from the Usage tab', async (_, tool, listed) => {
    const { readUsage, onOpenUsage } = detail(SEARCH_SOURCES[1], tool);
    if (!listed) {
      await waitFor(() => expect(screen.getByRole('heading', { name: 'Exa' })).toBeTruthy());
      expect(readUsage).not.toHaveBeenCalled();
      expect(screen.queryByText('This month')).toBeNull();
      return;
    }
    // Its own daily read, never the tab's window; the vendors' figure stands beside the list estimate.
    expect(readUsage.mock.calls).toEqual([['60d']]);
    const line = (await screen.findByText('This month')).closest('p')!;
    expect(line.textContent).toBe('This month3 requests · API list-price estimate ≥ $0.01 · vendor-reported $0.01View usage');
    await userEvent.click(within(line).getByRole('button', { name: 'View usage' }));
    expect(onOpenUsage).toHaveBeenCalledOnce();
  });

  it.each([null, '2026-12-01T10:00:00Z'])('offers retry exactly when cooldown_until=%s on both row and detail', async (cooldown_until) => {
    const candidate: HostedServiceExecutorCandidate = { ...(ACTIVE_SEARCH.executor_candidates[1] as HostedServiceExecutorCandidate), cooldown_until };
    const tool = { ...ACTIVE_SEARCH, executor_candidates: [candidate] };
    const view = tab(tool);
    if (cooldown_until === null) expect(screen.queryByRole('button', { name: 'Retry now' })).toBeNull();
    else {
      expect(screen.getByText(/Paused until 12-01/)).toBeTruthy();
      await userEvent.click(screen.getByRole('button', { name: 'Retry now' }));
      expect(view.onRetryService).toHaveBeenCalledWith('src_exa000001');
    }
    view.unmount();
    const panel = detail(SEARCH_SOURCES[1], tool);
    if (cooldown_until === null) expect(screen.queryByRole('button', { name: 'Retry now' })).toBeNull();
    else {
      await userEvent.click(screen.getByRole('button', { name: 'Retry now' }));
      expect(panel.onRetryService).toHaveBeenCalledWith('src_exa000001');
    }
  });
});

describe('search service add dialog', () => {
  it.each([false, true])('uses read membership for Added and DeepSeek search enabled=%s', async (enabled) => {
    const tool = { ...ACTIVE_SEARCH, executor_candidates: ACTIVE_SEARCH.executor_candidates.map((row) => row.source_id === 'src_deepseek1' && row.kind === 'service' ? { ...row, settings: { ...row.settings, enabled } } : row) };
    const { onOpenSource } = addDialog(tool);
    expect(within(screen.getByRole('region', { name: 'Tavily' })).getByText('Added')).toBeTruthy();
    expect(within(screen.getByRole('region', { name: 'DeepSeek' })).getByText(enabled ? 'Already in search order' : 'Added · not used for search')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Add DeepSeek' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Manage DeepSeek' }));
    expect(onOpenSource).toHaveBeenCalledWith('src_deepseek1');
  });

  it.each([
    ['tavily', 'pass'], ['tavily', 'balance'], ['tavily', 'limit'], ['tavily', 'unknown'],
    ['tavily', 'invalid'], ['exa', 'no_check'], ['deepseek', 'pass'], ['deepseek', 'balance'], ['deepseek', 'invalid'],
  ] as const)('creates %s through Source create and preserves the %s free-check outcome', async (vendor, outcome) => {
    // The UI consumes the Source envelope, not an invented free-check response.
    const base = SEARCH_SOURCES.find((source) => source.vendor === vendor)!;
    const saved: Source = { ...base, state: outcome === 'balance' || outcome === 'limit'
      ? { status: 'needs_action', detail_key: outcome === 'balance' ? 'models.source.needs_action.balance_exhausted' : 'models.source.needs_action.spend_limit_reached' }
      : { status: 'standby' }, verification_pending: outcome === 'unknown' || outcome === 'no_check' ? 'vp_fixture' : null };
    const create = vi.spyOn(modelsApi, 'createApiKeySource');
    const observe = vi.spyOn(modelsApi, 'observeApiKeySource');
    const answer = { source: saved, added_to: [], adopted_by: [] };
    if (outcome === 'invalid') create.mockRejectedValue(new ApiCallError('discovery_failed', 'modelHub.errors.authentication_failed', true, undefined, undefined, undefined, 422));
    else create.mockResolvedValue(answer);
    const tool = newVendors(); // Also the read for a relay-only DeepSeek user: no official Source ids.
    const { onAdded } = addDialog(tool);
    const label = { tavily: 'Tavily', exa: 'Exa', deepseek: 'DeepSeek' }[vendor];
    await userEvent.click(screen.getByRole('button', { name: `Add ${label}` }));
    if (vendor === 'deepseek') expect(screen.getByText(/Creates an ordinary DeepSeek provider on its official endpoint/)).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: 'Base URL' })).toBeNull();
    await userEvent.type(screen.getByLabelText('API key', { exact: true }), 'fixture-key');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    const expected = vendor === 'deepseek'
      ? { kind: 'api_key', vendor: 'deepseek', key: 'fixture-key', protocol: 'openai_chat', save_unverified: true, client_nonce: expect.stringMatching(/^scn_/) }
      : { kind: 'api_key', vendor, key: 'fixture-key', protocol: 'search', client_nonce: expect.stringMatching(/^scn_/) };
    expect(create).toHaveBeenCalledWith(expected);
    expect(observe).not.toHaveBeenCalled();
    if (outcome === 'invalid') {
      expect(onAdded).not.toHaveBeenCalled();
      expect(await screen.findByText('The API key was rejected. Check it and try again.')).toBeTruthy();
    } else await waitFor(() => expect(onAdded).toHaveBeenCalledWith(answer));
  });

  it('reconciles a lost search create response by nonce before any retry', async () => {
    let nonce = '';
    const create = vi.spyOn(modelsApi, 'createApiKeySource').mockImplementation(async (body) => { nonce = body.client_nonce!; throw new ApiCallError('bad_response', undefined, false); });
    const list = vi.spyOn(modelsApi, 'listSources').mockImplementation(async () => [{ ...SEARCH_SOURCES[1], client_nonce: nonce }]);
    const { onAdded } = addDialog(newVendors());
    await userEvent.click(screen.getByRole('button', { name: 'Add Exa' }));
    await userEvent.type(screen.getByLabelText('API key', { exact: true }), 'fixture-key');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    expect(list).toHaveBeenCalled();
    expect(create).toHaveBeenCalledOnce();
    expect(onAdded).toHaveBeenCalledWith(expect.objectContaining({ source: expect.objectContaining({ id: 'src_exa000001', client_nonce: nonce }) }));
  });
});
