import { QuotaTab } from '../../src/components/settings/models/QuotaTab';
import { readyRegion } from '../../src/components/settings/models/regionRead';
import type { QuotaWindow, SourceQuota } from '../../src/components/settings/models/types';

const NOW = Date.parse('2026-09-25T08:00:00Z');
const HOUR = 3_600_000;
const iso = (ms: number) => new Date(ms).toISOString();
const limit = (over: Partial<QuotaWindow>): QuotaWindow => ({
  id: 'five_hour', kind: 'session', label: 'five_hour', used_pct: 38,
  window_seconds: 18_000, resets_at: iso(NOW + 2 * HOUR), ...over,
});
const weekly = { kind: 'weekly' as const, window_seconds: 604_800, resets_at: iso(NOW + 4 * 24 * HOUR) };

// Two accounts, one with an unrecognised window whose upstream name is one long word.
const sources: SourceQuota[] = [
  {
    source_id: 'src_claude', vendor: 'anthropic', display_name: 'Claude Max', account_label: 'alex@example.com',
    plan: 'max', fetched_at: iso(NOW - 2 * 60_000), state: 'ok',
    windows: [
      limit({}),
      limit({ id: 'seven_day', label: 'seven_day', used_pct: 61, ...weekly }),
      limit({ ...weekly, id: 'x', kind: 'other', label: 'unrecognised_upstream_limit_name_'.repeat(2), used_pct: 12 }),
    ],
  },
  {
    source_id: 'src_codex', vendor: 'openai', display_name: 'ChatGPT Pro', account_label: null,
    plan: 'pro', fetched_at: iso(NOW - 60_000), state: 'ok',
    windows: [
      limit({ id: 'primary_window', label: 'primary_window', used_pct: 70, resets_at: iso(NOW + 47 * 60_000) }),
      limit({ id: 'secondary_window', label: 'secondary_window', used_pct: 45, ...weekly }),
    ],
  },
];

export function QuotaFixture() {
  return <main className="model-hub-shell mx-auto min-w-0 max-w-5xl p-4">
    <QuotaTab quota={readyRegion({ refresh_interval_seconds: 300, sources })} now={NOW} />
  </main>;
}
