// Mirror the unread total onto the installed app's home-screen icon badge via
// the Badging API. This is distinct from `options.badge` below, which is only
// the small monochrome glyph shown inside the notification itself. Best-effort:
// browsers without the API (and non-installed contexts) simply no-op, and a
// rejected badge promise must never block the notification from showing.
//
// Returns a promise to await, or null when there is nothing to do. `count` is
// the global unread total the server computed for this push; a missing/invalid
// count leaves the existing badge untouched (we don't guess).
function syncAppBadge(count) {
  if (!('setAppBadge' in navigator)) return null;
  if (typeof count !== 'number' || !Number.isFinite(count)) return null;
  const n = Math.max(0, Math.trunc(count));
  const op = n === 0 ? navigator.clearAppBadge?.() : navigator.setAppBadge(n);
  return op && typeof op.catch === 'function' ? op.catch(() => {}) : null;
}

const WEB_PUSH_LAUNCH_CACHE = 'avibe.web-push-launch.v1';
const WEB_PUSH_LAUNCH_ENTRY_PATH = '/__avibe/web-push-launch';

function urlBase64ToUint8Array(value) {
  const padding = '='.repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = self.atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i);
  return output;
}

function arrayBuffersEqual(left, right) {
  if (!left || left.byteLength !== right.byteLength) return false;
  const leftView = new Uint8Array(left);
  const rightView = new Uint8Array(right);
  for (let i = 0; i < leftView.length; i += 1) {
    if (leftView[i] !== rightView[i]) return false;
  }
  return true;
}

async function fetchCsrfToken() {
  const response = await fetch('/api/csrf-token', {
    credentials: 'same-origin',
  });
  if (!response.ok) throw new Error(`CSRF token request failed (${response.status})`);
  const payload = await response.json();
  if (typeof payload?.csrf_token !== 'string' || !payload.csrf_token) {
    throw new Error('CSRF token missing from response');
  }
  return payload.csrf_token;
}

async function syncPushSubscription(subscription, previousEndpoints) {
  const csrfToken = await fetchCsrfToken();
  const response = await fetch('/api/web-push/subscriptions', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-Vibe-CSRF-Token': csrfToken,
    },
    body: JSON.stringify({
      subscription: subscription.toJSON(),
      previous_endpoints: previousEndpoints,
    }),
  });
  if (!response.ok) throw new Error(`Push subscription sync failed (${response.status})`);
}

async function fetchVapidPublicKey() {
  const response = await fetch('/api/web-push/vapid-public-key', {
    credentials: 'same-origin',
  });
  if (!response.ok) throw new Error(`VAPID key request failed (${response.status})`);
  const payload = await response.json();
  if (typeof payload?.public_key !== 'string' || !payload.public_key) {
    throw new Error('VAPID public key missing from response');
  }
  return urlBase64ToUint8Array(payload.public_key);
}

async function replacementSubscription(event) {
  const applicationServerKey = await fetchVapidPublicKey();
  const replacement = event.newSubscription;
  if (
    replacement
    && arrayBuffersEqual(replacement.options?.applicationServerKey, applicationServerKey)
  ) {
    return replacement;
  }
  if (replacement) {
    await replacement.unsubscribe();
  }
  return self.registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey,
  });
}

// iOS may honor an installed PWA's manifest start URL instead of the path passed
// to openWindow(). Leave a short-lived launch handoff in Cache Storage so the
// app shell can still prefer the tapped notification over its remembered page.
function rememberPendingNotificationLaunch(url) {
  if (!self.caches) return Promise.resolve();
  const entryUrl = new URL(WEB_PUSH_LAUNCH_ENTRY_PATH, self.location.origin).href;
  const response = new Response(JSON.stringify({ url, createdAt: Date.now() }), {
    headers: { 'content-type': 'application/json' },
  });
  return self.caches
    .open(WEB_PUSH_LAUNCH_CACHE)
    .then((cache) => cache.put(entryUrl, response))
    .catch(() => {});
}

self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil(
    replacementSubscription(event).then((subscription) =>
      syncPushSubscription(
        subscription,
        event.oldSubscription?.endpoint ? [event.oldSubscription.endpoint] : [],
      ),
    ).catch(() => undefined),
  );
});

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = {};
  }

  const title = typeof payload.title === 'string' && payload.title ? payload.title : 'Avibe';
  const url = typeof payload.url === 'string' && payload.url ? payload.url : '/inbox';
  const options = {
    body: typeof payload.body === 'string' ? payload.body : '',
    tag: typeof payload.tag === 'string' ? payload.tag : undefined,
    renotify: typeof payload.tag === 'string' && payload.tag.length > 0,
    data: { url },
    icon: '/icon-192.png',
    badge: '/icon-192.png',
  };

  const tasks = [self.registration.showNotification(title, options)];
  const badgeTask = syncAppBadge(payload.badge_count);
  if (badgeTask) tasks.push(badgeTask);
  event.waitUntil(Promise.all(tasks));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = new URL(event.notification.data?.url || '/inbox', self.location.origin);
  if (targetUrl.origin !== self.location.origin) {
    targetUrl.href = new URL('/inbox', self.location.origin).href;
  }
  const href = targetUrl.href;
  const message = {
    type: 'vibe.notification-click',
    url: targetUrl.pathname + targetUrl.search + targetUrl.hash,
  };
  const appShellPaths = ['/inbox', '/agents', '/skills', '/harness', '/vaults', '/projects', '/more', '/chat', '/admin'];
  const isAppShellClient = (url) => {
    if (url.origin !== self.location.origin) return false;
    if (url.pathname === '/') return true;
    return appShellPaths.some((path) => url.pathname === path || url.pathname.startsWith(`${path}/`));
  };

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ('focus' in client && isAppShellClient(new URL(client.url))) {
          return client.focus().then((focusedClient) => {
            (focusedClient || client).postMessage(message);
            return focusedClient || client;
          });
        }
      }
      if (self.clients.openWindow) {
        return rememberPendingNotificationLaunch(message.url)
          .then(() => self.clients.openWindow(href))
          .then((openedClient) => {
            try {
              openedClient?.postMessage(message);
            } catch {
              // Cache Storage remains as the cold-launch fallback.
            }
            return openedClient;
          });
      }
      return undefined;
    }),
  );
});
