// App Lock in Settings › General (iOS I7-7A, Desktop D7-7A), shown only when the shell offers
// App Lock. One choice the shell stores on this device; every change is saved only after one
// system verification. The panel is drawn alone: the page's other panels are unchanged.
import type { AppLockSettingActivity } from '@/components/settings/AppLockSetting';
import type { AppLockDevice } from '@/lib/appLock';
import { APP_LOCK_FIXTURES, type AppLockStatus } from '@/lib/appLockContract';

import { frames, type FrameSpec } from '../../frame';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { General } from './fixtures';

const route = '/settings/general';
const fixtures = settingsFixtures();

const frame = (name: string, title: string, device: AppLockDevice, status: AppLockStatus, activity: AppLockSettingActivity = 'idle'): FrameSpec => ({
  name,
  title,
  width: device === 'iphone' ? 390 : 1440,
  height: device === 'iphone' ? 844 : 620,
  status: 'shipped',
  route,
  fixtures,
  render: settingsPage('general', <General device={device} status={status} activity={activity} />),
});

const f = APP_LOCK_FIXTURES;
const designFrames = frames([
  frame('iphone-off', 'iPhone · 设置 · App 锁 · 关闭（默认）', 'iphone', f.iphoneOff),
  frame('iphone-on', 'iPhone · 设置 · App 锁 · 5 分钟后', 'iphone', f.iphoneFiveMinutes),
  frame('iphone-verifying', 'iPhone · 设置 · 更改中，系统面板验证', 'iphone', { ...f.iphoneOff, policy: 'immediately' }, 'verifying'),
  frame('iphone-failed', 'iPhone · 设置 · 验证取消/未通过，设置不变', 'iphone', f.iphoneOff, 'failed'),
  frame('iphone-no-passcode', 'iPhone · 设置 · 设备没有密码，不能开启', 'iphone', f.iphoneNoPasscode),
  frame('iphone-face-id-denied', 'iPhone · 设置 · 未允许面容 ID，用设备密码', 'iphone', f.iphoneFaceIdDenied),
  frame('mac-off', 'Mac · 设置 · App 锁 · 关闭（默认）', 'mac', { ...f.macOneMinute, policy: 'off' }),
  frame('mac-on', 'Mac · 设置 · App 锁 · 1 分钟后', 'mac', f.macOneMinute),
  frame('mac-failed', 'Mac · 设置 · 验证取消/未通过，设置不变', 'mac', { ...f.macOneMinute, policy: 'immediately' }, 'failed'),
  frame('mac-password-only', 'Mac · 设置 · 无可用触控 ID，用登录密码', 'mac', f.macLidClosed),
  frame('mac-no-password', 'Mac · 设置 · 用户没有登录密码，不能开启', 'mac', { ...f.macOneMinute, policy: 'off', available: false }),
]);

export default designFrames;
