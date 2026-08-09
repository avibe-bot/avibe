import type { WorkbenchMessage } from '@/context/ApiContext';
import { getExistingWebPushSubscription } from './webPush';
import { readPageActivity } from './pageActivity';

export type InboxNotificationHost = {
  isPageActive: () => boolean;
  isCurrentChatVisible?: (sessionId: string) => boolean;
  notificationPermission: () => NotificationPermission | null;
  getPushSubscription: () => Promise<PushSubscription | null>;
  show: (title: string, options: NotificationOptions) => void;
};

const browserHost: InboxNotificationHost = {
  isPageActive: readPageActivity,
  isCurrentChatVisible: (sessionId) => {
    if (typeof window === 'undefined') return false;
    const match = window.location.pathname.match(/^\/chat\/([^/]+)$/);
    if (!match) return false;
    try {
      return decodeURIComponent(match[1]) === sessionId;
    } catch {
      return false;
    }
  },
  notificationPermission: () => (typeof Notification === 'undefined' ? null : Notification.permission),
  getPushSubscription: getExistingWebPushSubscription,
  show: (title, options) => {
    new Notification(title, options);
  },
};

export function isInboxNotificationMessage(message: WorkbenchMessage): boolean {
  return message.platform === 'avibe' && message.author === 'agent' && message.type === 'result' && Boolean(message.session_id);
}

/** Show a tab-local notification only when the page is not being presented. */
export async function notifyInboxMessage(
  message: WorkbenchMessage,
  host: InboxNotificationHost = browserHost,
): Promise<boolean> {
  if (!isInboxNotificationMessage(message)) return false;
  if (
    (host.isPageActive() &&
      (!host.isCurrentChatVisible || host.isCurrentChatVisible(message.session_id!))) ||
    host.notificationPermission() !== 'granted'
  ) {
    return false;
  }

  // The service worker owns notifications once Web Push is enabled. Do not add
  // a second Notification from the foreground SSE stream in that case.
  try {
    if (await host.getPushSubscription()) return false;
  } catch {
    // A broken or unavailable registration must not suppress the local fallback.
  }

  const trimmedBody = message.text?.trim() || '';
  const body = trimmedBody.length > 240 ? `${trimmedBody.slice(0, 237)}...` : trimmedBody;
  try {
    host.show('Avibe', {
      ...(body ? { body } : {}),
      icon: '/logo.png',
      tag: `avibe-session-${message.session_id}`,
      data: { url: `/chat/${encodeURIComponent(message.session_id!)}` },
    });
    return true;
  } catch {
    return false;
  }
}
