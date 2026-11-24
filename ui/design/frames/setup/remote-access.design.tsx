// Setup's optional remote-access step (src/components/onboarding/SetupRemoteAccessView.tsx), at
// `/setup/remote-access` after the AI assistants: the same claim as Settings · Devices while remote
// access is off, and a text link skips it. Offered only in a browser on this computer or an
// admitted private address, on an instance that is not bound.
import { SetupRemoteAccessView, type SetupRemoteAccessViewProps } from '@/components/onboarding/SetupRemoteAccessView';

import { frames, type FrameSpec } from '../../frame';
import { setupFixtures } from './fixtures';

const frame = (name: string, title: string, props: SetupRemoteAccessViewProps, size = { width: 1440, height: 900 }): FrameSpec => ({
  name, title, ...size, status: 'shipped', themes: ['light'], route: '/setup/remote-access',
  fixtures: setupFixtures({}),
  render: () => <SetupRemoteAccessView {...props} />,
});

const designFrames = frames([
  frame('browser', '设置向导 · 远程访问（可选）：主按钮「开启远程访问」，跳过是一个文字链接', { surface: 'browser' }),
  frame('browser-phone', '设置向导 · 远程访问（可选）· 手机宽度', { surface: 'browser' }, { width: 375, height: 844 }),
  frame('browser-busy', '设置向导 · 点了开启', { surface: 'browser', busy: true }),
  frame('browser-link-required', '设置向导 · 从这个地址开不了：一句话，然后进入工作台', { surface: 'browser', problem: { code: 'link_required' } }),
  frame('browser-unreachable', '设置向导 · 连不上 avibe.bot：可以再试，也可以跳过', { surface: 'browser', problem: { code: 'unreachable' } }),
  frame('browser-support', '设置向导 · 保存的绑定请求是死路：一句话加 support@avibe.bot，然后进入工作台', { surface: 'browser', problem: { code: 'pairing_needs_reconciliation' } }),
  frame('browser-local-busy', '设置向导 · 这台电脑上另一步正在进行：「没有完成，请再试一次」，可以再试，也可以跳过', { surface: 'browser', problem: { code: 'pairing_local_busy' } }),
  frame('browser-unreachable-phone', '设置向导 · 连不上 avibe.bot · 手机宽度', { surface: 'browser', problem: { code: 'unreachable' } }, { width: 375, height: 844 }),
]);

export default designFrames;
