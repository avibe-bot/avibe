/* @vitest-environment jsdom */

// Owner, 11-18: under a 工作区通知 error, open the Session it came from and 「重试」
// it there, with the chat page's retry; a failure that cannot be retried offers
// only the open button, and a started retry cannot be pressed twice.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import type { WorkbenchMessage } from '../../context/ApiContext';
import { WorkspaceNoticeActions } from './WorkspaceNoticeActions';

const apiFetch = vi.hoisted(() => vi.fn());
vi.mock('../../lib/apiFetch', () => ({ apiFetch }));
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

afterEach(() => {
  cleanup();
  apiFetch.mockReset();
});

const notice = (content: Record<string, unknown> = {}) => ({
  id: 'msg_ws', type: 'notify', author: 'agent', source: 'agent', session_id: 'ses-workspace-notices',
  text: 'Backend interrupted', content, metadata: { event: 'backend_failure', failure_id: 'turn:trn_a' },
}) as unknown as WorkbenchMessage;

const draw = (message: WorkbenchMessage, retry: boolean) => render(
  <MemoryRouter><WorkspaceNoticeActions message={message} source={{ session_id: 'ses_src', retry }} /></MemoryRouter>,
);

it('opens the Session the failure came from, and retries it there once', async () => {
  apiFetch.mockResolvedValue(new Response(JSON.stringify({ id: 'dlv_1', state: 'accepted' }), { status: 201 }));
  draw(notice(), true);
  expect(screen.getByRole('link', { name: /chat.workspaceNotice.openSession/ }).getAttribute('href')).toBe('/chat/ses_src');

  const button = screen.getByRole('button', { name: /common.retry/ });
  await act(async () => { fireEvent.click(button); });
  expect(apiFetch).toHaveBeenCalledExactlyOnceWith('/api/sessions/ses_src/messages', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ retry_for: 'msg_ws' }),
  }));
  const started = screen.getByRole('button', { name: /chat.retryRequested/ });
  expect((started as HTMLButtonElement).disabled).toBe(true);
  await act(async () => { fireEvent.click(started); });
  expect(apiFetch).toHaveBeenCalledTimes(1);
});

it('offers only the open button for a failure that cannot be retried', () => {
  draw(notice(), false);
  expect(screen.getByRole('link', { name: /chat.workspaceNotice.openSession/ })).toBeTruthy();
  expect(screen.queryByRole('button')).toBeNull();
});

it('shows a retry already started as started', () => {
  draw(notice({ failure_retry: { delivery_id: 'dlv_1', state: 'accepted' } }), false);
  expect((screen.getByRole('button', { name: /chat.retryRequested/ }) as HTMLButtonElement).disabled).toBe(true);
});
