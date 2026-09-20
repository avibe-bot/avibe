// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { VaultRequest } from '@/context/ApiContext';
import { VaultChatRequests } from './vault-chat-requests';

vi.mock('./vault-request-card', () => ({
  VaultRequestCard: ({ request }: { request: VaultRequest }) => (
    <div data-testid={`request-card-${request.id}`}>{request.id}</div>
  ),
}));

vi.mock('./vault-approval-dialog', () => ({
  VaultApprovalDialog: () => null,
}));

const request = (id: string, requestType: 'access' | 'provision'): VaultRequest => ({
  id,
  request_type: requestType,
  secret_name: `${id}_SECRET`,
  requester: {},
  delivery: {},
  status: 'pending',
  message_id: null,
  created_at: '2026-08-08T00:00:00Z',
  decided_at: null,
  expires_at: null,
  card: { request_type: requestType },
});

describe('VaultChatRequests', () => {
  afterEach(() => cleanup());

  it('keeps provision forms out of the transcript footer', () => {
    render(
      <VaultChatRequests
        requests={[request('provision', 'provision'), request('approval', 'access')]}
        onResolved={vi.fn()}
      />,
    );

    expect(screen.queryByTestId('request-card-provision')).toBeNull();
    expect(screen.getByTestId('request-card-approval')).toBeTruthy();
  });
});
