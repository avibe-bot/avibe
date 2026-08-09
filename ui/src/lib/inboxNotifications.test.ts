import { describe, expect, it, vi } from 'vitest';

import type { WorkbenchMessage } from '@/context/ApiContext';
import { notifyInboxMessage } from './inboxNotifications';

const message: WorkbenchMessage = {
  id: 'message-1',
  scope_id: 'scope-1',
  session_id: 'session-1',
  platform: 'avibe',
  author: 'agent',
  type: 'result',
  source: 'agent',
  author_id: null,
  author_name: null,
  native_message_id: null,
  parent_native_message_id: null,
  text: 'Reply is ready',
  content: {},
  metadata: {},
  created_at: '2026-08-10T00:00:00Z',
  updated_at: '2026-08-10T00:00:00Z',
  delivered_at: null,
  read_at: null,
};

const host = (overrides: Partial<Parameters<typeof notifyInboxMessage>[1]> = {}) => ({
  isPageActive: () => false,
  notificationPermission: () => 'granted' as NotificationPermission,
  getPushSubscription: vi.fn(async () => null),
  show: vi.fn(),
  ...overrides,
});

describe('notifyInboxMessage', () => {
  it('shows a local notification for an unread agent result while inactive', async () => {
    const runtime = host();
    await expect(notifyInboxMessage(message, runtime)).resolves.toBe(true);
    expect(runtime.show).toHaveBeenCalledWith(
      'Avibe',
      expect.objectContaining({ body: 'Reply is ready', tag: 'avibe-session-session-1' }),
    );
  });

  it('keeps notification previews bounded', async () => {
    const runtime = host();
    await notifyInboxMessage({ ...message, text: 'x'.repeat(300) }, runtime);
    expect(runtime.show).toHaveBeenCalledWith('Avibe', expect.objectContaining({ body: `${'x'.repeat(237)}...` }));
  });

  it('does not duplicate a notification when Web Push already has a subscription', async () => {
    const runtime = host({ getPushSubscription: vi.fn(async () => ({ endpoint: 'https://push.test' } as PushSubscription)) });
    await expect(notifyInboxMessage(message, runtime)).resolves.toBe(false);
    expect(runtime.show).not.toHaveBeenCalled();
  });

  it('keeps the local fallback when subscription lookup is unavailable', async () => {
    const runtime = host({ getPushSubscription: vi.fn(async () => { throw new Error('service worker unavailable'); }) });
    await expect(notifyInboxMessage(message, runtime)).resolves.toBe(true);
    expect(runtime.show).toHaveBeenCalledOnce();
  });

  it('does not notify while the page is focused or for non-result events', async () => {
    const active = host({ isPageActive: () => true });
    await expect(notifyInboxMessage(message, active)).resolves.toBe(false);
    await expect(notifyInboxMessage({ ...message, type: 'assistant' }, host())).resolves.toBe(false);
  });

  it('notifies when another visible route is open', async () => {
    const runtime = host({ isPageActive: () => true, isCurrentChatVisible: () => false });
    await expect(notifyInboxMessage(message, runtime)).resolves.toBe(true);
    expect(runtime.show).toHaveBeenCalledOnce();
  });
});
