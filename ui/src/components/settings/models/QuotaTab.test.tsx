// @vitest-environment jsdom
// What the 订阅额度 tab says about a quota report: the windows it names, the pace
// it claims, and what it admits about a reading that is not current.
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { degradedRegion, loadingRegion, readyRegion, type RegionRead } from './regionRead';
import { QuotaTab } from './QuotaTab';
import { windowLeftPct, windowPace, windowUsedPct } from './quotaProjection';
import type { QuotaSummary, QuotaWindow, SourceQuota } from './types';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'zh',
  fallbackLng: 'en',
  resources: { en: { translation: en }, zh: { translation: zh } },
  interpolation: { escapeValue: false },
});

const NOW = Date.parse('2026-09-25T03:00:00Z');
const HOUR = 3_600_000;
const iso = (ms: number) => new Date(ms).toISOString();

const window = (over: Partial<QuotaWindow> = {}): QuotaWindow => ({
  id: 'five_hour',
  kind: 'session',
  label: 'five_hour',
  used_pct: 38,
  window_seconds: 18_000,
  resets_at: iso(NOW + 2 * HOUR),
  ...over,
});

const claude = (over: Partial<SourceQuota> = {}): SourceQuota => ({
  source_id: 'src_claude',
  vendor: 'anthropic',
  display_name: 'Claude Max',
  account_label: 'alex@example.com',
  plan: 'max',
  fetched_at: iso(NOW - 2 * 60_000),
  state: 'ok',
  windows: [
    window(),
    window({ id: 'seven_day', kind: 'weekly', label: 'seven_day', used_pct: 61, window_seconds: 604_800, resets_at: iso(NOW + 4 * 24 * HOUR) }),
    window({ id: 'fable', kind: 'model_weekly', label: 'Fable', scope_model: 'Fable', used_pct: 86, window_seconds: 604_800, resets_at: iso(NOW + 4 * 24 * HOUR) }),
    window({ id: 'sonnet', kind: 'model_weekly', label: 'Sonnet', scope_model: 'Sonnet', used_pct: 22, window_seconds: 604_800, resets_at: iso(NOW + 4 * 24 * HOUR) }),
  ],
  ...over,
});

const codex = (over: Partial<SourceQuota> = {}): SourceQuota => ({
  source_id: 'src_codex',
  vendor: 'openai',
  display_name: 'ChatGPT Pro',
  account_label: null,
  plan: 'pro',
  fetched_at: iso(NOW - 60_000),
  state: 'ok',
  windows: [
    window({ id: 'primary_window', label: 'primary_window', used_pct: 100, resets_at: iso(NOW + 47 * 60_000) }),
    window({ id: 'secondary_window', kind: 'weekly', label: 'secondary_window', used_pct: 45, window_seconds: 604_800, resets_at: iso(NOW + 5 * 24 * HOUR) }),
    window({ id: 'additional:0:primary_window', kind: 'model_weekly', label: 'Spark', scope_model: 'Spark', used_pct: 8, window_seconds: 604_800, resets_at: iso(NOW + 5 * 24 * HOUR) }),
  ],
  ...over,
});

const summary = (sources: SourceQuota[]): QuotaSummary => ({ refresh_interval_seconds: 300, sources });

const draw = (quota: RegionRead<QuotaSummary>, over: { onRefresh?: () => void; onRequestReauth?: (id: string) => void } = {}) =>
  render(
    <I18nextProvider i18n={i18n}>
      <QuotaTab quota={quota} now={NOW} onRefresh={over.onRefresh} onRequestReauth={over.onRequestReauth} />
    </I18nextProvider>,
  );

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('QuotaTab', () => {
  it('MH-QUOTA-013: names every window in the final copy and states pace, reset, and status per account', () => {
    const { container } = draw(readyRegion(summary([claude(), codex()])));

    expect(screen.getByRole('heading', { name: '订阅额度' })).toBeTruthy();
    expect(screen.getByText('2 个已登录的订阅账号')).toBeTruthy();
    expect(screen.getByText('每 5 分钟自动刷新')).toBeTruthy();
    expect(screen.getByRole('button', { name: '立即刷新' })).toBeTruthy();

    const card = screen.getByRole('article', { name: 'Claude Max' });
    for (const label of ['5 小时额度', '每周额度', 'Fable 每周额度', 'Sonnet 每周额度']) {
      expect(within(card).getByText(label)).toBeTruthy();
    }
    expect(within(card).getAllByText('所有模型共用')).toHaveLength(2);
    expect(within(card).getByText('只算 Fable 的用量')).toBeTruthy();
    expect(within(card).getByText('单独计算的模型额度')).toBeTruthy();
    expect(within(card).getByText('这些模型也会同时扣每周额度，任一项用完就会暂停')).toBeTruthy();
    expect(within(card).getByText('可用')).toBeTruthy();
    expect(within(card).getByText('max')).toBeTruthy();
    expect(within(card).getByText('alex@example.com')).toBeTruthy();

    // Fable at 86% four days before a 7-day reset is spending faster than time.
    const fable = card.querySelector('[data-quota-window="fable"]') as HTMLElement;
    expect(fable.textContent).toMatch(/用得偏快，预计 .+ 用完/);
    expect(within(fable).getByRole('meter', { name: 'Fable 每周额度 已用 86%' })).toBeTruthy();
    expect(fable.textContent).toContain('14%');
    const session = card.querySelector('[data-quota-window="five_hour"]') as HTMLElement;
    expect(session.textContent).toContain('2 小时 0 分后重置');
    expect(session.querySelector('.model-hub-quota-time-mark')).not.toBeNull();

    // A spent window names itself in the status pill and says when it returns.
    const codexCard = screen.getByRole('article', { name: 'ChatGPT Pro' });
    expect(within(codexCard).getByText('5 小时额度已用完')).toBeTruthy();
    expect(codexCard.textContent).toContain('已用完，47 分钟后恢复');
    expect(within(codexCard).getByText('Spark 每周额度')).toBeTruthy();

    // Summary: the tightest usable window, the spent count, and upcoming resets.
    expect(screen.getByText('余量最少的额度')).toBeTruthy();
    expect(container.textContent).toContain('Claude Max · Fable 每周额度');
    expect(screen.getByText('已用完')).toBeTruthy();
    expect(screen.getByLabelText('即将重置').textContent).toContain('47 分钟');
    expect(screen.getByText('进度条是已用的部分，细竖线是这个周期已经过去的时间：进度条超过竖线，就是用得比时间快。')).toBeTruthy();

    // Money is out of scope: nothing on the tab states a price or a currency.
    expect(container.textContent).not.toMatch(/\$|USD|API 价格|回本/);
  });

  it('reports every account usable when nothing is spent, and reads pace from the window clock', () => {
    draw(readyRegion(summary([claude()])));
    expect(screen.getByText('所有账号都能正常使用')).toBeTruthy();

    const fresh = window({ used_pct: 10, resets_at: iso(NOW + 4 * HOUR) });
    expect(windowPace(fresh, NOW, false).pace.kind).toBe('ok');
    expect(windowPace(window({ used_pct: 25, resets_at: iso(NOW + 4 * HOUR) }), NOW, false).pace.kind).toBe('slightly_fast');
    expect(windowPace(window({ resets_at: iso(NOW - 1) }), NOW, false).pace.kind).toBe('reset');
    expect(windowPace(fresh, NOW, true)).toMatchObject({ pace: { kind: 'paused' }, tone: 'stale' });
  });

  it('MH-QUOTA-014: keeps an expired grant\'s last reading under a banner that offers re-login', async () => {
    const onRequestReauth = vi.fn();
    draw(readyRegion(summary([
      claude(),
      codex({ state: 'auth_expired', error_key: 'models.quota.error.auth_expired', fetched_at: iso(NOW - 3 * HOUR - 12 * 60_000) }),
    ])), { onRequestReauth });

    const card = screen.getByRole('article', { name: 'ChatGPT Pro' });
    expect(within(card).getByText('需重新登录')).toBeTruthy();
    expect(within(card).getByRole('status').textContent).toBe('登录已过期，下面是 3 小时 12 分前的数据重新登录');
    expect(within(card).getAllByText('暂停更新').length).toBeGreaterThan(0);
    expect(card.classList.contains('model-hub-quota-card--retained')).toBe(true);
    // A retained reading is not current, so it cannot fill the page summary,
    // and an account that must sign in again is not called usable.
    expect(screen.getByText('已读到的额度都没用完')).toBeTruthy();
    expect(screen.queryByText('所有账号都能正常使用')).toBeNull();

    await userEvent.click(within(card).getByRole('button', { name: '重新登录' }));
    expect(onRequestReauth).toHaveBeenCalledWith('src_codex');
  });

  it('never shows a limit with headroom as 0% left or 100% used beside its usable status', () => {
    expect(windowLeftPct(window({ used_pct: 99.9 }), NOW)).toBe(1);
    expect(windowLeftPct(window({ used_pct: 100 }), NOW)).toBe(0);
    expect(windowUsedPct(window({ used_pct: 99.9 }), NOW)).toBe(99);
    expect(windowUsedPct(window({ used_pct: 100 }), NOW)).toBe(100);
    draw(readyRegion(summary([claude({ windows: [window({ used_pct: 99.9, resets_at: iso(NOW + HOUR) })] })])));
    expect(screen.getByRole('article').textContent).not.toContain('0%');
    const meter = screen.getByRole('meter');
    expect(meter.getAttribute('aria-label')).toContain('99%');
    expect(meter.getAttribute('aria-label')).not.toContain('100%');
    expect(meter.getAttribute('aria-valuenow')).toBe('99');
  });

  it('reads a spent window whose reset has come as given back, in every figure', () => {
    // The reading predates the reset; until the next read the card must not say
    // 「已重置」 and 「可用」 beside a full bar and 0% left.
    for (const resetsAt of [NOW - 60_000, NOW]) {
      const spent = window({ used_pct: 100, resets_at: iso(resetsAt) });
      expect(windowLeftPct(spent, NOW)).toBe(100);
      expect(windowUsedPct(spent, NOW)).toBe(0);
      expect(windowPace(spent, NOW, false).pace.kind).toBe('reset');
      draw(readyRegion(summary([claude({ windows: [spent] })])));
      const card = screen.getByRole('article');
      expect(card.textContent).toContain('100%');
      expect(card.textContent).not.toContain('0% ');
      const meter = within(card).getByRole('meter');
      expect(meter.getAttribute('aria-valuenow')).toBe('0');
      expect((meter.querySelector('i') as HTMLElement).style.width).toBe('0%');
      expect(screen.getByText('所有账号都能正常使用')).toBeTruthy();
      cleanup();
    }
  });

  it('states an unread Source in words instead of an empty bar', () => {
    draw(readyRegion(summary([
      claude({ state: 'error', error_key: 'models.quota.error.unavailable', fetched_at: null, windows: [] }),
      codex({ state: 'auth_expired', error_key: 'models.quota.error.auth_expired', fetched_at: null, windows: [] }),
    ])), { onRequestReauth: () => {} });

    expect(screen.getByText('暂时读不到额度，稍后会自动重试')).toBeTruthy();
    const expired = screen.getByRole('article', { name: 'ChatGPT Pro' });
    expect(within(expired).getByText('登录已过期，读不到额度')).toBeTruthy();
    expect(within(expired).getByRole('button', { name: '重新登录' })).toBeTruthy();
    expect(screen.queryAllByRole('meter')).toHaveLength(0);
  });

  it('names a known failure reason and never calls a windowless report usable', () => {
    draw(readyRegion(summary([
      claude({ state: 'ok', windows: [] }),
      codex({ state: 'stale', error_key: 'models.quota.error.rate_limited', fetched_at: null, windows: [] }),
    ])));

    expect(within(screen.getByRole('article', { name: 'ChatGPT Pro' })).getByText('服务商暂时限制了额度查询')).toBeTruthy();
    // An `ok` report with no windows reads as unavailable on its card, so the summary cannot call it usable.
    const empty = screen.getByRole('article', { name: 'Claude Max' });
    expect(within(empty).getByText('暂时读不到额度，稍后会自动重试')).toBeTruthy();
    expect(within(empty).getByText('暂停更新')).toBeTruthy();
    expect(screen.queryByText('所有账号都能正常使用')).toBeNull();
    expect(screen.getByText('已读到的额度都没用完')).toBeTruthy();
  });

  it('keeps universal summary claims to the case where every account has a current reading', () => {
    const spent = claude({ windows: [window({ used_pct: 100, resets_at: iso(NOW + HOUR) })] });
    const expired = codex({ state: 'auth_expired', error_key: 'models.quota.error.auth_expired', fetched_at: null, windows: [] });
    const mixed = draw(readyRegion(summary([spent, expired])));
    expect(screen.getByText('暂时没有可读的额度')).toBeTruthy();
    expect(screen.queryByText('所有额度都已用完')).toBeNull();
    mixed.unmount();

    draw(readyRegion(summary([spent])));
    expect(screen.getByText('所有额度都已用完')).toBeTruthy();
  });

  it('keeps the last page under the failure strip and offers the empty state honestly', async () => {
    const stale = draw(degradedRegion(summary([claude()]), 'read_failed', true));
    expect(screen.getByText('刷新失败，请重试')).toBeTruthy();
    expect(screen.getByRole('article', { name: 'Claude Max' })).toBeTruthy();
    stale.unmount();

    draw(loadingRegion());
    expect(screen.getByText(/加载中|Loading/)).toBeTruthy();
    cleanup();

    const onRefresh = vi.fn();
    draw(readyRegion(summary([])), { onRefresh });
    expect(screen.getByText(/还没有已登录的订阅账号/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '立即刷新' }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('MH-QUOTA-015: renders an unrecognised window by its upstream name, with no invented hint; the 360px layout is held by ui/e2e/model-catalog/quota-layout.spec.ts', () => {
    draw(readyRegion(summary([claude({ windows: [window({ id: 'x', kind: 'other', label: '非常长的上游额度名称'.repeat(4) })] })])));
    expect(screen.getByText('非常长的上游额度名称'.repeat(4))).toBeTruthy();
    expect(screen.getByRole('article').querySelector('[data-quota-window="x"] small')).toBeNull();
  });

  it('renders the English copy through the same keys', async () => {
    await i18n.changeLanguage('en');
    try {
      draw(readyRegion(summary([claude()])));
      expect(screen.getByRole('heading', { name: 'Subscription quota' })).toBeTruthy();
      expect(screen.getByText('1 signed-in subscription account')).toBeTruthy();
      expect(screen.getByText('Fable weekly limit')).toBeTruthy();
      expect(screen.getByText('Counts Fable only')).toBeTruthy();
    } finally {
      await i18n.changeLanguage('zh');
    }
  });
});
