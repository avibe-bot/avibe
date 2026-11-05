import { frames } from '../../frame';
import { MobileDirectionScreen, type DirectionFixture, type MobileDirection, type DirectionSurface } from '../../kit/mobile-directions';
import { account, mixedInstances } from './fixtures';

const fixture: DirectionFixture = { email: account.email, code: '12', instances: mixedInstances };
const directions: { id: MobileDirection; name: string }[] = [
  { id: 'a', name: '云间来信' },
  { id: 'b', name: '随身工作台' },
  { id: 'c', name: '安静的入口' },
];
const surfaces: { id: DirectionSurface; name: string }[] = [
  { id: 'welcome', name: '欢迎' },
  { id: 'email', name: '邮箱' },
  { id: 'code', name: '验证码 · 输入中' },
  { id: 'instances', name: '选择实例' },
];
const designFrames = frames(directions.flatMap(direction => surfaces.map(surface => ({
  name: `${direction.id}-${surface.id}`,
  title: `${direction.id.toUpperCase()} · ${direction.name} · ${surface.name}`,
  width: 390, height: 844, status: 'proposed' as const, themes: ['light', 'dark'] as ('light' | 'dark')[],
  fixtures: { bots: [] },
  render: () => <MobileDirectionScreen direction={direction.id} surface={surface.id} fixture={fixture} />,
}))));

export default designFrames;
