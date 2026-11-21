// Settings › Notifications on the iPhone App with the Lock Screen progress setting (I5 Amendment 1,
// docs/plans/2026-11-20-ios-live-activity-contract.md §2.2): one device-only switch in the
// "this device" panel, off by default, shown only when the shell offers `liveActivities` at
// version 2. With it on, leaving the App while the open conversation's Turn runs shows that Turn
// (ui/design: ios/live-activity/*). The conversation page carries no entry. The page's other
// panels are unchanged and not redrawn here.
import { frames } from '../../frame';
import { NotificationsDevicePanel, type LockScreenSettingState } from '../../kit/live-activity';
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO } from '../../shell/fixtures';
import { settingsFixtures, settingsPage } from './fixtures';

const fixtures = settingsFixtures(
  {
    '/api/web-push/status': { ok: true, configured: true, public_key: 'BDesignVapidPublicKey', subscription_count: 1, current_subscription_enabled: true },
    '/api/web-push/vapid-public-key': { ok: true, public_key: 'BDesignVapidPublicKey' },
  },
  { nativeShell: { ...IOS_APP_CAPABILITIES, push: 1, liveActivities: 2 } },
);
const frame = (name: string, title: string, state: LockScreenSettingState) => ({
  name,
  title,
  ...IPHONE_17_PRO,
  status: 'proposed' as const,
  route: '/settings/notifications',
  fixtures,
  render: settingsPage('notifications', <NotificationsDevicePanel state={state} />),
});

const designFrames = frames([
  frame('off', 'iPhone · 设置 › 通知 · 在锁屏上显示进度 · 关闭（默认）', 'off'),
  frame('on', 'iPhone · 设置 › 通知 · 在锁屏上显示进度 · 打开', 'on'),
  frame('system-off', 'iPhone · 设置 › 通知 · 系统设置里关了实时活动：开关不可用，给去设置', 'systemOff'),
]);

export default designFrames;
