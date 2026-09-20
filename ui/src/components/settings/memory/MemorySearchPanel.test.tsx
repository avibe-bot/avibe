/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { MemoryListItem, MemoryListWarning } from '../../../context/ApiContext';
import { MemorySearchPanel } from './MemorySearchPanel';

const apiHarness = vi.hoisted(() => {
  const initial = {
    listMemoryEpisodes: vi.fn(),
    listMemoryProjects: vi.fn(),
    searchMemory: vi.fn(),
  };
  return { current: initial, initial };
});
const api = apiHarness.initial;
const copyTextToClipboard = vi.hoisted(() => vi.fn());
const translate = vi.hoisted(() => (
  key: string,
  options?: Record<string, string | number>,
) => {
  if (options?.page != null && options?.total != null) {
    return `${key}:${options.page}/${options.total}`;
  }
  if (options?.page != null) return `${key}:${options.page}`;
  if (options?.count != null) return `${key}:${options.count}`;
  if (options?.title != null) return `${key}:${options.title}`;
  return key;
});

vi.mock('../../../context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../../../context/ApiContext')>();
  return { ...original, useApi: () => apiHarness.current };
});

vi.mock('../../../lib/utils', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../../../lib/utils')>();
  return { ...original, copyTextToClipboard };
});

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: translate }),
}));

const episode = (overrides: Partial<MemoryListItem> = {}): MemoryListItem => ({
  id: 'entry-001',
  kind: 'episode',
  subject: 'Release planning',
  summary: 'Compared the rollout options.',
  body: 'Compared the rollout options and selected the staged release.',
  timestamp: '2026-08-14T09:30:00Z',
  project: 'default',
  ...overrides,
});

const listResult = (
  items: MemoryListItem[],
  overrides: Partial<{
    total_count: number | null;
    next_cursor: string | null;
    warnings: MemoryListWarning[];
  }> = {},
) => ({
  status: 'ok' as const,
  items,
  count: items.length,
  total_count: 0,
  warnings: [],
  page: 1,
  page_size: 20,
  next_cursor: null,
  ...overrides,
});

beforeEach(() => {
  apiHarness.current = api;
  api.listMemoryProjects.mockResolvedValue({
    status: 'ok',
    projects: [
      { id: 'default', kind: 'default' },
      { id: 'all', kind: 'all' },
      { id: 'notes', kind: 'named' },
    ],
  });
  api.listMemoryEpisodes.mockResolvedValue(listResult([]));
  api.searchMemory.mockResolvedValue({ status: 'ok', items: [], warnings: [] });
  copyTextToClipboard.mockResolvedValue(true);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('MemorySearchPanel browse and search modes', () => {
  it('[MEMORY-LIST-006] shows the subject first with summary and body fallbacks', async () => {
    const subjectFirst = episode({
      id: 'subject-first',
      subject: '2026-09-05 release decision',
      summary: '2026-09-05T07:30:00Z Chose the staged rollout.',
      body: 'Full release decision with every rollout consideration preserved.',
    });
    const summaryFallback = episode({
      id: 'summary-fallback',
      subject: '',
      summary: 'Summary fallback title',
      body: 'Summary fallback detail.',
    });
    const bodyFallback = episode({
      id: 'body-fallback',
      subject: '',
      summary: '',
      body: 'Body fallback title and detail.',
    });
    api.listMemoryEpisodes.mockResolvedValue(listResult([
      subjectFirst,
      summaryFallback,
      bodyFallback,
    ]));
    const user = userEvent.setup();

    render(<MemorySearchPanel enabled />);

    expect(await screen.findByText(subjectFirst.subject)).toBeTruthy();
    expect(screen.queryByText(subjectFirst.summary)).toBeNull();
    expect(screen.getByText(summaryFallback.summary)).toBeTruthy();
    expect(screen.getByText(bodyFallback.body)).toBeTruthy();

    const subjectRow = screen.getByRole('button', {
      name: `memory.search.browse.openDetail:${subjectFirst.subject}`,
    });
    const rowTimestamp = subjectRow.querySelector(`time[datetime="${subjectFirst.timestamp}"]`);
    expect(rowTimestamp?.textContent).toMatch(/^\d{4}-\d{2}-\d{2} · \d{2}:\d{2}$/);
    await user.click(subjectRow);

    expect(screen.getByText(subjectFirst.body)).toBeTruthy();
    const detailTimestamp = document.querySelector(
      `[aria-labelledby="memory-episode-detail-title"] time[datetime="${subjectFirst.timestamp}"]`,
    );
    expect(detailTimestamp?.textContent).toMatch(/^\d{4}-\d{2}-\d{2} · \d{2}:\d{2}$/);
  });

  it('[MEMORY-LIST-004][MEMORY-LIST-008] browses a page boundary and copies the selected entry ID', async () => {
    const first = episode();
    const second = episode({ id: 'entry-021', subject: 'Follow-up', summary: '', body: 'Second page body.' });
    api.listMemoryEpisodes.mockImplementation((_project: string, options: { page: number }) =>
      Promise.resolve(options.page === 1
        ? listResult([first], { total_count: 21 })
        : listResult([second], { total_count: 21 })),
    );
    const user = userEvent.setup();

    render(<MemorySearchPanel enabled />);

    expect(await screen.findByText(first.subject)).toBeTruthy();
    expect(screen.getByLabelText('memory.search.browse.sortLabel')).toHaveProperty('value', 'newest');
    expect(screen.getByLabelText('memory.search.browse.sortLabel')).toHaveProperty('disabled', false);
    expect(api.listMemoryEpisodes).toHaveBeenCalledWith('default', {
      page: 1,
      cursor: null,
      limit: 20,
    });
    expect(screen.queryByRole('button', { name: 'memory.search.browse.copyEntryId' })).toBeNull();
    const firstRow = screen.getByRole('button', {
      name: 'memory.search.browse.openDetail:Release planning',
    });
    expect(firstRow.getAttribute('aria-pressed')).toBe('false');
    await user.click(firstRow);
    expect(firstRow.getAttribute('aria-pressed')).toBe('true');
    await user.click(screen.getByRole('button', { name: 'memory.search.browse.copyEntryId' }));
    expect(copyTextToClipboard).toHaveBeenCalledWith(first.id);
    expect(await screen.findByRole('button', { name: 'memory.search.browse.entryIdCopied' })).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'memory.search.browse.next' }));
    expect(await screen.findByText(second.subject)).toBeTruthy();
    expect(screen.queryByText(second.body)).toBeNull();
    await user.click(screen.getByRole('button', {
      name: 'memory.search.browse.openDetail:Follow-up',
    }));
    expect(await screen.findByText(second.body)).toBeTruthy();
    expect(api.listMemoryEpisodes).toHaveBeenLastCalledWith('default', {
      page: 2,
      cursor: null,
      limit: 20,
    });
    expect((screen.getByRole('button', { name: 'memory.search.browse.next' }) as HTMLButtonElement).disabled).toBe(true);

    const callsBeforeReselect = api.listMemoryEpisodes.mock.calls.length;
    await user.click(screen.getByRole('radio', { name: 'memory.origin.user' }));
    expect(api.listMemoryEpisodes).toHaveBeenCalledTimes(callsBeforeReselect);
    expect(screen.getByText('memory.search.browse.pageSummary:2/2')).toBeTruthy();
    expect(screen.getByRole('button', {
      name: 'memory.search.browse.openDetail:Follow-up',
    })).toBeTruthy();
  });

  it('[MEMORY-LIST-003] resumes the all-project aggregate with the opaque cursor', async () => {
    const first = episode({ id: 'all-001', project: 'notes' });
    const second = episode({ id: 'all-002', project: 'default', subject: 'Older aggregate episode.' });
    api.listMemoryEpisodes.mockImplementation((project: string, options: { cursor: string | null }) => {
      if (project !== 'all') return Promise.resolve(listResult([]));
      return Promise.resolve(options.cursor === 'cursor-page-2'
        ? listResult([second], { total_count: 21 })
        : listResult([first], { total_count: 21, next_cursor: 'cursor-page-2' }));
    });
    const user = userEvent.setup();

    render(<MemorySearchPanel enabled />);
    await user.selectOptions(await screen.findByLabelText('memory.search.projectLabel'), 'all');

    expect(await screen.findByText(first.subject)).toBeTruthy();
    expect(api.listMemoryEpisodes).toHaveBeenLastCalledWith('all', {
      page: 1,
      cursor: null,
      limit: 20,
    });
    await user.click(screen.getByRole('button', { name: 'memory.search.browse.next' }));
    expect(await screen.findByText(second.subject)).toBeTruthy();
    expect(api.listMemoryEpisodes).toHaveBeenLastCalledWith('all', {
      page: 2,
      cursor: 'cursor-page-2',
      limit: 20,
    });

    await user.click(screen.getByRole('button', { name: 'memory.search.browse.previous' }));
    await waitFor(() => expect(api.listMemoryEpisodes).toHaveBeenLastCalledWith('all', {
      page: 1,
      cursor: null,
      limit: 20,
    }));
  });

  it('[MEMORY-LIST-004] refetches the displayed page when the API identity changes', async () => {
    const first = episode({ id: 'page-1', subject: 'Original first page.' });
    const second = episode({ id: 'page-2', subject: 'Original second page.' });
    api.listMemoryEpisodes.mockImplementation((_project: string, options: { page: number }) =>
      Promise.resolve(options.page === 1
        ? listResult([first], { total_count: 21 })
        : listResult([second], { total_count: 21 })),
    );
    const user = userEvent.setup();
    const view = render(<MemorySearchPanel enabled />);

    await screen.findByText('Original first page.');
    await user.click(screen.getByRole('button', { name: 'memory.search.browse.next' }));
    await screen.findByText('Original second page.');

    const replacementList = vi.fn().mockResolvedValue(listResult([
      episode({ id: 'replacement-page-2', subject: 'Replacement second page.' }),
    ], { total_count: 21 }));
    apiHarness.current = { ...api, listMemoryEpisodes: replacementList };
    view.rerender(<MemorySearchPanel enabled />);

    expect(await screen.findByText('Replacement second page.')).toBeTruthy();
    expect(replacementList).toHaveBeenCalledWith('default', {
      page: 2,
      cursor: null,
      limit: 20,
    });
    expect(screen.getByText('memory.search.browse.pageSummary:2/2')).toBeTruthy();
  });

  it('[MEMORY-LIST-003] invalidates descendant aggregate cursors when an earlier page changes', async () => {
    let pageOneReads = 0;
    api.listMemoryEpisodes.mockImplementation((project: string, options: { cursor: string | null }) => {
      if (project !== 'all') return Promise.resolve(listResult([]));
      if (options.cursor === 'cursor-page-2') {
        return Promise.resolve(listResult([
          episode({ id: 'page-2', subject: 'Aggregate page two.' }),
        ], { total_count: 60, next_cursor: 'cursor-page-3' }));
      }
      pageOneReads += 1;
      return Promise.resolve(listResult([
        episode({ id: `page-1-${pageOneReads}`, subject: 'Aggregate page one.' }),
      ], {
        total_count: 60,
        next_cursor: pageOneReads === 1 ? 'cursor-page-2' : null,
      }));
    });
    const user = userEvent.setup();

    render(<MemorySearchPanel enabled />);
    await user.selectOptions(await screen.findByLabelText('memory.search.projectLabel'), 'all');
    await screen.findByText('Aggregate page one.');
    await user.click(screen.getByRole('button', { name: 'memory.search.browse.next' }));
    await screen.findByText('Aggregate page two.');
    expect((screen.getByRole('button', { name: 'memory.search.browse.page:3' }) as HTMLButtonElement).disabled).toBe(false);

    await user.click(screen.getByRole('button', { name: 'memory.search.browse.page:1' }));
    await waitFor(() => expect(pageOneReads).toBe(2));
    expect((screen.getByRole('button', { name: 'memory.search.browse.page:2' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'memory.search.browse.page:3' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('[MEMORY-LIST-003] keeps duplicate aggregate IDs distinct by project', async () => {
    const defaultEpisode = episode({
      id: 'shared-id',
      project: 'default',
      subject: 'Default episode',
      summary: 'Default excerpt.',
      body: 'Default detail body.',
    });
    const notesEpisode = episode({
      id: 'shared-id',
      project: 'notes',
      subject: 'Notes episode',
      summary: 'Notes excerpt.',
      body: 'Notes detail body.',
    });
    api.listMemoryEpisodes.mockImplementation((project: string) => Promise.resolve(
      project === 'all'
        ? listResult([defaultEpisode, notesEpisode], { total_count: 2 })
        : listResult([]),
    ));
    const user = userEvent.setup();

    render(<MemorySearchPanel enabled />);
    await user.selectOptions(await screen.findByLabelText('memory.search.projectLabel'), 'all');
    const defaultRow = await screen.findByRole('button', {
      name: 'memory.search.browse.openDetail:Default episode',
    });
    const notesRow = screen.getByRole('button', {
      name: 'memory.search.browse.openDetail:Notes episode',
    });
    await user.click(notesRow);

    expect(await screen.findByText('Notes detail body.')).toBeTruthy();
    expect(screen.queryByText('Default detail body.')).toBeNull();
    expect(defaultRow.getAttribute('aria-pressed')).toBe('false');
    expect(notesRow.getAttribute('aria-pressed')).toBe('true');
  });

  it('[MEMORY-LIST-005] preserves retry and navigation controls after a page failure', async () => {
    let pageTwoAttempts = 0;
    api.listMemoryEpisodes.mockImplementation((project: string, options: { cursor: string | null }) => {
      if (project !== 'all') return Promise.resolve(listResult([]));
      if (options.cursor === 'cursor-page-2') {
        pageTwoAttempts += 1;
        return Promise.resolve(pageTwoAttempts === 1
          ? { status: 'failed', error: 'memory_provider_timeout' }
          : listResult([episode({ id: 'recovered-page', subject: 'Recovered page.' })], {
              total_count: 21,
            }));
      }
      return Promise.resolve(listResult([episode({ subject: 'First aggregate page.' })], {
        total_count: 21,
        next_cursor: 'cursor-page-2',
      }));
    });
    const user = userEvent.setup();

    render(<MemorySearchPanel enabled />);
    await user.selectOptions(await screen.findByLabelText('memory.search.projectLabel'), 'all');
    await screen.findByText('First aggregate page.');
    await user.click(screen.getByRole('button', { name: 'memory.search.browse.next' }));

    const retry = await screen.findByRole('button', { name: 'memory.search.browse.retry' });
    expect((screen.getByRole('button', { name: 'memory.search.browse.previous' }) as HTMLButtonElement).disabled).toBe(false);
    await user.click(retry);
    expect(await screen.findByText('Recovered page.')).toBeTruthy();
    expect(pageTwoAttempts).toBe(2);
  });

  it('[MEMORY-LIST-005] keeps an empty partial aggregate page resumable', async () => {
    api.listMemoryEpisodes.mockImplementation((project: string, options: { cursor: string | null }) => {
      if (project !== 'all') return Promise.resolve(listResult([]));
      return Promise.resolve(options.cursor === 'retry-cursor'
        ? listResult([episode({ id: 'recovered-entry', project: 'notes' })], { total_count: null })
        : listResult([], {
            total_count: null,
            next_cursor: 'retry-cursor',
            warnings: ['memory_list_partial'],
          }));
    });
    const user = userEvent.setup();

    render(<MemorySearchPanel enabled />);
    await user.selectOptions(await screen.findByLabelText('memory.search.projectLabel'), 'all');

    expect(await screen.findByText('memory.search.browse.partialEmpty')).toBeTruthy();
    expect(screen.queryByText('memory.search.browse.empty')).toBeNull();
    expect(screen.getByText('memory.search.browse.partial')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'memory.search.browse.next' }));

    expect(await screen.findByText('Release planning')).toBeTruthy();
    expect(api.listMemoryEpisodes).toHaveBeenLastCalledWith('all', {
      page: 2,
      cursor: 'retry-cursor',
      limit: 20,
    });
  });

  it('[MEMORY-LIST-005] treats an empty truncated aggregate page as incomplete', async () => {
    api.listMemoryEpisodes.mockImplementation((project: string) => Promise.resolve(
      project === 'all'
        ? listResult([], {
            total_count: null,
            next_cursor: 'retry-cursor',
            warnings: ['memory_list_truncated'],
          })
        : listResult([]),
    ));
    const user = userEvent.setup();

    render(<MemorySearchPanel enabled />);
    await user.selectOptions(await screen.findByLabelText('memory.search.projectLabel'), 'all');

    expect(await screen.findByText('memory.search.browse.partialEmpty')).toBeTruthy();
    expect(screen.queryByText('memory.search.browse.empty')).toBeNull();
    expect(screen.getByText('memory.search.browse.truncated')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'memory.search.browse.next' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('[MEMORY-LIST-007] keeps non-empty queries on the existing relevance search path', async () => {
    api.searchMemory.mockResolvedValue({
      status: 'ok',
      items: [{
        kind: 'episode',
        text: 'The matching relevance-ranked memory.',
        date: '2026-08-14',
        project: 'notes',
      }],
      warnings: [],
    });
    const user = userEvent.setup();

    render(<MemorySearchPanel enabled />);
    await user.type(screen.getByPlaceholderText('memory.search.placeholder'), '  release plan  ');
    await user.selectOptions(await screen.findByLabelText('memory.search.projectLabel'), 'all');
    await user.click(screen.getByRole('button', { name: 'memory.search.button' }));

    expect(await screen.findByText('The matching relevance-ranked memory.')).toBeTruthy();
    expect(api.searchMemory).toHaveBeenCalledWith('release plan', 20, 'all');
  });

  it('[MEMORY-SEARCH-009] labels owner origins and still renders legacy search items', async () => {
    api.searchMemory.mockResolvedValue({
      status: 'ok',
      items: [
        { kind: 'fact', text: 'User result', date: null, origin: 'user' },
        { kind: 'fact', text: 'Agent result', date: null, origin: 'agent' },
        { kind: 'fact', text: 'Shared result', date: null, origin: 'both' },
        { kind: 'fact', text: 'Legacy result', date: null },
      ],
      warnings: [],
    });
    const user = userEvent.setup();

    render(<MemorySearchPanel enabled />);
    await user.type(screen.getByPlaceholderText('memory.search.placeholder'), 'owner labels');
    await user.click(screen.getByRole('button', { name: 'memory.search.button' }));

    expect(await screen.findByText('memory.origin.user')).toBeTruthy();
    expect(screen.getByText('memory.origin.agent')).toBeTruthy();
    expect(screen.getByText('memory.origin.both')).toBeTruthy();
    expect(screen.getByText('Legacy result')).toBeTruthy();
  });

  it('[MEMORY-LIST-008] browses Agent episodes through the explicit owner selector', async () => {
    api.listMemoryEpisodes.mockImplementation((_project: string, options: { origin?: string }) =>
      Promise.resolve(listResult([
        episode({
          id: 'agent-entry',
          subject: 'Agent-owned episode',
          body: 'Agent-owned episode',
          summary: 'Agent-owned episode',
          origin: options.origin === 'agent' ? 'agent' : 'user',
        }),
      ])),
    );
    const user = userEvent.setup();

    render(<MemorySearchPanel enabled />);
    await screen.findByText('Agent-owned episode');
    await user.click(screen.getByRole('radio', { name: 'memory.origin.agent' }));

    await waitFor(() => expect(api.listMemoryEpisodes).toHaveBeenLastCalledWith('default', {
      page: 1,
      cursor: null,
      limit: 20,
      origin: 'agent',
    }));
    expect(screen.getAllByText('memory.origin.agent')).toHaveLength(2);
  });

  it('shows the Memory-closed state without issuing list calls', () => {
    render(<MemorySearchPanel enabled={false} />);

    expect(screen.getByText('memory.search.browse.closedTitle')).toBeTruthy();
    expect(screen.getByText('memory.search.browse.closedDescription')).toBeTruthy();
    expect(api.listMemoryEpisodes).not.toHaveBeenCalled();
  });
});
