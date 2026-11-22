// Settings › Notifications on the iPhone App: the Lock Screen progress setting (I5 Amendment 1,
// docs/plans/2026-11-20-ios-live-activity-contract.md §2.2). It is one device-only switch, added as the
// last row of the page's "this device" panel, off by default, and shown only when the shell announces
// `lockScreenProgress`. With it on, leaving the App while the open conversation's Turn runs shows that
// Turn (ui/design: ios/live-activity/*). The conversation page carries no entry. As with App Lock
// (ui/design: app-lock/settings/*), the proposed row is drawn alone in the page shell; the page's
// shipped panels and rows are unchanged and are not redrawn.
import { frames } from '../../frame';
import { NotificationsDevicePanel, type LockScreenSettingState } from '../../kit/live-activity';
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO } from '../../shell/fixtures';
import { settingsFixtures, settingsPage } from './fixtures';

const fixtures = settingsFixtures({}, { nativeShell: { ...IOS_APP_CAPABILITIES, lockScreenProgress: 1 } });
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
