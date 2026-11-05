// A reply arriving (docs/plans/web-ui-motion.md M3, M4): the working bubble gives way to the reply in
// the same place, the conversation glides up by what the reply adds, and the composer hands Stop back to
// Send. The same motion carries a conversation painted from the local cache when the live answer brings
// rows it did not have: a row that is new rises in, nothing that was already there moves on its own,
// and nothing pops.
import { frames } from '../../frame';
import { CHAT, chatStatesFixtures, TIER_TITLE } from './fixtures';
import { ArrivalLoop } from './ArrivalDemo';

const designFrames = frames(
  (['now', 'standard', 'reduced'] as const).map((tier) => ({
    name: `phone-${tier}`,
    title: `回复到达（缓存换成实时数据时同样适用） · ${TIER_TITLE[tier]} · 手机`,
    width: 390,
    height: 844,
    status: 'proposed' as const,
    route: `/chat/${CHAT.replied}`,
    fixtures: chatStatesFixtures('after', 'replied'),
    render: () => <ArrivalLoop tier={tier} />,
  })),
);

export default designFrames;
