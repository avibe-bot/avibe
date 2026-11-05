// Sheets (docs/plans/web-ui-motion.md M6), as shipped: the new-session sheet, a shipped Dialog, over the
// Inbox. On a phone it rises from the bottom edge on the sheet spring while the page behind dims, leaves
// faster, and can be dragged down to dismiss it; on a desktop the dialog fades and grows. Each frame
// plays the shipped animation as the sheet opens (the canvas replays it) and settles open.
import type { ReactNode } from 'react';

import { InboxPage } from '@/components/workbench/InboxPage';
import { NewSessionSheet } from '@/components/workbench/NewSessionSheet';

import { frames } from '../../frame';
import { InboxAt, Layer, PhoneStage } from '../../kit/motion/Stage';
import { TouchPoint } from '../../shell/TouchPoint';
import { agentsApi } from '../agents/fixtures';
import { consolePage } from '../workbench/fixtures';
import { inboxFixtures, mergeFixtures } from './fixtures';

const fixtures = mergeFixtures(inboxFixtures(), { api: agentsApi() });
const REPLAY = 1600;
const phone = { width: 390, height: 844, status: 'shipped' as const, route: '/inbox', fixtures, replay: REPLAY };

const sheetOverInbox = (pose?: ReactNode) => () => (
  <PhoneStage>
    <Layer name="page" opaque><InboxAt state="after" /></Layer>
    <NewSessionSheet open onClose={() => {}} onOpen={() => {}} />
    {pose}
  </PhoneStage>
);

/** Where a finger has pulled the sheet: 120 px down from its resting place, held by its header (the shipped
 *  sheet's top at 390 × 844, where `useSheetDrag` writes the same offset). */
const PULL = 120;
const GRAB_Y = 573;
const dragged = (
  <>
    <style>{`[role="dialog"][data-state="open"] { translate: 0 ${PULL}px; }`}</style>
    <TouchPoint top={GRAB_Y + PULL} left={195} />
  </>
);

const designFrames = frames([
  { ...phone, name: 'phone', title: '底部弹层打开 · 手机', render: sheetOverInbox() },
  { ...phone, name: 'phone-drag', title: '底部弹层 · 下拉中：过 30% 或快速下拉即关闭，否则回弹 · 手机', render: sheetOverInbox(dragged) },
  { ...phone, name: 'phone-reduced', title: '底部弹层 · 减少动态效果 · 手机', reducedMotion: true, render: sheetOverInbox() },
  {
    name: 'desktop',
    title: '对话框打开 · 桌面',
    width: 1200,
    height: 800,
    status: 'shipped',
    route: '/inbox',
    fixtures,
    replay: REPLAY,
    render: () => (
      <>
        {consolePage(<InboxPage onOpenSearch={() => {}} />)()}
        <NewSessionSheet open onClose={() => {}} onOpen={() => {}} />
      </>
    ),
  },
]);

export default designFrames;
