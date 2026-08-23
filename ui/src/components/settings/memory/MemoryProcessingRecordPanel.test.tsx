/* @vitest-environment jsdom */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { MemoryProcessingRecordPanel } from './MemoryProcessingRecordPanel';

const api = vi.hoisted(() => ({
  getMemoryProcessingRecordEntries: vi.fn(),
  getMemoryProcessingRecordEntry: vi.fn(),
}));

vi.mock('../../../context/ApiContext', () => ({
  useApi: () => api,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => {
      if (key === 'memory.processingRecord.reason.native_runs_unavailable') return 'Native runs cannot be read';
      if (key === 'memory.processingRecord.records.sourceNotice') {
        return `${options?.section}: ${options?.state} (${options?.reason})`;
      }
      if (key === 'memory.processingRecord.runStatus.failed') return 'Failed';
      if (key === 'memory.kind.episode') return 'Episode';
      return key;
    },
  }),
}));

describe('MemoryProcessingRecordPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getMemoryProcessingRecordEntries.mockResolvedValue({
      status: 'ok',
      entries: [{
        memcell_id: 'mc-1',
        project_id: 'default',
        session_id: 'session-1',
        owner_id: 'owner-1',
        timestamp_ms: 1_700_000_000_000,
        preview: 'Authorized payload',
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
    api.getMemoryProcessingRecordEntry.mockResolvedValue({
      status: 'ok',
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
            { type: 'text', text: 'First boundary', omitted_bytes: 0 },
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
        indexing: { status: 'available', items: [] },
      },
    });
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
  });
});
