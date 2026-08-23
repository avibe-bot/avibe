/* @vitest-environment jsdom */

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MemoryProcessingRecordPanel } from './MemoryProcessingRecordPanel';

const api = vi.hoisted(() => ({
  getMemoryProcessingRecordEntries: vi.fn(),
  getMemoryProcessingRecordEntry: vi.fn(),
}));

vi.mock('../../../context/ApiContext', () => ({
  useApi: () => api,
}));

vi.mock('react-i18next', () => {
  const t = (key: string, options?: Record<string, unknown>) => {
    if (key === 'memory.processingRecord.reason.native_runs_unavailable') return 'Native runs cannot be read';
    if (key === 'memory.processingRecord.records.sourceNotice') {
      return `${options?.section}: ${options?.state} (${options?.reason})`;
    }
    if (key === 'memory.processingRecord.runStatus.failed') return 'Failed';
    if (key === 'memory.kind.episode') return 'Episode';
    return key;
  };
  return { useTranslation: () => ({ t }) };
});

const listResult = (memcellId = 'mc-1', preview = 'Authorized payload') => ({
  status: 'ok' as const,
  entries: [{
    memcell_id: memcellId,
    project_id: 'default',
    session_id: 'session-1',
    owner_id: 'owner-1',
    timestamp_ms: 1_700_000_000_000,
    preview,
    payload: { status: 'available', reason: null, item_count: 1 },
    runs: { status: 'unavailable', reason: 'native_runs_unavailable', total: 0, statuses: {} },
  }],
  next_cursor: null,
  sections: {
    memcells: { status: 'available', observed_at: null },
    runs: { status: 'unavailable', observed_at: null, reason: 'native_runs_unavailable' },
    semantic: { status: 'available', observed_at: null },
  },
});

const detailResult = (firstBoundary = 'First boundary') => ({
  status: 'ok' as const,
  entry: {
    memcell_id: 'mc-1',
    project_id: 'default',
    session_id: 'session-1',
    owner_id: 'owner-1',
    timestamp_ms: 1_700_000_000_000,
  },
  payload: {
    status: 'available',
    items: [{
      id: 'message-1',
      timestamp_ms: 1_700_000_000_000,
      sender_id: 'owner-1',
      content: [
        { type: 'text', text: firstBoundary, omitted_bytes: 0 },
        { type: 'text', text: 'Second boundary', omitted_bytes: 0 },
      ],
    }],
  },
  runs: {
    status: 'partial',
    reason: 'native_run_retention_bounded',
    items: [{
      run_id: 'run-1',
      strategy: 'extract_user_memory',
      attempt: 2,
      status: 'failed',
      started_at: '2026-08-24T00:00:00Z',
      finished_at: '2026-08-24T00:00:01Z',
      error: 'Display-safe error',
      event_topic: 'everos.memory.UserPipelineStarted',
    }],
  },
  semantic: {
    status: 'available',
    items: [{ kind: 'episode', entry_id: 'episode-1', timestamp: null, content: 'Linked content' }],
  },
  current_state: {
    status: 'available',
    label: 'current_unattributed',
    profile: { status: 'present', updated_at_ms: 1_700_000_000_000 },
    indexing: {
      status: 'available',
      items: [{
        md_path: 'avibe/default_project/users/owner-1/episodes/episode-1.md',
        status: 'failed',
        updated_at: '2026-08-24T00:00:02Z',
        error: 'Index projection failed',
      }],
    },
  },
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

describe('MemoryProcessingRecordPanel', () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.clearAllMocks();
    api.getMemoryProcessingRecordEntries.mockResolvedValue(listResult());
    api.getMemoryProcessingRecordEntry.mockResolvedValue(detailResult());
  });

  it('shows native section availability and preserves payload boundaries in detail', async () => {
    render(<MemoryProcessingRecordPanel />);

    expect(await screen.findByText('Authorized payload')).toBeTruthy();
    expect(screen.getByText(/Native runs cannot be read/)).toBeTruthy();

    fireEvent.click(screen.getByText('Authorized payload').closest('button')!);

    await waitFor(() => expect(api.getMemoryProcessingRecordEntry).toHaveBeenCalledWith('mc-1'));
    expect(await screen.findByText('First boundary')).toBeTruthy();
    expect(screen.getByText('Second boundary')).toBeTruthy();
    expect(screen.getByText('Display-safe error')).toBeTruthy();
    expect(screen.getByText('Episode')).toBeTruthy();
    expect(screen.getByText('avibe/default_project/users/owner-1/episodes/episode-1.md')).toBeTruthy();
    expect(screen.getByText('Index projection failed')).toBeTruthy();
  });

  it('ignores a list response superseded by a refresh', async () => {
    const initial = deferred<ReturnType<typeof listResult>>();
    const refreshed = deferred<ReturnType<typeof listResult>>();
    api.getMemoryProcessingRecordEntries
      .mockReset()
      .mockImplementationOnce(() => initial.promise)
      .mockImplementationOnce(() => refreshed.promise);

    const view = render(<MemoryProcessingRecordPanel refreshToken={0} />);
    await waitFor(() => expect(api.getMemoryProcessingRecordEntries).toHaveBeenCalledTimes(1));
    view.rerender(<MemoryProcessingRecordPanel refreshToken={1} />);
    await waitFor(() => expect(api.getMemoryProcessingRecordEntries).toHaveBeenCalledTimes(2));

    await act(async () => { refreshed.resolve(listResult('mc-fresh', 'Fresh payload')); });
    expect(await screen.findByText('Fresh payload')).toBeTruthy();
    await act(async () => { initial.resolve(listResult('mc-stale', 'Stale payload')); });

    await waitFor(() => expect(screen.queryByText('Stale payload')).toBeNull());
    expect(screen.getByText('Fresh payload')).toBeTruthy();
  });

  it('refreshes the selected record detail', async () => {
    api.getMemoryProcessingRecordEntry
      .mockReset()
      .mockResolvedValueOnce(detailResult('Before refresh'))
      .mockResolvedValueOnce(detailResult('After refresh'));

    const view = render(<MemoryProcessingRecordPanel refreshToken={0} />);
    fireEvent.click((await screen.findByText('Authorized payload')).closest('button')!);
    expect(await screen.findByText('Before refresh')).toBeTruthy();

    view.rerender(<MemoryProcessingRecordPanel refreshToken={1} />);

    expect(await screen.findByText('After refresh')).toBeTruthy();
    expect(api.getMemoryProcessingRecordEntry).toHaveBeenCalledTimes(2);
  });
});
