import { describe, expect, it } from 'vitest';

import type { VaultRequest, WorkbenchMessage } from '@/context/ApiContext';
import {
  isVaultApprovalRequest,
  placeVaultProvisionRequests,
  vaultRequestType,
} from './vaultRequestPlacement';

const message = (
  id: string,
  createdAt: string,
  author = 'agent',
  deliveredAt: string | null = null,
): WorkbenchMessage => ({
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
  delivered_at: deliveredAt,
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

const orderedMessage = (
  idSuffix: string,
  orderAt: string,
  createdAt: string,
  author = 'agent',
): WorkbenchMessage => {
  const microseconds = Math.floor(Date.parse(orderAt) * 1_000);
  return message(`msg_${microseconds.toString(16).padStart(15, '0')}${idSuffix}`, createdAt, author);
};

const microsecondMessage = (
  idSuffix: string,
  microsecondsAfterSecond: number,
  author = 'agent',
): WorkbenchMessage => {
  const secondStart = Date.parse('2026-07-30T10:00:00Z') * 1_000;
  return message(
    `msg_${(secondStart + microsecondsAfterSecond).toString(16).padStart(15, '0')}${idSuffix}`,
    '2026-07-30T10:00:00Z',
    author,
  );
};

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

  it('uses the message id clock when the reply shares the request second', () => {
    const sameSecondMessages = [
      orderedMessage('11111111', '2026-07-30T10:00:00.100Z', '2026-07-30T10:00:00Z', 'user'),
      orderedMessage('22222222', '2026-07-30T10:00:00.800Z', '2026-07-30T10:00:00Z'),
    ];
    const placed = placeVaultProvisionRequests(sameSecondMessages, [
      request('p', 'provision', '2026-07-30T10:00:00.500Z'),
    ]);

    expect(placed.byMessageId.get(sameSecondMessages[1].id)?.map((item) => item.id)).toEqual(['p']);
    expect(placed.unanchored).toEqual([]);
  });

  it('does not round a request back onto an earlier reply in the same millisecond', () => {
    const earlierInput = microsecondMessage('11111111', 100_000, 'user');
    const earlierReply = microsecondMessage('22222222', 500_100);
    const ownerReply = microsecondMessage('33333333', 500_950);
    const placed = placeVaultProvisionRequests([earlierInput, earlierReply, ownerReply], [
      request('p', 'provision', '2026-07-30T10:00:00.500900Z'),
    ]);

    expect(placed.byMessageId.has(earlierReply.id)).toBe(false);
    expect(placed.byMessageId.get(ownerReply.id)?.map((item) => item.id)).toEqual(['p']);
    expect(placed.unanchored).toEqual([]);
  });

  it('does not cross a later user or harness input-turn boundary', () => {
    for (const boundaryKind of ['user', 'harness'] as const) {
      const origin = orderedMessage('11111111', '2026-07-30T10:00:00.100Z', '2026-07-30T10:00:00Z', 'user');
      const boundary = orderedMessage('22222222', '2026-07-30T10:00:00.700Z', '2026-07-30T10:00:00Z', boundaryKind);
      if (boundaryKind === 'harness') boundary.type = 'harness';
      const unrelated = orderedMessage('33333333', '2026-07-30T10:00:00.900Z', '2026-07-30T10:00:00Z');
      const placed = placeVaultProvisionRequests([origin, boundary, unrelated], [
        request(`p-${boundaryKind}`, 'provision', '2026-07-30T10:00:00.500Z'),
      ]);

      expect([...placed.byMessageId]).toEqual([]);
      expect(placed.unanchored.map((item) => item.id)).toEqual([`p-${boundaryKind}`]);
    }
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

  it('uses the requester input message when the request was persisted after its reply', () => {
    const source = message('source-user', '2026-07-30T10:00:00Z', 'user');
    const reply = message('owner-reply', '2026-07-30T10:01:00Z', 'agent');
    const placed = placeVaultProvisionRequests(
      [source, reply],
      [request('late', 'provision', '2026-07-30T10:02:00Z', null, { message_id: source.id })],
    );

    expect(placed.byMessageId.get(reply.id)?.map((item) => item.id)).toEqual(['late']);
    expect(placed.unanchored).toEqual([]);
  });

  it('resolves a native requester message id to its durable transcript row', () => {
    const source = { ...message('source-user', '2026-07-30T10:00:00Z', 'user'), native_message_id: 'slack-source' };
    const reply = message('owner-reply', '2026-07-30T10:01:00Z', 'agent');
    const placed = placeVaultProvisionRequests(
      [source, reply],
      [request('native-late', 'provision', '2026-07-30T10:02:00Z', null, { message_id: 'slack-source' })],
    );

    expect(placed.byMessageId.get(reply.id)?.map((item) => item.id)).toEqual(['native-late']);
    expect(placed.unanchored).toEqual([]);
  });

  it('scopes native requester matching to the originating platform', () => {
    const webSource = {
      ...message('web-source', '2026-07-30T10:00:00Z', 'user'),
      platform: 'web',
      native_message_id: 'shared-source',
    };
    const webReply = { ...message('web-reply', '2026-07-30T10:01:00Z'), platform: 'web' };
    const slackSource = {
      ...message('slack-source', '2026-07-30T10:02:00Z', 'user'),
      platform: 'slack',
      native_message_id: 'shared-source',
    };
    const slackReply = { ...message('slack-reply', '2026-07-30T10:03:00Z'), platform: 'slack' };
    const placed = placeVaultProvisionRequests(
      [webSource, webReply, slackSource, slackReply],
      [request('native-platform', 'provision', '2026-07-30T10:04:00Z', null, {
        message_id: 'shared-source',
        platform: 'slack',
      })],
    );

    expect(placed.byMessageId.get(slackReply.id)?.map((item) => item.id)).toEqual(['native-platform']);
    expect(placed.byMessageId.has(webReply.id)).toBe(false);
    expect(placed.unanchored).toEqual([]);
  });

  it('uses a resolved durable source override for stable turn identities', () => {
    const source = message('source-user', '2026-07-30T10:00:00Z', 'user');
    const reply = message('owner-reply', '2026-07-30T10:01:00Z', 'agent');
    const placed = placeVaultProvisionRequests(
      [source, reply],
      [request('turn-late', 'provision', '2026-07-30T10:02:00Z', null, { turn_id: 'turn-owner' })],
      new Map([['turn-late', source.id]]),
    );

    expect(placed.byMessageId.get(reply.id)?.map((item) => item.id)).toEqual(['turn-late']);
    expect(placed.unanchored).toEqual([]);
  });

  it('does not cross a later input when resolving from the requester message', () => {
    const source = message('source-user', '2026-07-30T10:00:00Z', 'user');
    const boundary = message('later-user', '2026-07-30T10:01:00Z', 'user');
    const reply = message('unrelated-reply', '2026-07-30T10:02:00Z', 'agent');
    const placed = placeVaultProvisionRequests(
      [source, boundary, reply],
      [request('late', 'provision', '2026-07-30T10:03:00Z', null, { message_id: source.id })],
    );

    expect([...placed.byMessageId]).toEqual([]);
    expect(placed.unanchored.map((item) => item.id)).toEqual(['late']);
  });

  it('does not timestamp-place an unresolved explicit source onto another turn', () => {
    const unrelatedReply = message('unrelated-reply', '2026-07-30T10:02:00Z', 'agent');
    const placed = placeVaultProvisionRequests(
      [unrelatedReply],
      [request('missing-source', 'provision', '2026-07-30T10:01:00Z', null, { message_id: 'trimmed-source' })],
    );

    expect([...placed.byMessageId]).toEqual([]);
    expect(placed.unanchored.map((item) => item.id)).toEqual(['missing-source']);
  });

  it('keeps a delayed reply when the next input arrives after request creation', () => {
    const source = message('source-user', '2026-07-30T10:00:00Z', 'user');
    const reply = message('owner-reply', '2026-07-30T10:01:00Z', 'agent');
    const laterUser = message('later-user', '2026-07-30T10:03:00Z', 'user');
    const unrelatedReply = message('unrelated-reply', '2026-07-30T10:04:00Z', 'agent');
    const placed = placeVaultProvisionRequests(
      [source, reply, laterUser, unrelatedReply],
      [request('late', 'provision', '2026-07-30T10:02:00Z', null, { message_id: source.id })],
    );

    expect(placed.byMessageId.get(reply.id)?.map((item) => item.id)).toEqual(['late']);
    expect(placed.byMessageId.has(unrelatedReply.id)).toBe(false);
    expect(placed.unanchored).toEqual([]);
  });

  it('uses transcript time for queued source-turn boundaries', () => {
    const source = message('source-user', '2026-07-30T10:00:00Z', 'user');
    const ownerReply = message('owner-reply', '2026-07-30T10:00:30Z', 'agent');
    const queuedInput = message(
      'queued-input',
      '2026-07-30T09:59:00Z',
      'user',
      '2026-07-30T10:02:00Z',
    );
    const unrelatedReply = message('unrelated-reply', '2026-07-30T10:03:00Z');
    const placed = placeVaultProvisionRequests(
      [source, ownerReply, queuedInput, unrelatedReply],
      [request('late', 'provision', '2026-07-30T10:01:00Z', null, { message_id: source.id })],
    );

    expect(placed.byMessageId.get(ownerReply.id)?.map((item) => item.id)).toEqual(['late']);
    expect(placed.byMessageId.has(unrelatedReply.id)).toBe(false);
    expect(placed.unanchored).toEqual([]);
  });

  it('ignores a stale requester message when a later turn owns the request', () => {
    const source = message('stale-source', '2026-07-30T10:00:00Z', 'user');
    const oldReply = message('old-reply', '2026-07-30T10:00:10Z', 'agent');
    const actualSource = message('actual-source', '2026-07-30T10:01:00Z', 'user');
    const actualReply = message('actual-reply', '2026-07-30T10:02:00Z', 'agent');
    const placed = placeVaultProvisionRequests(
      [source, oldReply, actualSource, actualReply],
      [request('late', 'provision', '2026-07-30T10:01:30Z', null, { message_id: source.id })],
    );

    expect(placed.byMessageId.get(actualReply.id)?.map((item) => item.id)).toEqual(['late']);
    expect(placed.byMessageId.has(oldReply.id)).toBe(false);
    expect(placed.unanchored).toEqual([]);
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

  it('uses transcript-entry time when a queued row was accepted after the request', () => {
    const queuedInput = message(
      'queued-input',
      '2026-07-30T09:59:00Z',
      'user',
      '2026-07-30T10:02:00Z',
    );
    const unrelatedReply = message('unrelated-reply', '2026-07-30T10:03:00Z');
    const placed = placeVaultProvisionRequests(
      [queuedInput, unrelatedReply],
      [request('trimmed', 'provision', '2026-07-30T10:01:00Z')],
    );

    expect([...placed.byMessageId]).toEqual([]);
    expect(placed.unanchored.map((item) => item.id)).toEqual(['trimmed']);
  });
});
