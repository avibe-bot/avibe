import { describe, expect, it } from 'vitest';

import type { VaultRequest, WorkbenchMessage } from '@/context/ApiContext';
import {
  isVaultApprovalRequest,
  placeVaultProvisionRequests,
  vaultRequestType,
} from './vaultRequestPlacement';

const message = (id: string, createdAt: string, author = 'agent'): WorkbenchMessage => ({
  id,
  scope_id: null,
  session_id: 'ses_test',
  platform: 'avibe',
  author,
  type: author === 'agent' ? 'result' : 'user',
  source: author,
  author_id: null,
  author_name: null,
  native_message_id: null,
  parent_native_message_id: null,
  text: id,
  content: {},
  metadata: {},
  created_at: createdAt,
  updated_at: createdAt,
  delivered_at: null,
  read_at: null,
});

const request = (
  id: string,
  type: string,
  createdAt: string,
  messageId: string | null = null,
  requester: unknown = {},
): VaultRequest => ({
  id,
  request_type: type,
  secret_name: 'API_KEY',
  requester,
  delivery: {},
  status: 'pending',
  message_id: messageId,
  created_at: createdAt,
  decided_at: null,
  expires_at: null,
});

describe('vaultRequestType', () => {
  it('keeps provision separate from access and sign approvals', () => {
    expect(vaultRequestType(request('p', 'provision', '2026-07-30T10:00:00Z'))).toBe('provision');
    expect(isVaultApprovalRequest(request('a', 'access', '2026-07-30T10:00:00Z'))).toBe(true);
    expect(isVaultApprovalRequest(request('s', 'sign', '2026-07-30T10:00:00Z'))).toBe(true);
    expect(isVaultApprovalRequest(request('p', 'provision', '2026-07-30T10:00:00Z'))).toBe(false);
  });
});

describe('placeVaultProvisionRequests', () => {
  const messages = [
    message('user-before', '2026-07-30T09:59:00Z', 'user'),
    message('agent-owner', '2026-07-30T10:00:10Z'),
    message('agent-later', '2026-07-30T10:01:00Z'),
  ];

  it('uses an explicit Agent message anchor when one is present', () => {
    const placed = placeVaultProvisionRequests(
      messages,
      [request('p', 'provision', '2026-07-30T10:00:00Z', 'agent-later')],
    );

    expect(placed.byMessageId.get('agent-later')?.map((item) => item.id)).toEqual(['p']);
    expect(placed.unanchored).toEqual([]);
  });

  it('anchors legacy requests to the first Agent reply after creation', () => {
    const placed = placeVaultProvisionRequests(
      messages,
      [request('p', 'provision', '2026-07-30T10:00:00Z')],
    );

    expect(placed.byMessageId.get('agent-owner')?.map((item) => item.id)).toEqual(['p']);
    expect(placed.byMessageId.has('agent-later')).toBe(false);
  });

  it('uses an available turn identity before the timestamp fallback', () => {
    const identifiedMessages = messages.map((item) => item.id === 'agent-later'
      ? { ...item, metadata: { turn_id: 'turn-owner' } }
      : item);
    const placed = placeVaultProvisionRequests(identifiedMessages, [
      request('p', 'provision', '2026-07-30T10:00:00Z', null, { turn_id: 'turn-owner' }),
    ]);

    expect(placed.byMessageId.get('agent-later')?.map((item) => item.id)).toEqual(['p']);
    expect(placed.byMessageId.has('agent-owner')).toBe(false);
  });

  it('keeps approvals out of message placement and leaves unmatched provisions visible', () => {
    const placed = placeVaultProvisionRequests(messages, [
      request('approval', 'access', '2026-07-30T10:00:00Z'),
      request('future', 'provision', '2026-07-30T11:00:00Z'),
    ]);

    expect([...placed.byMessageId]).toEqual([]);
    expect(placed.unanchored.map((item) => item.id)).toEqual(['future']);
  });

  it('does not attach a request whose owning reply may be outside the retained window', () => {
    const placed = placeVaultProvisionRequests(
      [message('agent-visible', '2026-07-30T10:00:10Z')],
      [request('older', 'provision', '2026-07-30T09:00:00Z')],
    );

    expect([...placed.byMessageId]).toEqual([]);
    expect(placed.unanchored.map((item) => item.id)).toEqual(['older']);
  });
});
