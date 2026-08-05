/* @vitest-environment jsdom */

import { useState } from 'react';
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
  MEMORY_LOG_ENTRY_LIMIT,
  mergeMemoryLogEntries,
  memoryLogEnumLabel,
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
  project_id: 'p-11111111111111111111111111111111',
  principal_id: 'u-11111111111111111111111111111111',
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

const detailResult = (
  providerCallStatus = 'ok',
  strategyStatus = 'success',
  indexingError: string | null = null,
): MemoryLogDetailResult => ({
  status: 'ok',
  entry: entry('mc-alpha', 'Alpha detail'),
  capture: { status: 'available', delivery_states: ['delivered'], matched_message_count: 1 },
  steps: [
    { type: 'capture', status: 'delivered' },
    { type: 'memcell', status: 'created', timestamp_ms: 1_722_816_000_000, memcell_id: 'mc-alpha' },
    {
      type: 'strategy',
      status: strategyStatus,
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
      status: providerCallStatus,
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
    indexing: { status: indexingError ? 'failed' : 'indexed', updated_at_ms: 1_722_816_002_000, error: indexingError },
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
      status={null}
      onClearAll={() => undefined}
      {...props}
    />,
  );

describe('MemoryLogPanel', () => {
  it.each([
    ['dead_letter', 'deadLetter'],
    ['crashed', 'crashed'],
  ])('renders terminal EverOS run status %s as a localized failure', async (status, label) => {
    api.getMemoryLog.mockResolvedValue(listResult([entry('mc-alpha', 'Alpha')]));
    api.getMemoryLogEntry.mockResolvedValue(detailResult('ok', status));
    const user = userEvent.setup();

    renderPanel();
    await user.click(await screen.findByText('Alpha'));

    const badge = await screen.findByText(`memory.log.status.${label}`);
    expect(badge.classList.contains('text-destructive')).toBe(true);
  });

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
    expect(screen.getByText(/memory\.log\.projectId/)).toBeTruthy();
    expect(screen.getByText('p-11111111111111111111111111111111')).toBeTruthy();
    expect(screen.getByText(/memory\.log\.userId/)).toBeTruthy();
    expect(screen.getByText('u-11111111111111111111111111111111')).toBeTruthy();
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

  it('renders the scrubbed current indexing error with the indexing status', async () => {
    api.getMemoryLog.mockResolvedValue(listResult([entry('mc-alpha', 'Alpha')]));
    api.getMemoryLogEntry.mockResolvedValue(detailResult('ok', 'success', 'indexing failed: provider unavailable'));
    const user = userEvent.setup();

    renderPanel();
    await user.click(await screen.findByText('Alpha'));

    expect(await screen.findByText(/memory\.log\.currentIndexing: memory\.log\.status\.failed/)).toBeTruthy();
    expect(screen.getByText('indexing failed: provider unavailable')).toBeTruthy();
  });

  it.each(['ok', 'success'])('renders provider call status %s as successful', async (providerCallStatus) => {
    api.getMemoryLog.mockResolvedValue(listResult([entry('mc-alpha', 'Alpha')]));
    api.getMemoryLogEntry.mockResolvedValue(detailResult(providerCallStatus));
    const user = userEvent.setup();

    renderPanel();
    await user.click(await screen.findByText('Alpha'));

    expect((await screen.findByText('memory.log.callSummary')).classList.contains('bg-mint-soft')).toBe(true);
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

  it('leaves transient recorder recovery to the page-level restart action', async () => {
    api.getMemoryLog.mockResolvedValue(listResult([]));
    renderPanel({ status: status('writer_failures') });

    expect(await screen.findByText('memory.log.recorderDegraded')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'memory.log.restartAction' })).toBeNull();
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

  it('drops list and expanded payload state when Clear remounts the log', async () => {
    api.getMemoryLog
      .mockResolvedValueOnce(listResult([entry('mc-alpha', 'Alpha')]))
      .mockResolvedValue(listResult([]));
    api.getMemoryLogEntry.mockResolvedValue(detailResult());
    const user = userEvent.setup();

    const Harness = () => {
      const [generation, setGeneration] = useState(0);
      return (
        <>
          <button type="button" onClick={() => setGeneration((value) => value + 1)}>clear-finished</button>
          <MemoryLogPanel
            key={generation}
            enabled
            status={null}
            onClearAll={() => undefined}
          />
        </>
      );
    };

    render(<Harness />);
    await user.click(await screen.findByText('Alpha'));
    await user.click(screen.getByRole('button', { expanded: false }));
    expect(await screen.findByText(/hello/)).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'clear-finished' }));
    await waitFor(() => expect(screen.queryByText(/hello/)).toBeNull());
    expect(screen.queryByText('Alpha')).toBeNull();
    await waitFor(() => expect(api.getMemoryLog).toHaveBeenCalledTimes(2));
  });
});

describe('Memory Log bounded helpers and static states', () => {
  it('deduplicates cursor accumulation without reordering accepted entries', () => {
    expect(mergeMemoryLogEntries([entry('a'), entry('b')], [entry('b'), entry('c')], false).map((x) => x.memcell_id))
      .toEqual(['a', 'b', 'c']);
  });

  it('caps cursor accumulation at the fixed render limit', () => {
    const current = Array.from({ length: MEMORY_LOG_ENTRY_LIMIT - 5 }, (_, index) => entry(`old-${index}`));
    const incoming = Array.from({ length: 20 }, (_, index) => entry(`new-${index}`));
    const merged = mergeMemoryLogEntries(current, incoming, false);

    expect(merged).toHaveLength(MEMORY_LOG_ENTRY_LIMIT);
    expect(merged.at(-1)?.memcell_id).toBe('new-4');
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

  it('renders partial sections distinctly and stops pagination at the visible limit', () => {
    render(
      <MemoryLogListContent
        entries={[entry('a')]}
        sections={{ ...sections, everos: { status: 'partial', reason: 'runs_missing' } }}
        loading={false}
        loaded
        error={null}
        forbidden={false}
        nextCursor="next"
        limitReached
        onOpen={() => undefined}
        onRefresh={() => undefined}
        onLoadMore={() => undefined}
      />,
    );

    expect(screen.getByText('memory.log.sectionPartial')).toBeTruthy();
    expect(screen.getByText('memory.log.limitReached:200')).toBeTruthy();
    expect(screen.getAllByRole('status')).toHaveLength(2);
    expect(screen.queryByRole('button', { name: 'memory.log.loadMore' })).toBeNull();
  });

  it('localizes known enums and leaves future values as inert fallback text', () => {
    expect(memoryLogEnumLabel(translate as never, 'reason', 'runs_missing'))
      .toBe('memory.log.reason.runsMissing');
    expect(memoryLogEnumLabel(translate as never, 'status', 'ok')).toBe('memory.log.status.ok');
    expect(memoryLogEnumLabel(translate as never, 'status', 'future_status')).toBe('future_status');
  });
});
