/* @vitest-environment jsdom */

// A dialog opened from a pending row outlives that row: the request it answers leaves the list while
// the dialog finishes (a seed-phrase wallet's done screen, a failed restriction's retry), so the
// section keeps the dialog until the user closes it. The Inbox drops a request as soon as a read no
// longer lists it, which is how the dialog would otherwise vanish mid-step.
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import type { VaultRequest } from '@/context/ApiContext';

import { VaultPendingRequests } from './vault-pending-requests';

vi.mock('@/context/ApiContext', async (original) => ({ ...await original<typeof import('@/context/ApiContext')>(), useApi: () => ({}) }));
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('./vault-approval-dialog', () => ({
  VaultApprovalDialog: ({ request }: { request: VaultRequest | null }) => (request ? <div role="dialog">{`approve:${request.id}`}</div> : null),
}));
vi.mock('./vault-secret-dialog', () => ({
  VaultSecretDialog: ({ request }: { request: VaultRequest }) => <div role="dialog">{`provide:${request.id}`}</div>,
}));

afterEach(cleanup);

const request = (id: string, type: string) =>
  ({ id, request_type: type, secret_name: id.toUpperCase(), card: { request_type: type }, session: null }) as unknown as VaultRequest;

it.each([
  ['an approval', request('vrq_use', 'access'), 'vaults.requests.review', 'approve:vrq_use'],
  ['a value to provide', request('vrq_new', 'provision'), 'vaults.request.provide', 'provide:vrq_new'],
])('keeps %s open after its request leaves the list', (_label, pending, button, dialog) => {
  const view = (requests: VaultRequest[]) => (
    <MemoryRouter><VaultPendingRequests requests={requests} onSettled={vi.fn()} /></MemoryRouter>
  );
  const { rerender } = render(view([pending]));
  fireEvent.click(screen.getByRole('button', { name: button }));
  expect(screen.getByRole('dialog').textContent).toBe(dialog);

  rerender(view([]));
  expect(screen.getByRole('dialog').textContent).toBe(dialog);
});

it('draws nothing while nothing is listed, open or to open', () => {
  const { container } = render(<MemoryRouter><VaultPendingRequests requests={[]} onSettled={vi.fn()} /></MemoryRouter>);
  expect(container.innerHTML).toBe('');
});
