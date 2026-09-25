// @vitest-environment jsdom
// What the 订阅额度 tab says about a quota report: the windows it names, the pace
// it claims, and what it admits about a reading that is not current.
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { degradedRegion, loadingRegion, readyRegion, type RegionRead } from './regionRead';
import { QuotaTab } from './QuotaTab';
import { PAYBACK_CLEAR_MULTIPLE, quotaPayback, windowLeftPct, windowPace, windowUsedPct } from './quotaProjection';
import type { PricedUsage, QuotaSummary, QuotaValueTotals, QuotaWindow, SourceQuota, SourceQuotaValue } from './types';

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
    expect(within(card).getByText('Max')).toBeTruthy();
    expect(within(card).getByText('alex@example.com')).toBeTruthy();

    // Fable at 86% four days before a 7-day reset is spending faster than time.
    const fable = card.querySelector('[data-quota-window="fable"]') as HTMLElement;
    expect(fable.textContent).toMatch(/用得偏快，预计 .+后用完/);
    expect(within(fable).getByRole('meter', { name: 'Fable 每周额度 已用 86%' })).toBeTruthy();
    expect(fable.textContent).toContain('14%');
    const session = card.querySelector('[data-quota-window="five_hour"]') as HTMLElement;
    expect(session.textContent).toContain('2 小时后重置');
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

    // A server that predates the value block draws the quota alone: no price, no currency.
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

  it('names an unrecognised identifier in words, its span in the window wording, with the full name as its title', async () => {
    const title = () => screen.getByRole('article').querySelector('[data-quota-window="x"] strong')!;
    draw(readyRegion(summary([claude({ windows: [window({ id: 'x', kind: 'other', label: 'seven_day_cowork' })] })])));
    expect(title().textContent).toBe('7 天 Cowork');
    expect(title().getAttribute('title')).toBe('7 天 Cowork');
    expect(screen.queryByText(/seven_day/)).toBeNull();
    await i18n.changeLanguage('en');
    try {
      cleanup();
      draw(readyRegion(summary([claude({ windows: [
        window({ id: 'x', kind: 'other', label: 'seven_day_cowork' }),
        window({ id: 'y', kind: 'model_weekly', label: 'claude_opus' }),
      ] })])));
      expect(title().textContent).toBe('7-day Cowork');
      expect(screen.getByText('Claude Opus weekly limit')).toBeTruthy();
      expect(screen.getByRole('meter', { name: '7-day Cowork: 38% used' })).toBeTruthy();
    } finally {
      await i18n.changeLanguage('zh');
    }
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

  it('MH-QUOTA-027: names a reported plan in words, never as a bare upstream id', async () => {
    const badge = (vendor: string, plan: string) => {
      cleanup();
      draw(readyRegion(summary([codex({ vendor, plan, display_name: 'Account' })])));
      return screen.getByRole('article').querySelector('.model-hub-quota-plan')!.textContent;
    };
    // Known ids, whatever their casing or separators, and with or without the vendor's prefix.
    expect(badge('openai', 'prolite')).toBe('Pro 5x');
    expect(badge('codex', 'ProLite')).toBe('Pro 5x');
    expect(badge('openai', 'chatgpt_pro_5x')).toBe('Pro 5x');
    expect(badge('openai', 'pro')).toBe('Pro');
    expect(badge('openai', 'business')).toBe('Business');
    expect(badge('anthropic', 'claude_max_20x')).toBe('Max 20x');
    expect(badge('anthropic', 'max-5x')).toBe('Max 5x');
    // An id another vendor's table names is not borrowed; an unknown id reads as words.
    expect(badge('anthropic', 'prolite')).toBe('Prolite');
    expect(badge('anthropic', 'chatgpt_pro')).toBe('Chatgpt Pro');
    expect(badge('openai', 'claude_pro')).toBe('Claude Pro');
    expect(badge('openai', 'team_plus-annual')).toBe('Team Plus Annual');
    expect(badge('xai', 'supergrok')).toBe('Supergrok');
    // An unknown id still drops the vendor prefix the badge never repeats.
    expect(badge('openai', 'chatgpt_team_plus')).toBe('Team Plus');
    await i18n.changeLanguage('en');
    try {
      expect(badge('openai', 'prolite')).toBe('Pro 5x');
    } finally {
      await i18n.changeLanguage('zh');
    }
  });

  it('MH-QUOTA-028: a countdown drops its zero second part', async () => {
    const reset = (ms: number) => {
      cleanup();
      draw(readyRegion(summary([claude({ windows: [window({ used_pct: 1, window_seconds: 604_800, kind: 'weekly', resets_at: iso(NOW + ms) })] })])));
      return screen.getByRole('article').querySelector('.model-hub-quota-reset')!.textContent;
    };
    expect(reset(5 * 24 * HOUR)).toBe('5 天后重置');
    expect(reset(5 * 24 * HOUR + 3 * HOUR)).toBe('5 天 3 小时后重置');
    expect(reset(HOUR)).toBe('1 小时后重置');
    expect(reset(HOUR + 20 * 60_000)).toBe('1 小时 20 分后重置');
    expect(reset(20 * 60_000)).toBe('20 分钟后重置');
    await i18n.changeLanguage('en');
    try {
      expect(reset(5 * 24 * HOUR)).toBe('Resets in 5d');
      expect(reset(HOUR)).toBe('Resets in 1h');
      expect(reset(HOUR + 20 * 60_000)).toBe('Resets in 1h 20m');
    } finally {
      await i18n.changeLanguage('zh');
    }
  });

  it('MH-QUOTA-029: shows a countdown alone and keeps its exact moment in a tooltip and the accessible name', async () => {
    const resetAt = NOW + 5 * 24 * HOUR + 7 * 60 * 60_000;
    const stamp = (at: number) => new Intl.DateTimeFormat('zh', {
      weekday: 'short', year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).format(at);
    const exact = stamp(resetAt);
    const { container } = draw(readyRegion(summary([
      claude({ windows: [window({ id: 'seven_day', kind: 'weekly', used_pct: 20, window_seconds: 604_800, resets_at: iso(resetAt) })] }),
      codex(),
    ])));
    const row = container.querySelector('[data-quota-window="seven_day"]') as HTMLElement;
    const reset = row.querySelector('.model-hub-quota-reset') as HTMLElement;
    // The line reads the countdown alone; the clock is not drawn beside it.
    expect(reset.textContent).toBe('5 天 7 小时后重置');
    expect(container.querySelector('.model-hub-quota-reset em')).toBeNull();
    const trigger = within(reset).getByRole('button', { name: `5 天 7 小时后重置 (${exact})` });
    expect(document.body.textContent).not.toContain(exact);

    // Keyboard focus opens it and blur closes it; a tap toggles it.
    fireEvent.focus(trigger);
    expect((await screen.findByRole('dialog')).textContent).toBe(exact);
    fireEvent.blur(trigger);
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.pointerDown(trigger, { pointerType: 'touch' });
    fireEvent.focus(trigger);
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(trigger);
    expect(screen.getByRole('dialog').textContent).toBe(exact);
    fireEvent.pointerDown(trigger, { pointerType: 'touch' });
    fireEvent.click(trigger);
    expect(screen.queryByRole('dialog')).toBeNull();
    // A mouse hover opens it too.
    fireEvent.pointerEnter(trigger, { pointerType: 'mouse' });
    expect(screen.getByRole('dialog').textContent).toBe(exact);
    fireEvent.pointerLeave(trigger, { pointerType: 'mouse' });
    expect(screen.queryByRole('dialog')).toBeNull();

    // Every other countdown takes the same form: the upcoming-reset chips, the
    // spent-limit note, and the fast-pace forecast.
    const chips = within(screen.getByLabelText('即将重置')).getAllByRole('button');
    expect(chips.map((chip) => chip.textContent)).toContain('47 分钟');
    expect(chips.find((chip) => chip.textContent === '47 分钟')!.getAttribute('aria-label')).toBe(`47 分钟 (${stamp(NOW + 47 * 60_000)})`);
    expect(screen.getByRole('button', { name: `ChatGPT Pro · 47 分钟后恢复 (${stamp(NOW + 47 * 60_000)})` })).toBeTruthy();
    cleanup();
    draw(readyRegion(summary([claude()])));
    const fable = document.querySelector('[data-quota-window="fable"] .model-hub-quota-pace') as HTMLElement;
    expect(fable.textContent).toMatch(/^用得偏快，预计 .+后用完$/);
    expect(within(fable).getByRole('button').getAttribute('aria-label')).toMatch(/^用得偏快，预计 .+后用完 \(.+\)$/);
  });

  it('keeps the exact time of a retained reading behind its 「ago」 banner', () => {
    const fetchedAt = NOW - 3 * HOUR - 12 * 60_000;
    draw(readyRegion(summary([codex({ state: 'stale', fetched_at: iso(fetchedAt) })])));
    const banner = within(screen.getByRole('article')).getByRole('status');
    expect(banner.textContent).toBe('暂时读不到最新额度，下面是 3 小时 12 分前的数据');
    const stamp = new Intl.DateTimeFormat('zh', {
      weekday: 'short', year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).format(fetchedAt);
    expect(within(banner).getByRole('button').getAttribute('aria-label')).toBe(`${banner.textContent} (${stamp})`);
  });

  describe('API-price value', () => {
    const priced = (api_cost_usd: number, over: Partial<PricedUsage> = {}): PricedUsage => ({
      api_cost_usd, excluded_tokens: 0, api_cost_lower_bound: false, ...over,
    });
    const sourceValue = (period: number, fee: number | null, over: Partial<SourceQuotaValue> = {}): SourceQuotaValue => ({
      currency: 'USD',
      price_table_date: '2026-09-23',
      plan_key: fee === null ? null : 'claude_max_20x',
      fee_usd: fee,
      multiple: fee === null ? null : period / fee,
      week: priced(period / 3),
      period: { basis: 'billing_cycle', from_day: '2026-09-05', to_day: '2026-09-25', renews_on: '2026-10-05', ...priced(period) },
      ...over,
    });
    const totals = (week: number, period: { cost: number; fee: number } | null, over: Partial<QuotaValueTotals> = {}): QuotaValueTotals => ({
      currency: 'USD',
      price_table_date: '2026-09-23',
      week: priced(week),
      period: period && { sources: 1, fee_usd: period.fee, multiple: period.cost / period.fee, ...priced(period.cost) },
      ...over,
    });
    const valued = (sources: SourceQuota[], value: QuotaValueTotals, pending?: string[]): QuotaSummary => ({
      ...summary(sources), value, ...(pending ? { pending } : {}),
    });

    it('MH-QUOTA-021: classifies payback at the 1× and 1.1× thresholds', () => {
      expect(PAYBACK_CLEAR_MULTIPLE).toBe(1.1);
      expect(quotaPayback(99, 100)).toMatchObject({ kind: 'short', shortfallUsd: 1 });
      expect(quotaPayback(100, 100)).toMatchObject({ kind: 'even', surplusUsd: 0 });
      expect(quotaPayback(109, 100)?.kind).toBe('even');
      expect(quotaPayback(110, 100)).toMatchObject({ kind: 'paid', surplusUsd: 10 });
      expect(quotaPayback(50, 0)).toBeNull();
      expect(quotaPayback(Number.NaN, 20)).toBeNull();
      // On each account card, in words.
      const line = (cost: number) => {
        cleanup();
        draw(readyRegion(valued([claude({ value: sourceValue(cost, 100) })], totals(cost / 3, { cost, fee: 100 }))));
        return screen.getByRole('article').querySelector('[data-quota-payback]')!.textContent;
      };
      expect(line(99)).toBe('还差 $1.00 回本');
      expect(line(100)).toBe('刚好回本');
      expect(line(109)).toBe('刚好回本');
      expect(line(110)).toBe('回本 1.1 倍');
      // Rounded down: 1.99× is not claimed as 2.0×.
      expect(line(199)).toBe('回本 1.9 倍');
    });

    it('MH-QUOTA-022: states the week and period value against the fee, with the price table date', () => {
      const { container } = draw(readyRegion(valued(
        [claude({ value: sourceValue(412.5, 200) }), codex({ value: sourceValue(18, 200, { plan_key: 'chatgpt_pro', week: priced(6) }) })],
        totals(143.5, { cost: 430.5, fee: 400 }),
      )));
      expect(screen.getAllByText('近 7 天按 API 价格').length).toBe(3);
      const stats = container.querySelector('.model-hub-quota-stats')!;
      expect(stats.textContent).toContain('$143.50');
      expect(stats.textContent).toContain('本期回本');
      expect(stats.textContent).toContain('1.0 倍月费');
      expect(stats.textContent).toContain('已回本，多薅了 $30.50');
      const [claudeCard, codexCard] = screen.getAllByRole('article');
      expect(within(claudeCard).getByText('本期按 API 价格')).toBeTruthy();
      expect(claudeCard.textContent).toContain('$412.50');
      expect(claudeCard.textContent).toContain('回本 2.0 倍');
      // Counted between host-calendar days (2026-09-25 → 2026-10-05), whatever the browser's zone.
      expect(claudeCard.textContent).toContain('距下次续费10 天');
      // The renewal day is a host-calendar day: its weekday is that day's, whatever the browser's zone.
      expect(within(claudeCard).getByRole('button', { name: '10 天 (2026/10/5周一)' })).toBeTruthy();
      expect(codexCard.textContent).toContain('还差 $182.00 回本');
      const foot = container.querySelector('.model-hub-quota-foot')!.textContent!;
      expect(foot).toContain('进度条是已用的部分');
      expect(foot).toContain('“按 API 价格”指同样的用量按官方 API 标价要花多少钱，用来看订阅值不值，不是实际扣费。');
      expect(foot).toContain('价格表日期 2026-09-23。');
    });

    it('MH-QUOTA-023: short of the fee says how much, and an unknown plan hides payback but keeps the value', () => {
      draw(readyRegion(valued([claude({ value: sourceValue(150, 200) })], totals(50, { cost: 150, fee: 200 }))));
      expect(document.querySelector('.model-hub-quota-stats')!.textContent).toContain('还差 $50.00 回本');
      expect(document.querySelector('.model-hub-quota-stats')!.textContent).toContain('0.7 倍月费');
      cleanup();

      const { container } = draw(readyRegion(valued(
        [claude({ plan: null, value: sourceValue(90, null, { period: { basis: 'rolling_30d', from_day: '2026-08-27', to_day: '2026-09-25', renews_on: null, ...priced(90) } }) })],
        totals(30, null),
      )));
      const card = screen.getByRole('article');
      expect(card.textContent).toContain('$30.00');
      expect(within(card).getByText('近 30 天按 API 价格')).toBeTruthy();
      expect(card.textContent).toContain('$90.00');
      expect(card.querySelector('[data-quota-payback]')).toBeNull();
      expect(card.textContent).not.toContain('回本');
      expect(card.textContent).not.toContain('距下次续费');
      expect(container.querySelector('.model-hub-quota-stats')!.textContent).toContain('套餐未知，暂不计算回本');
    });

    it('MH-QUOTA-024: a model with no price says so and is counted, never priced as zero', () => {
      const unknown = priced(0, { excluded_tokens: 12_000 });
      draw(readyRegion(valued(
        [claude({ value: sourceValue(0, 200, { week: unknown, period: { basis: 'billing_cycle', from_day: '2026-09-05', to_day: '2026-09-25', renews_on: '2026-10-05', ...unknown } }) })],
        totals(0, { cost: 0, fee: 200 }, { week: unknown }),
      )));
      const card = screen.getByRole('article');
      expect(within(card).getAllByText('暂无价格').length).toBe(2);
      expect(card.textContent).toContain('12,000 tokens 暂无价格，未计入');
      expect(document.querySelector('.model-hub-quota-stats')!.textContent).toContain('暂无价格');
      cleanup();
      // Partly priced: the figure stands and the leftover is named; a lower bound says ≥.
      draw(readyRegion(valued(
        [claude({ value: sourceValue(40, 200, { week: priced(40, { excluded_tokens: 500, api_cost_lower_bound: true }) }) })],
        totals(40, { cost: 40, fee: 200 }, { week: priced(40, { excluded_tokens: 500, api_cost_lower_bound: true }) }),
      )));
      expect(screen.getByRole('article').textContent).toContain('≥ $40.00');
      expect(document.querySelector('.model-hub-quota-stats')!.textContent).toContain('500 tokens 暂无价格，未计入');
      cleanup();
      // A floor short of the fee names no shortfall; a floor past it still proves the payback.
      const floor = (cost: number) => priced(cost, { excluded_tokens: 500, api_cost_lower_bound: true });
      draw(readyRegion(valued(
        [claude({ value: sourceValue(40, 200, { period: { basis: 'billing_cycle', from_day: '2026-09-05', to_day: '2026-09-25', renews_on: '2026-10-05', ...floor(40) } }) })],
        totals(40, { cost: 40, fee: 200 }, { period: { sources: 1, fee_usd: 200, multiple: 0.2, ...floor(40) } }),
      )));
      expect(document.body.textContent).not.toContain('还差');
      expect(screen.getByRole('article').textContent).toContain('部分用量暂无价格，差额暂不显示');
      expect(document.querySelector('.model-hub-quota-stats')!.textContent).toContain('≥ 0.2 倍');
      cleanup();
      draw(readyRegion(valued(
        [claude({ value: sourceValue(400, 200, { period: { basis: 'billing_cycle', from_day: '2026-09-05', to_day: '2026-09-25', renews_on: '2026-10-05', ...floor(400) } }) })],
        totals(400, { cost: 400, fee: 200 }, { period: { sources: 1, fee_usd: 200, multiple: 2, ...floor(400) } }),
      )));
      expect(screen.getByRole('article').textContent).toContain('回本 ≥ 2.0 倍');
      expect(document.querySelector('.model-hub-quota-stats')!.textContent).toContain('已回本，多薅了 ≥ $200.00');
      cleanup();
      // A floor just past the fee is at least paid back, never 「刚好回本」.
      draw(readyRegion(valued(
        [claude({ value: sourceValue(210, 200, { period: { basis: 'billing_cycle', from_day: '2026-09-05', to_day: '2026-09-25', renews_on: '2026-10-05', ...floor(210) } }) })],
        totals(210, { cost: 210, fee: 200 }, { period: { sources: 1, fee_usd: 200, multiple: 1.05, ...floor(210) } }),
      )));
      expect(screen.getByRole('article').textContent).not.toContain('刚好回本');
      expect(screen.getByRole('article').textContent).toContain('回本 ≥ 1.0 倍');
      cleanup();
      // A floor of zero (calls whose size went unreported) is still a floor: 「≥ $0.00」, never an exact zero or a shortfall.
      const zero = priced(0, { api_cost_lower_bound: true });
      draw(readyRegion(valued(
        [claude({ value: sourceValue(0, 200, { week: zero, period: { basis: 'billing_cycle', from_day: '2026-09-05', to_day: '2026-09-25', renews_on: '2026-10-05', ...zero } }) })],
        totals(0, { cost: 0, fee: 200 }, { week: zero, period: { sources: 1, fee_usd: 200, multiple: 0, ...zero } }),
      )));
      expect(document.body.textContent).toContain('≥ $0.00');
      expect(document.body.textContent).not.toMatch(/(?<!≥ )\$0\.00/);
      expect(document.body.textContent).not.toContain('还差');
    });

    it('MH-QUOTA-025: a Source still being read shows a loading line instead of 「暂时读不到」', () => {
      const unread = claude({ state: 'error', error_key: 'models.quota.error.unavailable', windows: [], fetched_at: null, plan: null });
      draw(readyRegion({ ...summary([unread]), pending: ['src_claude'] }));
      const card = screen.getByRole('article');
      expect(within(card).getByRole('status').textContent).toBe('正在读取额度…');
      expect(card.textContent).not.toContain('暂时读不到额度');
      cleanup();
      draw(readyRegion(summary([unread])));
      expect(screen.getByRole('article').textContent).toContain('暂时读不到额度');
    });

    it('MH-QUOTA-026: renders the value copy in English through the same keys', async () => {
      await i18n.changeLanguage('en');
      try {
        const { container } = draw(readyRegion(valued([claude({ value: sourceValue(412.5, 200) })], totals(137.5, { cost: 412.5, fee: 200 }))));
        expect(container.querySelector('.model-hub-quota-stats')!.textContent).toContain('Last 7 days at API price');
        expect(container.textContent).toContain('Paid back 2.0×');
        expect(container.textContent).toContain('Paid back, $212.50 ahead');
        expect(container.textContent).toContain('Price table from 2026-09-23.');
        expect(container.textContent).not.toMatch(/[一-鿿]/);
      } finally {
        await i18n.changeLanguage('zh');
      }
    });
  });
});
