// Setup's optional remote-access step (src/components/onboarding/SetupRemoteAccessView.tsx), after
// the AI assistants. It is the same block as Settings · Devices while remote access is off, and one
// tap skips it. Today the browser's Setup says nothing about remote access (the terminal prints a
// hint to run `vibe remote`), and the Desktop App shows 「本机已准备好」 after Setup with
// 「登录并登记本机」, which registers under a random domain (desktop/instances/setup-entry-*); this
// step replaces that screen. Not part of the Setup sequence yet.
import { SetupRemoteAccessView, type SetupRemoteAccessViewProps } from '@/components/onboarding/SetupRemoteAccessView';

import { frames, type FrameSpec } from '../../frame';
import { setupFixtures } from './fixtures';

const frame = (name: string, title: string, props: SetupRemoteAccessViewProps, size = { width: 1440, height: 900 }): FrameSpec => ({
  name, title, ...size, status: 'shipped', themes: ['light'], route: '/setup',
  fixtures: setupFixtures({}),
  render: () => <SetupRemoteAccessView {...props} />,
});

const designFrames = frames([
  frame('browser', '设置向导 · 远程访问（可选）· 浏览器：一个动作，去 avibe.bot 起名后自动回来', { surface: 'browser' }),
  frame('desktop-signed-out', '设置向导 · 远程访问（可选）· Desktop App 未登录：走 App 自己的登录', { surface: 'desktop', account: null, name: '' }),
  frame('desktop-signed-in', '设置向导 · 远程访问（可选）· Desktop App 登录后回到这一步：起名，一个动作', { surface: 'desktop', account: 'max@example.test', name: 'max', nameState: 'free' }),
  frame('browser-phone', '设置向导 · 远程访问（可选）· 手机宽度', { surface: 'browser' }, { width: 390, height: 844 }),
]);

export default designFrames;
