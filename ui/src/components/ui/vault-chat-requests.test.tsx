// @vitest-environment jsdom

import { act, cleanup, render, screen } from '@testing-library/react';
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
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('renders the given provision forms but reports only approvals off-screen', () => {
    const observed: Element[] = [];
    let notify: (entries: Partial<IntersectionObserverEntry>[]) => void = () => {};
    vi.stubGlobal('IntersectionObserver', class {
      constructor(callback: (entries: Partial<IntersectionObserverEntry>[]) => void) {
        notify = callback;
      }
      observe(element: Element) {
        observed.push(element);
      }
      disconnect() {}
    });
    const onOffscreen = vi.fn();
    render(
      <VaultChatRequests
        requests={[request('provision', 'provision'), request('approval', 'access')]}
        onResolved={vi.fn()}
        onOffscreenApprovalsChange={onOffscreen}
      />,
    );

    expect(screen.getByTestId('request-card-provision')).toBeTruthy();
    expect(screen.getByTestId('request-card-approval')).toBeTruthy();
    expect(observed.map((element) => (element as HTMLElement).dataset.requestId)).toEqual(['approval']);

    act(() => notify(observed.map((target) => ({ target, isIntersecting: false }))));
    expect(onOffscreen.mock.lastCall?.[0].map((item: VaultRequest) => item.id)).toEqual(['approval']);
  });
});
