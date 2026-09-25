// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../../i18n/en.json';
import { readyRegion } from './regionRead';
import type { UsageBucket, UsageBucketRow, UsageCounters, UsageReport, UsageWindowKey } from './types';
import { UsageTab } from './UsageTab';
import { buildUsageCsv, csvCell } from './usageCsv';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const counters = (over: Partial<UsageCounters> = {}): UsageCounters => ({
  requests: 2,
  token_reports: 2,
  input_tokens: 100,
  cached_input_tokens: 25,
  output_tokens: 40,
  ...over,
});

const row = (over: Partial<UsageBucketRow> = {}): UsageBucketRow => ({
  ...counters(),
  source_id: 'source-a',
  model_id: 'model-a',
  ...over,
});

const bucket = (
  key: string,
  rows: UsageBucketRow[] = [row()],
  over: Partial<UsageBucket> = {},
): UsageBucket => ({
  key,
  start_at: `2026-09-24T${key}:00:00+08:00`,
  end_at: `2026-09-24T${String(Number(key) + 1).padStart(2, '0')}:00:00+08:00`,
  history_complete: true,
  rows,
  ...over,
});

const report = (over: Partial<UsageReport> = {}): UsageReport => ({
  window_days: 1,
  from_day: '2026-09-24',
  to_day: '2026-09-24',
  totals: counters({ requests: 6, token_reports: 6, input_tokens: 300, cached_input_tokens: 75, output_tokens: 120 }),
  sources: [{
    source_id: 'source-a',
    label: 'Shared label',
    last_metered_at: '2026-09-24T02:00:00+08:00',
    ...counters({ requests: 6, token_reports: 6, input_tokens: 300, cached_input_tokens: 75, output_tokens: 120 }),
    models: [{ model_id: 'model-a', label: 'Shared model', ...counters({ requests: 6, token_reports: 6, input_tokens: 300, cached_input_tokens: 75, output_tokens: 120 }) }],
  }],
  days: [],
  window_key: '24h',
  granularity: 'hour',
  from_at: '2026-09-24T00:00:00+08:00',
  to_at: '2026-09-24T03:00:00+08:00',
  buckets: [bucket('00'), bucket('01'), bucket('02')],
  ...over,
});

const draw = (
  value: UsageReport,
  over: { windowKey?: UsageWindowKey; onWindowChange?: (window: UsageWindowKey) => void } = {},
) => render(
  <I18nextProvider i18n={i18n}>
    <UsageTab
      usage={readyRegion(value)}
      windowKey={over.windowKey ?? '24h'}
      onWindowChange={over.onWindowChange ?? vi.fn()}
    />
  </I18nextProvider>,
);

afterEach(cleanup);

describe('UsageTab', () => {
  it('defaults to 24 hours and forwards the selected contract window', async () => {
    const onWindowChange = vi.fn();
    draw(report(), { onWindowChange });

    expect(screen.getByRole('radio', { name: '24 hours', checked: true })).toBeTruthy();
    await userEvent.click(screen.getByRole('radio', { name: '7 days' }));
    expect(onWindowChange).toHaveBeenCalledWith('7d');
  });

  it('MH-USAGE-016: names the window the report was served over, not the one asked for', () => {
    const { container } = draw(report({
      from_at: '2026-08-18T00:00:00+08:00',
      to_at: '2026-08-18T03:00:00+08:00',
    }), { windowKey: '7d' });

    const heading = container.querySelector('.model-hub-usage-heading');
    expect(heading?.textContent).toContain('Aug 18, 2026');
    expect(heading?.textContent).not.toContain('Sep 24, 2026');
  });

  it('MH-USAGE-017: keeps a vanished Source identifiable and a vanished model unnamed', () => {
    const vanishedModelId = 'model-opaque-digest-9f2c1d';
    const { container } = draw(report({
      sources: [{
        source_id: 'source-vanished',
        label: null,
        last_metered_at: null,
        ...counters(),
        models: [{ model_id: vanishedModelId, label: null, ...counters() }],
      }],
      buckets: [bucket('00', [row({ source_id: 'source-vanished', model_id: vanishedModelId })])],
    }));

    expect(container.textContent).toContain('source-vanished');
    expect(container.textContent).toContain('Unknown model');
    expect(container.textContent).not.toContain(vanishedModelId);
  });

  it('MH-USAGE-018: reads a shortfall as reports that never arrived', () => {
    const shortfall = counters({
      requests: 4,
      token_reports: 1,
      input_tokens: 100,
      cached_input_tokens: 25,
      output_tokens: 40,
    });
    const { container } = draw(report({
      totals: shortfall,
      sources: [],
      buckets: [bucket('00', [row(shortfall)])],
    }));

    expect(container.textContent).toContain('3 requests have no token report');
    expect(container.textContent).toContain('All tokens');
  });

  it('uses the translated historical-model label for filters, chart, table, and collisions', async () => {
    const translated = createInstance();
    await translated.use(initReactI18next).init({
      lng: 'de',
      fallbackLng: 'en',
      resources: {
        en: { translation: en },
        de: { translation: { settings: { models: { usage: { unknownModel: 'Historisches Modell' } } } } },
      },
      interpolation: { escapeValue: false },
    });
    const value = report({
      sources: [{
        source_id: 'source-a',
        label: 'Supplier',
        last_metered_at: null,
        ...counters(),
        models: [{ model_id: 'model-a', label: 'Historisches Modell', ...counters() }],
      }],
      buckets: [bucket('00', [row({ model_id: 'removed-model' }), row()])],
    });
    const { container } = render(
      <I18nextProvider i18n={translated}>
        <UsageTab usage={readyRegion(value)} windowKey="24h" onWindowChange={vi.fn()} />
      </I18nextProvider>,
    );
    const labels = [
      'Supplier · Historisches Modell · source-a · removed-model',
      'Supplier · Historisches Modell · source-a · model-a',
    ];
    for (const label of labels) expect(within(screen.getByRole('table')).getByText(label)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'All Model' }));
    for (const label of labels) expect(screen.getByRole('option', { name: new RegExp(label) })).toBeTruthy();
    fireEvent.pointerDown(document.body);
    await userEvent.click(screen.getByRole('button', { name: 'Model' }));
    for (const label of labels) expect(container.querySelector('.model-hub-usage-legend')?.textContent).toContain(label);
    expect(container.textContent).not.toContain('Unknown model');
  });

  it('formats every percentage in the active locale even when translations fall back', async () => {
    const translated = createInstance();
    await translated.use(initReactI18next).init({
      lng: 'de-DE',
      fallbackLng: 'en',
      resources: { en: { translation: en } },
      interpolation: { escapeValue: false },
    });
    const { container } = render(
      <I18nextProvider i18n={translated}>
        <UsageTab usage={readyRegion(report({
          buckets: [bucket('00', [
            row({ input_tokens: 2, cached_input_tokens: 1, output_tokens: 0 }),
            row({ model_id: 'model-b', input_tokens: 14, cached_input_tokens: 1, output_tokens: 0 }),
          ])],
        }))} windowKey="24h" onWindowChange={vi.fn()} />
      </I18nextProvider>,
    );

    expect(container.querySelector('.model-hub-usage-stat-grid')?.textContent).toContain('12,5\u00a0%');
    expect(container.querySelector('tbody')?.textContent).toContain('12,5\u00a0%');
    expect(container.querySelector('tbody')?.textContent).toContain('87,5\u00a0%');
    expect(container.querySelector('tfoot')?.textContent).toContain('100\u00a0%');
    expect(container.textContent).not.toContain('12.5%');
  });

  it('exposes the current sort direction together with the actual detail row order', async () => {
    const { container } = draw(report({
      buckets: [bucket('00', [
        row({ input_tokens: 2, cached_input_tokens: 1, output_tokens: 0 }),
        row({ model_id: 'model-b', input_tokens: 14, cached_input_tokens: 1, output_tokens: 0 }),
      ])],
    }));
    const header = screen.getByRole('columnheader', { name: /All tokens/ });
    const amounts = () => within(container.querySelector('tbody')!).getAllByRole('row').map(
      (detail) => within(detail).getAllByRole('cell')[5]?.textContent,
    );

    expect(header.getAttribute('aria-sort')).toBe('descending');
    expect(within(header).getByRole('button', { name: /Sorted descending/ })).toBeTruthy();
    expect(amounts()).toEqual(['14', '2']);
    await userEvent.click(within(header).getByRole('button'));
    expect(header.getAttribute('aria-sort')).toBe('ascending');
    expect(within(header).getByRole('button', { name: /Sorted ascending/ })).toBeTruthy();
    expect(amounts()).toEqual(['2', '14']);
    await userEvent.click(within(header).getByRole('button'));
    expect(header.getAttribute('aria-sort')).toBe('descending');
    expect(amounts()).toEqual(['14', '2']);
  });

  it('MH-USAGE-019: plots every bucket in the window, including the ones that carried nothing', () => {
    const { container } = draw(report({
      buckets: [bucket('00', []), bucket('01', []), bucket('02', [row()])],
    }));

    expect(screen.getAllByRole('button', { name: /Usage bucket/ })).toHaveLength(3);
    expect(container.textContent).toContain('3 hourly points');
  });

  it('MH-USAGE-021: draws calls whose tokens never came back, and names the window unreported', () => {
    const unreported = counters({
      requests: 4,
      token_reports: 0,
      input_tokens: 0,
      cached_input_tokens: 0,
      output_tokens: 0,
    });
    const { container } = draw(report({
      totals: unreported,
      sources: [],
      buckets: [bucket('00', [row(unreported)])],
    }));

    expect(container.textContent).toContain('Some requests have no token report; token values are unavailable');
    expect(container.textContent).toContain('—');
    expect(container.querySelector('tfoot')?.textContent).not.toContain('100%');
  });

  it('MH-USAGE-023: states every bucket in accessible text, not only in a pointer tooltip', () => {
    draw(report());
    const buckets = screen.getAllByRole('button', { name: /Usage bucket/ });

    expect(buckets.map((item) => item.getAttribute('aria-label'))).toEqual([
      'Usage bucket Sep 24, 2026, 00:00 UTC+08:00 – Sep 24, 2026, 01:00 UTC+08:00',
      'Usage bucket Sep 24, 2026, 01:00 UTC+08:00 – Sep 24, 2026, 02:00 UTC+08:00',
      'Usage bucket Sep 24, 2026, 02:00 UTC+08:00 – Sep 24, 2026, 03:00 UTC+08:00',
    ]);
  });

  it('MH-USAGE-024: every figure the report states names the row and the column it answers', () => {
    draw(report());
    const table = screen.getByRole('table');
    const headers = within(table).getAllByRole('columnheader');
    const rows = within(table).getAllByRole('row').filter((item) => within(item).queryAllByRole('rowheader').length > 0);

    expect(headers.length).toBeGreaterThan(1);
    expect(rows.length).toBeGreaterThan(0);
    for (const rowElement of rows) {
      expect(within(rowElement).getAllByRole('rowheader')).toHaveLength(1);
      expect(within(rowElement).getAllByRole('cell')).toHaveLength(headers.length - 1);
    }
  });

  it('MH-USAGE-025: reads a window whose reports all came back zero as measured, not as missing', () => {
    const measuredZero = counters({
      requests: 4,
      token_reports: 4,
      input_tokens: 0,
      cached_input_tokens: 0,
      output_tokens: 0,
    });
    const { container } = draw(report({
      totals: measuredZero,
      sources: [],
      buckets: [bucket('00', [row(measuredZero)])],
    }));

    expect(container.textContent).not.toContain('Some requests have no token report');
    const card = container.querySelector('.model-hub-usage-stat-label')?.parentElement;
    expect(card?.textContent).toContain('0');
  });

  it('does not claim a 100% share when the selected total is zero', () => {
    const measuredZero = counters({
      requests: 4,
      token_reports: 4,
      input_tokens: 0,
      cached_input_tokens: 0,
      output_tokens: 0,
    });
    const { container } = draw(report({
      totals: measuredZero,
      sources: [],
      buckets: [bucket('00', [row(measuredZero)])],
    }));

    expect(container.querySelector('tfoot')?.textContent).not.toContain('100%');
  });

  it('MH-USAGE-026: every token figure on screen states its own coverage', () => {
    const unreported = counters({
      requests: 2,
      token_reports: 0,
      input_tokens: 0,
      cached_input_tokens: 0,
      output_tokens: 0,
    });
    const { container } = draw(report({
      totals: unreported,
      sources: [],
      buckets: [bucket('00', [row(unreported)])],
    }));

    expect(container.textContent).toContain('Some requests have no token report; token values are unavailable');
    expect(container.querySelector('.model-hub-usage-table-scroll')?.textContent).toContain('—');
  });

  it('MH-USAGE-027: keeps the usage surface within a narrow semantic layout', () => {
    const { container } = draw(report());
    const chart = container.querySelector('.model-hub-usage-chart-wrap');
    const tableScroll = container.querySelector('.model-hub-usage-table-scroll');

    expect(chart).not.toBeNull();
    expect(tableScroll).not.toBeNull();
    expect(screen.getAllByRole('button', { name: /Usage bucket/ })).toHaveLength(3);
  });

  it('renders exact accounting and preserves source/model pair rows', () => {
    const second = row({ source_id: 'source-b', model_id: 'model-a' });
    const value = report({
      totals: counters({ requests: 4, token_reports: 4, input_tokens: 200, cached_input_tokens: 50, output_tokens: 80 }),
      sources: [
        {
          source_id: 'source-a',
          label: 'Same source label',
          last_metered_at: null,
          ...counters(),
          models: [{ model_id: 'model-a', label: 'Same model label', ...counters() }],
        },
        {
          source_id: 'source-b',
          label: 'Same source label',
          last_metered_at: null,
          ...counters(),
          models: [{ model_id: 'model-a', label: 'Same model label', ...counters() }],
        },
      ],
      buckets: [bucket('00', [row(), second]), bucket('01', []), bucket('02', [])],
    });
    draw(value);

    expect(screen.getByText('Same source label · Same model label · source-a')).toBeTruthy();
    expect(screen.getByText('Same source label · Same model label · source-b')).toBeTruthy();
    expect(screen.getByText('All tokens = input + output. Cache reads are included in input and shown separately.')).toBeTruthy();
    expect(screen.getAllByText('50.0%').length).toBeGreaterThan(0);
  });

  it('keeps incomplete empty buckets out of the ordinary empty state and shows gaps', () => {
    const incomplete = report({
      totals: counters({ requests: 0, token_reports: 0, input_tokens: 0, cached_input_tokens: 0, output_tokens: 0 }),
      sources: [],
      buckets: [
        bucket('00', [], { history_complete: false }),
        bucket('01', [], { history_complete: false }),
        bucket('02', [], { history_complete: false }),
      ],
    });
    const { container } = draw(incomplete);

    expect(screen.getAllByText('Some buckets have incomplete history').length).toBeGreaterThan(0);
    expect(screen.queryByText('No usage in this window')).toBeNull();
    expect(container.querySelectorAll('.model-hub-usage-unknown-point')).toHaveLength(9);
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  it('keeps ordinary detail dismissible and pins directly from hover', async () => {
    const { container } = draw(report());
    const buckets = screen.getAllByRole('button', { name: /Usage bucket/ });
    fireEvent.pointerEnter(buckets[0]);
    expect(screen.getByRole('dialog', { name: 'Usage bucket details' })).toBeTruthy();

    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('dialog', { name: 'Usage bucket details' })).toBeNull();

    fireEvent.pointerEnter(buckets[1]);
    const pin = screen.getByRole('button', { name: 'Pin this bucket' });
    await userEvent.click(pin);
    expect(screen.getByRole('dialog', { name: 'Usage bucket details' }).getAttribute('data-pinned')).toBe('true');
    fireEvent.pointerDown(document.body);
    expect(screen.getByRole('dialog', { name: 'Usage bucket details' })).toBeTruthy();
    expect(container.querySelector('.model-hub-usage-details-header p')?.textContent).toContain('Showing Sep 24, 2026');

    await userEvent.click(screen.getByRole('button', { name: 'Unpin this bucket' }));
    expect(screen.getByRole('dialog', { name: 'Usage bucket details' }).getAttribute('data-pinned')).toBe('false');
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('dialog', { name: 'Usage bucket details' })).toBeNull();
  });

  it('moves keyboard focus into detail and restores the bucket without reopening on Escape', async () => {
    const rendered = draw(report());
    const bucketButton = screen.getAllByRole('button', { name: /Usage bucket/ })[0]!;
    const user = userEvent.setup();

    for (let step = 0; step < 32 && document.activeElement !== bucketButton; step += 1) {
      await user.tab();
    }
    expect(document.activeElement).toBe(bucketButton);

    await user.keyboard('{Enter}');
    const pin = screen.getByRole('button', { name: 'Pin this bucket' });
    expect(document.activeElement).toBe(pin);

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'Usage bucket details' })).toBeNull();
    expect(document.activeElement).toBe(bucketButton);

    await user.keyboard(' ');
    const repinned = screen.getByRole('button', { name: 'Pin this bucket' });
    expect(document.activeElement).toBe(repinned);

    await user.keyboard('{Enter}');
    expect(screen.getByRole('dialog', { name: 'Usage bucket details' }).getAttribute('data-pinned')).toBe('true');

    rendered.rerender(
      <I18nextProvider i18n={i18n}>
        <UsageTab
          usage={readyRegion(report({ buckets: [bucket('00', [row({ requests: 3 })]), bucket('01'), bucket('02')] }))}
          windowKey="24h"
          onWindowChange={vi.fn()}
        />
      </I18nextProvider>,
    );

    expect(screen.getByRole('dialog', { name: 'Usage bucket details' }).getAttribute('data-pinned')).toBe('true');
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Unpin this bucket' }));

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'Usage bucket details' })).toBeNull();
    expect(document.activeElement).toBe(bucketButton);

    // A non-modal detail must not reclaim focus after the user tabs elsewhere.
    await user.keyboard('{Enter}');
    await user.tab();
    const tableGrouping = screen.getByRole('button', { name: 'By model' });
    expect(document.activeElement).toBe(tableGrouping);
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'Usage bucket details' })).toBeNull();
    expect(document.activeElement).toBe(tableGrouping);
  });

  it('dismisses pinned details on the first Escape', async () => {
    draw(report());
    fireEvent.pointerEnter(screen.getAllByRole('button', { name: /Usage bucket/ })[0]!);
    await userEvent.click(screen.getByRole('button', { name: 'Pin this bucket' }));
    expect(screen.getByRole('dialog', { name: 'Usage bucket details' })).toBeTruthy();

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(screen.queryByRole('dialog', { name: 'Usage bucket details' })).toBeNull();
  });

  it('clears a pinned bucket that disappears from a same-window refresh', async () => {
    const value = report();
    const rendered = draw(value);
    fireEvent.pointerEnter(screen.getAllByRole('button', { name: /Usage bucket/ })[1]!);
    await userEvent.click(screen.getByRole('button', { name: 'Pin this bucket' }));
    expect(screen.getByRole('dialog', { name: 'Usage bucket details' }).getAttribute('data-pinned')).toBe('true');

    rendered.rerender(
      <I18nextProvider i18n={i18n}>
        <UsageTab usage={readyRegion(report({ buckets: [bucket('00')] }))} windowKey="24h" onWindowChange={vi.fn()} />
      </I18nextProvider>,
    );

    expect(screen.queryByRole('dialog', { name: 'Usage bucket details' })).toBeNull();
    expect(rendered.container.querySelector('.model-hub-usage-details-header p')?.textContent).toContain('same filters');
  });

  it('shows both offsets in the report heading across a DST change', () => {
    const { container } = draw(report({
      from_at: '2026-11-01T01:00:00-04:00',
      to_at: '2026-11-01T01:00:00-05:00',
    }));

    expect(container.textContent).toContain('UTC-04:00');
    expect(container.textContent).toContain('UTC-05:00');
  });

  it('includes both bucket offsets in DST-adjacent detail text', () => {
    const dst = report({
      buckets: [bucket('00', [row()], {
        start_at: '2026-11-01T01:00:00-04:00',
        end_at: '2026-11-01T01:00:00-05:00',
      })],
    });
    draw(dst);
    fireEvent.pointerEnter(screen.getByRole('button', { name: /Usage bucket/ }));
    expect(screen.getByRole('dialog').textContent).toContain('UTC-04:00');
    expect(screen.getByRole('dialog').textContent).toContain('UTC-05:00');
  });

  it('labels the current 24-hour bucket when it is only partially elapsed', () => {
    const value = report({
      to_at: '2026-09-24T02:15:00+08:00',
      buckets: [
        bucket('00'),
        bucket('01'),
        bucket('02', [row()], { end_at: '2026-09-24T02:15:00+08:00' }),
      ],
    });
    draw(value);

    fireEvent.pointerEnter(screen.getAllByRole('button', { name: /Usage bucket/ })[2]!);

    expect(screen.getByRole('dialog').textContent).toContain('Current partial hour');
    expect(screen.getByRole('dialog').textContent).toContain('History complete');
  });

  it('neutralizes formula-like supplier/model labels without quoting numeric counters', () => {
    expect(csvCell('=supplier')).toBe("'=supplier");
    expect(csvCell('@model')).toBe("'@model");
    expect(csvCell('\tmodel')).toBe("'\tmodel");
    expect(csvCell(120)).toBe('120');
  });

  it('exports pinned raw rows with identity and interval metadata', () => {
    const value = report({
      sources: [{
        source_id: 'source-formula',
        label: '=supplier',
        last_metered_at: null,
        ...counters({ input_tokens: 148230, cached_input_tokens: 96010, output_tokens: 4120 }),
        models: [{ model_id: 'model-formula', label: '@model', ...counters() }],
      }],
      buckets: [
        bucket('00', [row({
          source_id: 'source-formula',
          model_id: 'model-formula',
          requests: 4,
          token_reports: 1,
          input_tokens: 148230,
          cached_input_tokens: 96010,
          output_tokens: 4120,
        })]),
        bucket('01', [row()]),
      ],
    });
    const headers = {
      bucketKey: 'bucket_key',
      startAt: 'start_at',
      endAt: 'end_at',
      historyComplete: 'history_complete',
      sourceId: 'source_id',
      modelId: 'model_id',
      ledgerKey: 'ledger_key',
      sourceLabel: 'source_label',
      modelLabel: 'model_label',
      requests: 'requests',
      tokenReports: 'token_reports',
      inputTokens: 'input_tokens',
      nonCachedInputTokens: 'non_cached_input_tokens',
      cachedInputTokens: 'cached_input_tokens',
      outputTokens: 'output_tokens',
      totalTokens: 'total_tokens',
    };
    const csv = buildUsageCsv(value, { sourceIds: [], modelKeys: [] }, '00', headers, 'Unknown model');

    expect(csv.split('\r\n')).toHaveLength(2);
    expect(csv).toContain('2026-09-24T00:00:00+08:00');
    expect(csv).toContain("source-formula,'@model,model-formula");
    expect(csv).toContain("'=supplier");
    expect(csv).toContain("'@model");
    expect(csv).toContain('4,1,148230');
    expect(csv).toContain('148230');
    expect(csv).not.toContain('148,230');
    expect(csv).toContain('152350');

    const unknownCsv = buildUsageCsv(report({
      buckets: [bucket('00', [row({ token_reports: 0 })])],
    }), { sourceIds: [], modelKeys: [] }, '00', headers, 'Unknown model');
    expect(unknownCsv.split('\r\n')[0]).toContain('token_reports');
    expect(unknownCsv.split('\r\n')[1]?.split(',').slice(-7)).toEqual(['2', '0', '', '', '', '', '']);

    const idleCsv = buildUsageCsv(report({
      buckets: [bucket('00', [])],
    }), { sourceIds: [], modelKeys: [] }, '00', headers, 'Unknown model');
    expect(idleCsv.split('\r\n')[1]?.split(',').slice(-7)).toEqual(['0', '0', '0', '0', '0', '0', '0']);
  });

  it('does not export a folded ledger key as the canonical model ID', () => {
    const foldedKey = `model-head-${'x'.repeat(180)}`;
    const csv = buildUsageCsv(report({
      sources: [{
        source_id: 'source-removed',
        label: null,
        last_metered_at: null,
        ...counters(),
        models: [],
      }],
      buckets: [bucket('00', [row({
        source_id: 'source-removed',
        model_id: foldedKey,
      })])],
    }), { sourceIds: [], modelKeys: [] }, '00', {
      bucketKey: 'bucket_key',
      startAt: 'start_at',
      endAt: 'end_at',
      historyComplete: 'history_complete',
      sourceId: 'source_id',
      modelId: 'model_id',
      ledgerKey: 'ledger_key',
      sourceLabel: 'source_label',
      modelLabel: 'model_label',
      requests: 'requests',
      tokenReports: 'token_reports',
      inputTokens: 'input_tokens',
      nonCachedInputTokens: 'non_cached_input_tokens',
      cachedInputTokens: 'cached_input_tokens',
      outputTokens: 'output_tokens',
      totalTokens: 'total_tokens',
    }, 'Unknown model');

    const [header, line] = csv.split('\r\n');
    expect(header).toContain('model_id,ledger_key');
    expect(line?.split(',').slice(5, 7)).toEqual(['', foldedKey]);
  });
});
