// The lock screen (iOS I7-7A, Desktop D7-7A). Drawn natively above every window by one lock
// owner per app; this is its visual spec. Cancel returns to `ready` (not an error). The system
// Face ID / Touch ID / password sheet itself is the OS's and is not drawn.
import { frames, type FrameSpec } from '../../frame';
import { LockScreen, type LockDevice, type LockState, type PasscodeReason } from '../../kit/app-lock';

type Size = [number, number];
const IPHONE: Size = [390, 844];
const MAC_MAIN: Size = [1280, 800];
const MAC_WINDOW: Size = [880, 600];

const frame = (
  name: string,
  title: string,
  device: LockDevice,
  size: Size,
  props: { state?: LockState; reason?: PasscodeReason; compact?: boolean; pending?: 'askVibey' | 'link' } = {},
): FrameSpec => ({
  name, title, width: size[0], height: size[1], status: 'approved',
  render: () => <LockScreen device={device} {...props} />,
});

const designFrames = frames([
  frame('iphone-ready', 'iPhone · 已锁定 · 冷启动/超时回来，自动弹面容 ID；取消后也是这里', 'iphone', IPHONE),
  frame('iphone-verifying', 'iPhone · 系统面板验证中', 'iphone', IPHONE, { state: 'verifying' }),
  frame('iphone-failed', 'iPhone · 验证未通过', 'iphone', IPHONE, { state: 'failed' }),
  frame('iphone-not-enrolled', 'iPhone · 未录入面容 ID，用设备密码', 'iphone', IPHONE, { state: 'passcodeOnly', reason: 'notEnrolled' }),
  frame('iphone-denied', 'iPhone · 未允许面容 ID，用设备密码', 'iphone', IPHONE, { state: 'passcodeOnly', reason: 'denied' }),
  frame('iphone-lockout', 'iPhone · 面容 ID 多次失败，系统转设备密码', 'iphone', IPHONE, { state: 'lockout' }),
  frame('iphone-switcher', 'iPhone · App 切换器快照 / 离开时的遮罩', 'iphone', IPHONE, { state: 'cover' }),
  frame('mac-main-ready', 'Mac · 主窗 · 已锁定（触控 ID）', 'mac', MAC_MAIN),
  frame('mac-main-failed', 'Mac · 主窗 · 验证未通过', 'mac', MAC_MAIN, { state: 'failed' }),
  frame('mac-main-unavailable', 'Mac · 主窗 · 触控 ID 不可用（合盖/外接键盘），用登录密码', 'mac', MAC_MAIN, { state: 'passcodeOnly', reason: 'unavailable' }),
  frame('mac-main-no-touch-id', 'Mac · 主窗 · 没有录入指纹/无触控 ID 的 Mac', 'mac', MAC_MAIN, { state: 'passcodeOnly', reason: 'notEnrolled' }),
  frame('mac-main-lockout', 'Mac · 主窗 · 触控 ID 多次失败，转登录密码', 'mac', MAC_MAIN, { state: 'lockout' }),
  frame('mac-main-ask-vibey', 'Mac · 主窗 · 锁住时收到「发给 Vibey」，解锁后进输入框', 'mac', MAC_MAIN, { pending: 'askVibey' }),
  frame('mac-window', 'Mac · 独立窗（Show/文件/终端）· 同一把锁', 'mac', MAC_WINDOW, { compact: true }),
  frame('mac-window-verifying', 'Mac · 独立窗 · 验证中', 'mac', MAC_WINDOW, { compact: true, state: 'verifying' }),
]);

export default designFrames;
