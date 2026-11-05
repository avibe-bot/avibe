// Sheets on a phone (docs/plans/web-ui-motion.md M6), approved on 2026-11-05: opening and closing on the
// sheet spring, dragging one down to dismiss it, and the reduced-motion fade. The sheet is the shipped
// new-session sheet over the Inbox.
import { frames } from '../../frame';
import { agentsApi } from '../agents/fixtures';
import { inboxFixtures, mergeFixtures } from './fixtures';
import { SheetDragLoop, SheetLoop } from './SheetDemo';

const fixtures = mergeFixtures(inboxFixtures(), { api: agentsApi() });
const phone = { width: 390, height: 844, status: 'approved' as const, route: '/inbox', fixtures };

const designFrames = frames([
  { ...phone, name: 'phone', title: '底部弹层打开与关闭 · 手机', render: () => <SheetLoop /> },
  { ...phone, name: 'phone-drag', title: '底部弹层 · 下拉：短拉回弹，快速下拉关闭 · 手机', render: () => <SheetDragLoop /> },
  { ...phone, name: 'phone-reduced', title: '底部弹层 · 减少动态效果 · 手机', render: () => <SheetLoop reduced /> },
]);

export default designFrames;
