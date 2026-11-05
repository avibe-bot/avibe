// A reply arriving (docs/plans/web-ui-motion.md M3, M4), approved on 2026-11-05: the working bubble gives
// way to the reply in the same place and the conversation glides up by what it adds. The same motion
// carries a cached conversation when the live answer brings rows it did not have.
import { frames } from '../../frame';
import { CHAT, chatStatesFixtures } from './fixtures';
import { ArrivalLoop } from './ArrivalDemo';

const phone = { width: 390, height: 844, status: 'approved' as const, route: `/chat/${CHAT.replied}`, fixtures: chatStatesFixtures('after', 'replied') };

const designFrames = frames([
  { ...phone, name: 'phone', title: '回复到达（缓存换成实时数据时同样适用） · 手机', render: () => <ArrivalLoop /> },
  { ...phone, name: 'phone-reduced', title: '回复到达 · 减少动态效果 · 手机', render: () => <ArrivalLoop reduced /> },
]);

export default designFrames;
