// @vitest-environment jsdom
// What the 用量 tab is allowed to SAY about a report. The derivations themselves
// are `usageProjection.test.ts`'s subject; every assertion here is about a claim
// the tab could make that the payload does not support — a span it was not
// served, an identity it cannot render, an empty window drawn as zeroes, or a
// shortfall presented as spare capacity.
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../../i18n/en.json';
import { degradedRegion, loadingRegion, readyRegion, unreadRegion, type RegionRead } from './regionRead';
import type { UsageByModel, UsageBySource, UsageCounters, UsageSummary } from './types';
import { UsageTab } from './UsageTab';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const counters = (over: Partial<UsageCounters> = {}): UsageCounters => ({
  requests: 12,
  token_reports: 12,
  input_tokens: 148230,
  cached_input_tokens: 96010,
  output_tokens: 4120,
  ...over,
});

const model = (over: Partial<UsageByModel> = {}): UsageByModel => ({
  ...counters(),
  model_id: 'claude-opus-4-6',
  label: 'claude-opus-4-6',
  ...over,
});

const source = (over: Partial<UsageBySource> = {}): UsageBySource => ({
  ...counters(),
  source_id: 'src_conform001',
  label: 'Contract source',
  last_metered_at: '2026-08-18T03:14:00+00:00',
  models: [model()],
  ...over,
});

const summary = (over: Partial<UsageSummary> = {}): UsageSummary => ({
  window_days: 30,
  from_day: '2026-07-20',
  to_day: '2026-08-18',
  totals: counters(),
  sources: [source()],
  days: [{ ...counters(), day: '2026-08-18' }],
  ...over,
});

const draw = (
  usage: RegionRead<UsageSummary>,
  over: { windowDays?: 7 | 30 | 60; onWindowChange?: (days: number) => void; onRetry?: () => void } = {},
) =>
  render(
    <I18nextProvider i18n={i18n}>
      <UsageTab
        usage={usage}
        windowDays={over.windowDays ?? 30}
        onWindowChange={over.onWindowChange ?? (() => {})}
        onRetry={over.onRetry}
      />
    </I18nextProvider>,
  );

afterEach(cleanup);

describe('UsageTab', () => {
  it('MH-USAGE-016: names the window the report was served over, not the one asked for', () => {
    // The server clamps `days` to retention and answers with what it covered.
    // Printing the request back would caption a 62-day report as 7.
    const { container } = draw(readyRegion(summary({ window_days: 62 })), { windowDays: 7 });
    expect(container.textContent).toContain('trailing 62 days');
    expect(container.textContent).not.toContain('trailing 7 days');
  });

  it('MH-USAGE-017: keeps a vanished Source identifiable and a vanished model unnamed', () => {
    // Asymmetric by contract: `src_*` is a string the user could have seen
    // elsewhere, a ledger key is a digest nobody typed.
    const key = 'claude-opus-4-6-20260514-thinking#9f2c1d';
    const { container } = draw(
      readyRegion(summary({ sources: [source({ label: null, models: [model({ label: null, model_id: key })] })] })),
    );
    expect(container.textContent).toContain('src_conform001');
    expect(container.textContent).toContain(en.settings.models.usage.bySource.goneSource);
    expect(container.textContent).toContain(en.settings.models.usage.bySource.goneModel);
    expect(container.textContent).not.toContain(key);
    expect(container.textContent).not.toContain('9f2c1d');
  });

  it('MH-USAGE-018: reads a shortfall as reports that never arrived', () => {
    // The schema forbids the other reading outright: the gap between requests
    // and token reports is missing evidence, never spare quota.
    const { container } = draw(readyRegion(summary({ totals: counters({ requests: 12, token_reports: 9 }) })));
    expect(container.textContent).toContain('3 requests came back with no token report');
    expect(container.textContent).not.toContain(en.settings.models.usage.requests.reported);
  });

  it('confirms full reporting when every request carried its tokens', () => {
    const { container } = draw(readyRegion(summary()));
    expect(container.textContent).toContain(en.settings.models.usage.requests.reported);
  });

  it('says nothing was metered instead of drawing a table of zeroes', () => {
    const { container } = draw(readyRegion(summary({ sources: [], days: [] })));
    expect(container.textContent).toContain(en.settings.models.usage.empty);
    expect(container.textContent).not.toContain(en.settings.models.usage.bySource.title);
    expect(container.textContent).not.toContain(en.settings.models.usage.byDay.title);
  });

  it('MH-USAGE-019: plots every day of the window, including the ones that carried nothing', () => {
    // One reported day inside a 30-day span. Drawing `days[]` directly would
    // close the gap and read as a month of continuous traffic.
    const { container } = draw(readyRegion(summary()));
    expect(container.querySelectorAll('.model-hub-usage-track')).toHaveLength(30);
    expect(screen.getByRole('img', { name: /Metered tokens per day/ })).toBeTruthy();
  });

  it('MH-USAGE-021: draws a day of calls whose tokens never came back, and names the window unreported', () => {
    // Every request in the window answered without a token report, which the
    // schema permits and the requests card already states as a shortfall. The
    // series has nothing to scale, so every bar sits on its floor — but the days
    // ran, and folding them into 「no metered turn」 would report our own missing
    // evidence as the user's idleness.
    const unreported = counters({ requests: 4, token_reports: 0, input_tokens: 0, cached_input_tokens: 0, output_tokens: 0 });
    const { container } = draw(readyRegion(summary({
      from_day: '2026-08-16',
      to_day: '2026-08-18',
      window_days: 3,
      totals: unreported,
      sources: [source({ ...unreported, models: [model(unreported)] })],
      days: [{ ...unreported, day: '2026-08-16' }, { ...unreported, day: '2026-08-18' }],
    })));

    expect(container.textContent).toContain(en.settings.models.usage.byDay.unreported);
    expect(container.textContent).not.toContain(en.settings.models.usage.byDay.quiet);
    // Every column says what its day carried, so a bar the report could not
    // measure still reads as a day that ran rather than as an unexplained sliver.
    // (Whether it is floored to one is CSS the DOM cannot answer for; the decision
    // behind it is `usageDayIsMetered`, asserted over columns in
    // `usageProjection.test.ts`.)
    const readouts = [...container.querySelectorAll('.model-hub-usage-track')].map((track) => track.getAttribute('title'));
    expect(readouts).toEqual([
      'Aug 16, 2026 · 0 tokens · 4 requests',
      'Aug 17, 2026 · 0 tokens · 0 requests',
      'Aug 18, 2026 · 0 tokens · 4 requests',
    ]);
  });

  it('MH-USAGE-023: states every day of the window in text, not only in a pointer tooltip', () => {
    const { container } = draw(readyRegion(summary()));
    const chart = screen.getByRole('img', { name: /Metered tokens per day/ });
    const tooltips = [...container.querySelectorAll('.model-hub-usage-track')].map((track) => track.getAttribute('title'));

    // A `title` opens on hover alone, and the chart is one image whose columns
    // assistive tech never enumerates — so the same readouts have to exist as
    // text outside it. Asserted against the tooltips rather than against a list
    // of days: the two readings cannot disagree, whatever the window holds.
    const items = [...container.querySelectorAll('li')].filter((item) => !chart.contains(item));
    expect(items.map((item) => item.textContent)).toEqual(tooltips);
  });

  it('keeps the last report on screen while a new one is read, and claims no failure', () => {
    const { container } = draw(degradedRegion(summary(), 'refreshing', false));
    expect(container.textContent).toContain('Contract source');
    expect(container.textContent).not.toContain(en.settings.models.toast.refreshFailed);
  });

  it('keeps the last report on screen when a refresh fails, under a retry', async () => {
    // The alternative is an empty state, which would read as "nothing was ever
    // metered" — a different claim from "the figure could not be refreshed".
    const onRetry = vi.fn();
    const { container } = draw(degradedRegion(summary(), 'read_failed', true), { onRetry });
    expect(container.textContent).toContain('Contract source');
    expect(container.textContent).toContain(en.settings.models.toast.refreshFailed);
    await userEvent.click(screen.getByRole('button', { name: en.settings.models.upstream.retry }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('offers a retry with no report to show when the first read never landed', () => {
    const { container } = draw(unreadRegion<UsageSummary>());
    expect(container.textContent).toContain(en.settings.models.toast.refreshFailed);
    expect(container.textContent).not.toContain(en.settings.models.usage.empty);
  });

  it('draws no report and no failure while the first read is in flight', () => {
    const { container } = draw(loadingRegion<UsageSummary>());
    expect(container.textContent).not.toContain(en.settings.models.toast.refreshFailed);
    expect(container.textContent).not.toContain(en.settings.models.usage.empty);
  });

  it('hands its owner a window the contract accepts, as a number', async () => {
    // The control speaks strings; `days` is a bounded number on the wire.
    const onWindowChange = vi.fn();
    draw(readyRegion(summary()), { onWindowChange });
    await userEvent.click(screen.getByRole('radio', { name: '7d' }));
    expect(onWindowChange).toHaveBeenCalledWith(7);
  });
});
