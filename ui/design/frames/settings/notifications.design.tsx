// Settings · Notifications: which messages notify, and push on this device.
import { SettingsNotificationsPage } from '@/components/settings/SettingsNotificationsPage';

import { frames } from '../../frame';
import { settingsFixtures, settingsPage } from './fixtures';

const page = settingsPage('notifications', <SettingsNotificationsPage />);
const route = '/settings/notifications';

// Push is set up on the server and another device subscribes; this browser has not turned it on.
const fixtures = settingsFixtures({
  '/api/web-push/status': { ok: true, configured: true, public_key: 'BDesignVapidPublicKey', subscription_count: 1, current_subscription_enabled: false },
});

const designFrames = frames([
  { name: 'default', title: '设置 · 通知', width: 1440, height: 900, status: 'shipped', route, fixtures, render: page },
  { name: 'phone', title: '设置 · 通知 · 手机', width: 390, height: 844, status: 'shipped', route, fixtures, render: page },
]);

export default designFrames;
