/* @vitest-environment jsdom */

// ui/design: inbox/approvals/desktop. The Inbox lists the Vault requests still waiting on the
// reader, above the conversations, and reads them again each time it is entered; nothing polls, so
// entering is when one that expired meanwhile leaves the list (and the entry's count).
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import type { VaultRequest } from '../../context/ApiContext';
import { PendingVaultRequestsContext } from '../../context/PendingVaultRequestsContext';
import { InboxPage } from './InboxPage';

vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: () => ({
    inboxSessions: [], unreadBySession: {}, totalUnread: 0, unreadSessions: 0,
    unreadByCategory: { bot: 0, session: 0 }, nextCursor: null, loading: false, loadingMore: false,
    feedStatus: 'loaded', refresh: vi.fn(), loadMore: vi.fn(), markRead: vi.fn(),
  }),
}));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_chat: true } }),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../ui/vault-pending-requests', () => ({
  VaultPendingRequests: ({ requests }: { requests: VaultRequest[] }) => (
    <ul aria-label="pending">{requests.map((request) => <li key={request.id}>{request.id}</li>)}</ul>
  ),
}));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const request = (id: string, expiresAt: string | null) => ({ id, expires_at: expiresAt }) as VaultRequest;

it('lists the requests still waiting and reads them again on entry', () => {
  vi.useFakeTimers({ now: new Date('2026-11-16T04:00:00Z'), toFake: ['Date'] });
  const refresh = vi.fn();
  render(
    <PendingVaultRequestsContext.Provider value={{
      requests: [request('vrq_live', '2026-11-16T04:10:00Z'), request('vrq_expired', '2026-11-16T03:50:00Z'), request('vrq_open', null)],
      refresh,
      forget: vi.fn(),
    }}>
      <MemoryRouter initialEntries={['/inbox']}><InboxPage onOpenSearch={() => {}} /></MemoryRouter>
    </PendingVaultRequestsContext.Provider>,
  );

  expect(screen.getByRole('list', { name: 'pending' }).textContent).toBe('vrq_livevrq_open');
  expect(refresh).toHaveBeenCalled();
});

it('keeps the section mounted while nothing waits, so a dialog it opened can finish', () => {
  // The request a dialog answers leaves the list while the dialog finishes (a wallet's done screen);
  // the section itself decides it has nothing to draw.
  render(<MemoryRouter initialEntries={['/inbox']}><InboxPage onOpenSearch={() => {}} /></MemoryRouter>);
  expect(screen.getByRole('list', { name: 'pending' }).textContent).toBe('');
});
