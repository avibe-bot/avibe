/* @vitest-environment jsdom */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderToStaticMarkup } from 'react-dom/server';

import type {
  MemoryLogDetailResult,
  MemoryLogEntry,
  MemoryLogListResult,
  MemoryStatus,
} from '../../../context/ApiContext';
import {
  MemoryLogListContent,
  MemoryLogPanel,
  mergeMemoryLogEntries,
  prepareJsonPreview,
} from './MemoryLogPanel';

const api = vi.hoisted(() => ({
  getMemoryLog: vi.fn(),
  getMemoryLogEntry: vi.fn(),
}));
const translate = vi.hoisted(() => (key: string, values?: { count?: number }) =>
  values?.count === undefined ? key : `${key}:${values.count}`,
);

vi.mock('../../../context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../../../context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: translate }),
}));

vi.mock('../../ui/preview-json', () => ({
  default: ({ value }: { value: object }) => <pre data-testid="json-tree">{JSON.stringify(value)}</pre>,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const entry = (memcellId: string, preview = memcellId): MemoryLogEntry => ({
  memcell_id: memcellId,
  timestamp_ms: 1_722_816_000_000,
  preview,
  message_count: 1,
  run_summary: { total: 2, statuses: { success: 2 } },
  authorized_call_count: 1,
});

const sections = {
  everos: { status: 'available' as const },
  capture: { status: 'available' as const },
  calls: { status: 'available' as const },
};

const listResult = (entries: MemoryLogEntry[], nextCursor: string | null = null): MemoryLogListResult => ({
  status: 'ok',
  entries,
  next_cursor: nextCursor,
  sections,
});

const detailResult = (): MemoryLogDetailResult => ({
  status: 'ok',
  entry: entry('mc-alpha', 'Alpha detail'),
  capture: { status: 'available', delivery_states: ['delivered'], matched_message_count: 1 },
  steps: [
    { type: 'capture', status: 'delivered' },
    { type: 'memcell', status: 'created', timestamp_ms: 1_722_816_000_000, memcell_id: 'mc-alpha' },
    {
      type: 'strategy',
      status: 'success',
      started_at_ms: 1_722_816_001_000,
      strategy: 'extract_user_profile',
      relation: 'profile_trigger',
      run_id: 'run-1',
    },
  ],
  calls: [
    {
      id: 'call-1',
      started_at_ms: 1_722_816_001_500,
      duration_ms: 42,
      kind: 'llm',
      stage: 'strategy',
      model: 'model-1',
      status: 'success',
      error: null,
      finish_reason: 'stop',
      prompt_tokens: 4,
      completion_tokens: 2,
      request: { prompt: 'hello' },
      response: { answer: 'world' },
      request_bytes: 18,
      response_bytes: 18,
      dropped_before: 3,
    },
  ],
  omitted_call_count: 2,
  omitted_step_count: 4,
  current_state: {
    status: 'available',
    profile: { status: 'present', updated_at_ms: 1_722_816_002_000 },
    indexing: { status: 'indexed', updated_at_ms: 1_722_816_002_000 },
    label: 'current_state',
  },
  sections,
});

const status = (reason: string): MemoryStatus => ({
  status: 'ok',
  state: 'degraded',
  buckets: { syncing: 0, succeeded: 0, unknown: 0, failed: 0, dead: 0, missed: 0 },
  pending: 0,
  processing: 0,
  awaiting_receipt: 0,
  succeeded: 0,
  receipt_unknown: 0,
  distill_failed: 0,
  dead: 0,
  missed: 0,
  queue_plaintext_bytes: 0,
  provider_disk_bytes: 0,
  last_success_at: null,
  last_flush_observation: null,
  last_flush_status: null,
  last_flush_error_code: null,
  last_flush_request_id: null,
  last_flush_at: null,
  processing_fault_kind: null,
  processing_fault_since: null,
  processing_alert_active: false,
  recorder: { state: 'degraded', reason },
  error: null,
  data_exists: true,
});

const renderPanel = (props?: Partial<React.ComponentProps<typeof MemoryLogPanel>>) =>
  render(
    <MemoryLogPanel
      enabled
      loggingEnabled
      status={null}
      onRestartRuntime={() => undefined}
      restarting={false}
      onClearAll={() => undefined}
      {...props}
    />,
  );

describe('MemoryLogPanel', () => {
  it('accumulates cursor pages and refresh replaces the old result', async () => {
    let firstPageReads = 0;
    api.getMemoryLog.mockImplementation((cursor: string | null) => {
      if (cursor === 'cursor-2') return Promise.resolve(listResult([entry('mc-beta', 'Beta')]));
      firstPageReads += 1;
      return Promise.resolve(
        firstPageReads === 1
          ? listResult([entry('mc-alpha', 'Alpha')], 'cursor-2')
          : listResult([entry('mc-fresh', 'Fresh')]),
      );
    });
    api.getMemoryLogEntry.mockResolvedValue(detailResult());
    const user = userEvent.setup();

    renderPanel();
    expect(await screen.findByText('Alpha')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'memory.log.loadMore' }));
    expect(await screen.findByText('Beta')).toBeTruthy();
    expect(api.getMemoryLog).toHaveBeenCalledWith('cursor-2', 20);

    await user.click(screen.getByRole('button', { name: 'memory.log.refresh' }));
    expect(await screen.findByText('Fresh')).toBeTruthy();
    await waitFor(() => expect(screen.queryByText('Alpha')).toBeNull());
  });

  it('opens detail in-tab, expands inert payloads, and returns to the list', async () => {
    api.getMemoryLog.mockResolvedValue(listResult([entry('mc-alpha', 'Alpha')]));
    api.getMemoryLogEntry.mockResolvedValue(detailResult());
    const user = userEvent.setup();

    renderPanel();
    await user.click(await screen.findByText('Alpha'));
    expect(await screen.findByText('memory.log.timeline')).toBeTruthy();
    expect(screen.getByText('memory.log.omittedSteps:4')).toBeTruthy();
    expect(screen.getByText('memory.log.omittedCalls:2')).toBeTruthy();
    expect(screen.getByText('memory.log.droppedCalls:3')).toBeTruthy();

    const callToggle = screen.getByRole('button', { expanded: false });
    await user.click(callToggle);
    expect(await screen.findByText(/hello/)).toBeTruthy();
    expect(screen.getByText(/world/)).toBeTruthy();
    await user.click(callToggle);
    await waitFor(() => expect(screen.queryByText(/hello/)).toBeNull());

    await user.click(screen.getByRole('button', { name: 'memory.log.back' }));
    expect(await screen.findByText('Alpha')).toBeTruthy();
  });

  it('ignores a slow refresh once a newer refresh has completed', async () => {
    let resolveSlow: ((value: MemoryLogListResult) => void) | undefined;
    let readCount = 0;
    api.getMemoryLog.mockImplementation(() => {
      readCount += 1;
      if (readCount === 1) return Promise.resolve(listResult([entry('mc-initial', 'Initial')]));
      if (readCount === 2) {
        return new Promise<MemoryLogListResult>((resolve) => {
          resolveSlow = resolve;
        });
      }
      return Promise.resolve(listResult([entry('mc-fast', 'Fast')]));
    });
    api.getMemoryLogEntry.mockResolvedValue(detailResult());
    const user = userEvent.setup();

    renderPanel();
    expect(await screen.findByText('Initial')).toBeTruthy();
    const refresh = screen.getByRole('button', { name: 'memory.log.refresh' });
    await user.click(refresh);
    await user.click(refresh);
    expect(await screen.findByText('Fast')).toBeTruthy();
    resolveSlow?.(listResult([entry('mc-slow', 'Slow')]));
    await Promise.resolve();
    expect(screen.queryByText('Slow')).toBeNull();
    expect(screen.getByText('Fast')).toBeTruthy();
  });

  it('reuses runtime restart for transient recorder degradation', async () => {
    api.getMemoryLog.mockResolvedValue(listResult([]));
    const restart = vi.fn();
    const user = userEvent.setup();
    renderPanel({ status: status('writer_failures'), onRestartRuntime: restart });

    await user.click(await screen.findByRole('button', { name: 'memory.log.restartAction' }));
    expect(restart).toHaveBeenCalledTimes(1);
  });

  it('keeps the normal timeline visible when provider payload logging is off', async () => {
    api.getMemoryLog.mockResolvedValue(listResult([entry('mc-alpha', 'Alpha')]));
    renderPanel({ loggingEnabled: false });

    expect(await screen.findByText('Alpha')).toBeTruthy();
    expect(screen.getByText('memory.log.loggingOff')).toBeTruthy();
  });

  it('guides corrupt logs to the existing Clear confirmation', async () => {
    api.getMemoryLog.mockResolvedValue(listResult([]));
    const clear = vi.fn();
    const user = userEvent.setup();
    renderPanel({ status: status('call_log_corrupt'), onClearAll: clear });

    await user.click(await screen.findByRole('button', { name: 'memory.log.clearAction' }));
    expect(clear).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'memory.log.restartAction' })).toBeNull();
  });
});

describe('Memory Log bounded helpers and static states', () => {
  it('deduplicates cursor accumulation without reordering accepted entries', () => {
    expect(mergeMemoryLogEntries([entry('a'), entry('b')], [entry('b'), entry('c')], false).map((x) => x.memcell_id))
      .toEqual(['a', 'b', 'c']);
  });

  it('falls back to inert text for invalid, oversized, or node-heavy JSON', () => {
    expect(prepareJsonPreview('{invalid').mode).toBe('text');
    expect(prepareJsonPreview('x'.repeat(256 * 1024 + 1)).mode).toBe('text');
    expect(prepareJsonPreview(Array.from({ length: 4001 }, (_, index) => index)).mode).toBe('text');
    expect(prepareJsonPreview({ safe: ['small'] }).mode).toBe('tree');
  });

  it('keeps loading, empty, failure, and forbidden static render states explicit', () => {
    const base = {
      entries: [],
      sections: null,
      loading: false,
      loaded: true,
      error: null,
      forbidden: false,
      nextCursor: null,
      onOpen: () => undefined,
      onRefresh: () => undefined,
      onLoadMore: () => undefined,
    };
    expect(renderToStaticMarkup(<MemoryLogListContent {...base} loaded={false} />)).toContain('memory.log.loading');
    expect(renderToStaticMarkup(<MemoryLogListContent {...base} />)).toContain('memory.log.empty');
    expect(renderToStaticMarkup(<MemoryLogListContent {...base} error="failed" />)).toContain('failed');
    expect(renderToStaticMarkup(<MemoryLogListContent {...base} forbidden />)).toContain('memory.log.forbidden');
  });

  it('keeps the five-tab row in a page-local horizontal overflow container', () => {
    const source = readFileSync(
      join(dirname(fileURLToPath(import.meta.url)), '..', 'SettingsMemoryPage.tsx'),
      'utf8',
    );
    expect(source).toMatch(/data-testid="memory-tabs-scroll" className="max-w-full overflow-x-auto pb-1"/);
    expect(source).toContain('<div className="min-w-max">');
    expect(source).toContain("{ id: 'log' as const");
  });
});
