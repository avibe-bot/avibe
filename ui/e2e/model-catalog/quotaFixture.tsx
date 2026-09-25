import { QuotaTab } from '../../src/components/settings/models/QuotaTab';
import { readyRegion } from '../../src/components/settings/models/regionRead';
import type { PricedUsage, QuotaValueTotals, QuotaWindow, SourceQuota, SourceQuotaValue } from '../../src/components/settings/models/types';

const NOW = Date.parse('2026-09-25T08:00:00Z');
const HOUR = 3_600_000;
const iso = (ms: number) => new Date(ms).toISOString();
const limit = (over: Partial<QuotaWindow>): QuotaWindow => ({
  id: 'five_hour', kind: 'session', label: 'five_hour', used_pct: 38,
  window_seconds: 18_000, resets_at: iso(NOW + 2 * HOUR), ...over,
});
const weekly = { kind: 'weekly' as const, window_seconds: 604_800, resets_at: iso(NOW + 4 * 24 * HOUR) };

const priced = (api_cost_usd: number, excluded_tokens = 0): PricedUsage => ({
  api_cost_usd, excluded_tokens, api_cost_lower_bound: false,
});
const valued = (over: Partial<SourceQuotaValue>): SourceQuotaValue => ({
  currency: 'USD', price_table_date: '2026-09-23', plan_key: null, fee_usd: null, multiple: null,
  week: priced(0),
  period: { basis: 'rolling_30d', from_day: '2026-08-27', to_day: '2026-09-25', renews_on: null, ...priced(0) },
  ...over,
});

// Two accounts, one with an unrecognised window whose upstream name is one long word;
// one paid off many times over on a known cycle, one short on the trailing 30 days.

const sources: SourceQuota[] = [
  {
    source_id: 'src_claude', vendor: 'anthropic', display_name: 'Claude Max', account_label: 'alex@example.com',
    plan: 'max', fetched_at: iso(NOW - 2 * 60_000), state: 'ok',
    windows: [
      limit({}),
      limit({ id: 'seven_day', label: 'seven_day', used_pct: 61, ...weekly }),
      limit({ ...weekly, id: 'x', kind: 'other', label: 'unrecognised_upstream_limit_name_'.repeat(2), used_pct: 12 }),
    ],
    value: valued({
      plan_key: 'claude_max_20x', fee_usd: 200, multiple: 6.4201, week: priced(412.37),
      period: {
        basis: 'billing_cycle', from_day: '2026-09-05', to_day: '2026-09-25', renews_on: '2026-10-05',
        ...priced(1284.02),
      },
    }),
  },
  {
    source_id: 'src_codex', vendor: 'openai', display_name: 'ChatGPT Pro', account_label: null,
    plan: 'pro', fetched_at: iso(NOW - 60_000), state: 'ok',
    windows: [
      limit({ id: 'primary_window', label: 'primary_window', used_pct: 70, resets_at: iso(NOW + 47 * 60_000) }),
      limit({ id: 'secondary_window', label: 'secondary_window', used_pct: 45, ...weekly }),
    ],
    value: valued({
      plan_key: 'chatgpt_pro', fee_usd: 200, multiple: 0.6125, week: priced(38.2, 12_400),
      period: { basis: 'rolling_30d', from_day: '2026-08-27', to_day: '2026-09-25', renews_on: null, ...priced(122.5, 12_400) },
    }),
  },
];

const value: QuotaValueTotals = {
  currency: 'USD', price_table_date: '2026-09-23', week: priced(450.57, 12_400),
  period: { sources: 2, fee_usd: 400, multiple: 3.5163, ...priced(1406.52, 12_400) },
};

export function QuotaFixture() {
  return <main className="model-hub-shell mx-auto min-w-0 max-w-5xl p-4">
    <QuotaTab quota={readyRegion({ refresh_interval_seconds: 300, sources, value })} now={NOW} />
  </main>;
}
