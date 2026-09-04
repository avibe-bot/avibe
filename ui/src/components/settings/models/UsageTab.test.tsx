// @vitest-environment jsdom
// What the 用量 tab is allowed to SAY about a report. The derivations themselves
// are `usageProjection.test.ts`'s subject; every assertion here is about a claim
// the tab could make that the payload does not support — a span it was not
// served, an identity it cannot render, an empty window drawn as zeroes, or a
// shortfall presented as spare capacity.
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
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

/** A table's rows that carry figures — the ones headed by a row rather than by a column. */
const bodyRows = (table: HTMLElement) =>
  within(table).getAllByRole('row').filter((row) => within(row).queryAllByRole('rowheader').length > 0);

/**
 * One row's stated figures, its row header first — which is the order that decides
 * which column each of them answers.
 */
const figures = (row: HTMLElement) => [...within(row).getAllByRole('rowheader'), ...within(row).getAllByRole('cell')];

const surfaceCss = readFileSync(join(__dirname, 'modelHubSurface.css'), 'utf8');

const rule = (selector: string) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return surfaceCss.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`))?.[1] ?? '';
};

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
    expect(container.textContent).not.toContain(en.settings.models.usage.byDay.zero);
    // Every column says what its day carried, so a bar the report could not
    // measure still reads as a day that ran rather than as an unexplained sliver.
    // (Whether it is floored to one is CSS the DOM cannot answer for; the decision
    // behind it is `usageDayIsMetered`, asserted over columns in
    // `usageProjection.test.ts`.)
    //
    // The two kinds of day in this window state their tokens differently, which is
    // the point: a day of uncosted calls has no number to print, while the day
    // between them ran nothing and so really did cost zero.
    const readouts = [...container.querySelectorAll('.model-hub-usage-track')].map((track) => track.getAttribute('title'));
    expect(readouts).toEqual([
      'Aug 16, 2026 · — tokens · 4 requests',
      'Aug 17, 2026 · 0 tokens · 0 requests',
      'Aug 18, 2026 · — tokens · 4 requests',
    ]);
  });

  it('MH-USAGE-025: reads a window whose reports all came back zero as measured, not as missing', () => {
    // The mirror of MH-USAGE-021, one question over. Every call here WAS costed and
    // the answer was nothing — an explicit all-zero usage block, which
    // `extract_protocol_usage` forwards as a report rather than as an absence, so
    // `token_reports` equals `requests` while every token count is 0. Reading that
    // as 「no day reported its tokens」 would call a valid upstream answer missing
    // evidence, and blanking the figures would hide a cost the report does state.
    const free = counters({ requests: 4, token_reports: 4, input_tokens: 0, cached_input_tokens: 0, output_tokens: 0 });
    const { container } = draw(readyRegion(summary({
      from_day: '2026-08-17',
      to_day: '2026-08-18',
      window_days: 2,
      totals: free,
      sources: [source({ ...free, models: [model(free)] })],
      days: [{ ...free, day: '2026-08-18' }],
    })));

    expect(container.textContent).toContain(en.settings.models.usage.byDay.zero);
    expect(container.textContent).not.toContain(en.settings.models.usage.byDay.unreported);
    expect(container.textContent).not.toContain(en.settings.models.usage.byDay.quiet);
    // A measured zero is a number, and the requests card has no shortfall to state
    // about it either — nothing went unreported.
    expect(container.textContent).toContain(en.settings.models.usage.requests.reported);
    expect(container.textContent).not.toContain(en.settings.models.usage.tokens.none);
    // And the cached card may say what MH-USAGE-026 forbids it to say, because here
    // it is true: these calls were costed, and the cost included no input.
    expect(container.textContent).toContain(en.settings.models.usage.cached.none);
    const tokenFigures = [...container.querySelectorAll('[data-usage-token]')];
    expect(tokenFigures.length).toBeGreaterThan(0);
    for (const figure of tokenFigures) expect(figure.textContent).toBe('0');
  });

  it('MH-USAGE-026: every token figure on screen states its own coverage, whatever states it', () => {
    // The class MH-USAGE-021 and MH-USAGE-025 are two members of: a token figure
    // printed without asking whether anything reported it. Asked of every figure the
    // tab renders rather than of a list of the seven known ones — the marker is
    // emitted by the single door they all go through, so a card, cell, or column
    // added later is covered by existing rather than by an edit here.
    const uncosted = counters({ requests: 6, token_reports: 0, input_tokens: 0, cached_input_tokens: 0, output_tokens: 0 });
    const { container } = draw(readyRegion(summary({
      from_day: '2026-08-17',
      to_day: '2026-08-18',
      window_days: 2,
      totals: uncosted,
      sources: [source({ ...uncosted, models: [model(uncosted)] })],
      // Both reported days carried calls nobody costed, so no day of this window
      // may print a token number at all.
      days: [{ ...uncosted, day: '2026-08-17' }, { ...uncosted, day: '2026-08-18' }],
    })));

    const tokenFigures = [...container.querySelectorAll('[data-usage-token]')];
    expect(tokenFigures.length).toBeGreaterThan(0);
    for (const figure of tokenFigures) expect(figure.textContent).toBe(en.settings.models.usage.blank);
    // …and the sentences that interpolate a figure instead of holding one say the
    // same thing, since a tooltip and a note have no element of their own to mark.
    const readouts = [...container.querySelectorAll('.model-hub-usage-track')].map((track) => track.getAttribute('title'));
    expect(readouts).toEqual([
      'Aug 17, 2026 · — tokens · 6 requests',
      'Aug 18, 2026 · — tokens · 6 requests',
    ]);
    // The notes go the same way. The tokens card replaces the input/output split
    // rather than blanking both halves of it — 「— in · — out」 states a breakdown of
    // a number nobody gave — and the cached card must not read an absence of reports
    // as an absence of input.
    expect(container.textContent).toContain(en.settings.models.usage.tokens.none);
    expect(container.textContent).not.toContain(' in · ');
    expect(container.textContent).not.toContain(en.settings.models.usage.cached.none);
  });

  it('MH-USAGE-023: states every day of the window in text, not only in a pointer tooltip', () => {
    const { container } = draw(readyRegion(summary()));
    const tooltips = [...container.querySelectorAll('.model-hub-usage-track')].map((track) => track.getAttribute('title') ?? '');

    // A `title` opens on hover alone, and the chart is one image whose columns
    // assistive tech never enumerates — so the same three figures have to exist
    // as cells outside it. Read out of the tooltips rather than out of a list of
    // days: the two readings of a day cannot disagree, whatever the window holds.
    const rows = bodyRows(screen.getByRole('table', { name: /Metered tokens and requests per day/ }));
    expect(rows).toHaveLength(tooltips.length);
    rows.forEach((row, index) => {
      const said = tooltips[index].split(' · ');
      const stated = figures(row).map((figure) => figure.textContent ?? '');
      expect(stated).toHaveLength(said.length);
      stated.forEach((text, column) => expect(said[column]).toContain(text));
    });
  });

  it('MH-USAGE-027: keeps a 390px viewport within bounds and states long chart labels without clipping', () => {
    const longPeak = counters({ input_tokens: 123_456_789_012, output_tokens: 34_567_890 });
    const { container } = draw(readyRegion(summary({
      from_day: '2026-08-16',
      to_day: '2026-08-18',
      window_days: 3,
      totals: longPeak,
      sources: [source({ ...longPeak, models: [model(longPeak)] })],
      days: [{ ...longPeak, day: '2026-08-18' }],
    })));

    // A table's intrinsic columns can remain wider than the one-pixel `sr-only`
    // box applied to the table itself. A generic paint/layout container is the
    // boundary that keeps the complete semantic table out of visual geometry.
    const dailyTable = screen.getByRole('table', { name: /Metered tokens and requests per day/ });
    expect(dailyTable.classList.contains('sr-only')).toBe(false);
    expect(dailyTable.parentElement?.classList.contains('sr-only')).toBe(true);
    expect(dailyTable.parentElement?.classList.contains('model-hub-usage-a11y-table')).toBe(true);
    expect(rule('.model-hub-usage-a11y-table')).toContain('contain: strict');

    const axis = container.querySelector('.model-hub-usage-axis');
    const labels = [...(axis?.querySelectorAll('.model-hub-usage-axis-label') ?? [])];
    expect(labels).toHaveLength(3);
    expect(labels[1].textContent).toContain('123,491,356,902');
    expect(labels[1].textContent).toContain('Aug 18, 2026');
    for (const label of labels) expect(label.classList.contains('truncate')).toBe(false);

    // jsdom has no layout engine, so the narrow-width browser property is stated
    // by the same min-zero fractional grid and emergency wrapping Chrome uses.
    // No track owns an intrinsic pixel width, so this holds below and above 390px
    // rather than special-casing the reproduced fixture width.
    expect(rule('.model-hub-usage-axis')).toContain('grid-template-columns: var(--model-hub-usage-axis-columns)');
    expect(rule('.model-hub-usage-axis-label')).toContain('min-width: 0');
    expect(rule('.model-hub-usage-axis-label')).toContain('overflow-wrap: anywhere');
    expect(rule('.model-hub-usage-axis')).toContain('--model-hub-usage-axis-columns: minmax(0, 1fr) minmax(0, 2fr) minmax(0, 1fr)');
  });

  it('MH-USAGE-024: every figure the report states names the row and the column it answers', () => {
    // The class this closes: a per-row figure stated by visual position alone,
    // which no reader who cannot see the layout can recover. Asked of whatever
    // tables the tab renders rather than of a list of them, so a third panel of
    // figures is covered by existing instead of by an edit here.
    const { container } = draw(readyRegion(summary({
      sources: [
        source({ models: [model({ model_id: 'claude-opus-4-6', label: 'claude-opus-4-6' })] }),
        // The ragged shape: a Source with no metering timestamp, over a model that
        // has none by definition. Both stop before the last column.
        source({
          source_id: 'src_probe002',
          label: 'Probe source',
          last_metered_at: null,
          models: [model({ model_id: 'claude-haiku-4-5', label: 'claude-haiku-4-5' })],
        }),
      ],
    })));

    const named: string[] = [];
    for (const table of screen.getAllByRole('table')) {
      const headers = within(table).getAllByRole('columnheader');
      expect(headers.length).toBeGreaterThan(1);
      // Whether this table's headers can leave the accessibility tree, which is
      // what decides whether position alone is enough. jsdom loads no stylesheet,
      // so the class is the only readable statement of it.
      const stacks = /(^|\s)hidden(\s|$)/.test(headers[0].parentElement?.className ?? '');
      const rows = bodyRows(table);
      expect(rows.length).toBeGreaterThan(0);
      for (const row of rows) {
        const heads = within(row).getAllByRole('rowheader');
        expect(heads).toHaveLength(1);
        named.push(heads[0].textContent ?? '');
        const stated = figures(row);
        // A figure answers the column it sits in, so no row may end early: the
        // model row has no metering timestamp of its own and holds that column
        // open with an empty cell rather than shifting the ones before it under
        // the wrong header.
        expect(stated).toHaveLength(headers.length);
        stated.forEach((figure, column) => {
          const label = figure.querySelector('.model-hub-usage-cell-label');
          // Where the headers go, the cell's own label is the association that
          // stays — so every figure that states something has to carry one, and it
          // has to name the column the figure sits in. Two statements about one
          // cell, which a drifting column order would answer differently.
          if (stacks && figure !== heads[0] && figure.textContent !== '') expect(label).not.toBeNull();
          if (label !== null) expect(label.textContent).toBe(headers[column].textContent);
        });
      }
    }

    // …and the rows are the whole report's, not the subset that happens to render:
    // every Source, every model under it, and every day the chart draws.
    const days = container.querySelectorAll('.model-hub-usage-track').length;
    expect(named).toHaveLength(4 + days);
    expect(named).toEqual(expect.arrayContaining(['Contract source', 'Probe source', 'claude-opus-4-6', 'claude-haiku-4-5']));
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
