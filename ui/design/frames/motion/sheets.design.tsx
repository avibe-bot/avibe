// Sheets on a phone (docs/plans/web-ui-motion.md M6): opening and closing, tier by tier, and dragging
// one down to dismiss it. The sheet is the shipped new-session sheet over the Inbox.
import { frames } from '../../frame';
import { agentsApi } from '../agents/fixtures';
import { inboxFixtures, mergeFixtures, TIER_TITLE } from './fixtures';
import { SheetDragLoop, SheetLoop } from './SheetDemo';

const fixtures = mergeFixtures(inboxFixtures(), { api: agentsApi() });
const phone = { width: 390, height: 844, status: 'proposed' as const, route: '/inbox', fixtures };

const designFrames = frames([
  ...(['now', 'restrained', 'standard', 'pronounced', 'reduced'] as const).map((tier) => ({
    ...phone,
    name: `phone-${tier}`,
    title: `底部弹层打开与关闭 · ${TIER_TITLE[tier]} · 手机`,
    render: () => <SheetLoop tier={tier} />,
  })),
  { ...phone, name: 'phone-drag', title: '底部弹层 · 下拉：短拉回弹，快速下拉关闭 · 标准 · 手机', render: () => <SheetDragLoop /> },
]);

export default designFrames;
